"""Proveedor Microsoft Graph (Outlook.com / Microsoft 365) sobre httpx.

Limitaciones conocidas de Graph para ``/me/messages``:

* ``$search`` (KQL) **no** se puede combinar con ``$filter`` ni con ``$orderby``.
  Si la consulta necesita texto/remitente/asunto/etc. se usa solo ``$search`` y
  las fechas se expresan en KQL (``received>=``). ``unread`` no existe en KQL,
  así que se filtra del lado del cliente sobre una página más grande.
* Con ``$filter`` + ``$orderby`` Graph exige que la propiedad ordenada aparezca
  primero en el filtro (si no, responde ``InefficientFilter``); por eso siempre
  se incluye ``receivedDateTime ge …`` como primera cláusula.
* ``$search`` devuelve como máximo 1000 resultados y sin paginación ordenada.
"""

from __future__ import annotations

import html
from collections.abc import Callable, Generator
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

from ..ids import format_id
from ..models import (
    Account,
    Address,
    DraftInput,
    DraftRef,
    MailError,
    MailQuery,
    Message,
    ReplyInput,
    SentRef,
    Thread,
    ThreadSummary,
)
from .text import html_to_text, preview, truncate

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SUMMARY_SELECT = "id,conversationId,subject,from,receivedDateTime,isRead,hasAttachments,bodyPreview"
FULL_SELECT = SUMMARY_SELECT + ",toRecipients,ccRecipients,body"
MAX_THREAD_PAGES = 5


@dataclass(frozen=True)
class GraphQuery:
    params: dict[str, str]
    client_unread: bool | None = None


def _kql_value(value: str) -> list[str]:
    cleaned = value.replace('"', " ").replace("'", " ")
    return [w for w in cleaned.split() if w]


def to_graph_query(q: MailQuery, max_results: int) -> GraphQuery:
    terms: list[str] = []
    if q.text:
        terms.extend(_kql_value(q.text))
    for prop, value in (("from", q.from_), ("to", q.to), ("subject", q.subject)):
        if value:
            terms.extend(f"{prop}:{w}" for w in _kql_value(value))
    if q.raw:
        terms.append(q.raw.replace('"', "'").strip())

    if terms:
        if q.after:
            terms.append(f"received>={q.after.isoformat()}")
        if q.before:
            terms.append(f"received<{q.before.isoformat()}")
        if q.has_attachment is not None:
            terms.append(f"hasAttachments:{str(q.has_attachment).lower()}")
        top = max_results if q.unread is None else min(max(max_results * 3, 25), 100)
        return GraphQuery(
            params={"$search": '"' + " AND ".join(terms) + '"', "$top": str(top)},
            client_unread=q.unread,
        )

    after = q.after.isoformat() if q.after else "1900-01-01"
    clauses = [f"receivedDateTime ge {after}T00:00:00Z"]
    if q.before:
        clauses.append(f"receivedDateTime lt {q.before.isoformat()}T00:00:00Z")
    if q.unread is not None:
        clauses.append(f"isRead eq {str(not q.unread).lower()}")
    if q.has_attachment is not None:
        clauses.append(f"hasAttachments eq {str(q.has_attachment).lower()}")
    return GraphQuery(
        params={
            "$filter": " and ".join(clauses),
            "$orderby": "receivedDateTime desc",
            "$top": str(max(max_results * 2, 10)),
        }
    )


class _BearerAuth(httpx.Auth):
    def __init__(self, token_provider: Callable[[], str]) -> None:
        self._tp = token_provider

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = f"Bearer {self._tp()}"
        yield request


def _addr(obj: dict[str, Any] | None) -> Address | None:
    ea = (obj or {}).get("emailAddress") or {}
    if not ea.get("address"):
        return None
    return Address(email=str(ea["address"]).lower(), name=ea.get("name") or None)


def _addrs(items: list[dict[str, Any]] | None) -> list[Address]:
    return [a for a in (_addr(i) for i in items or []) if a]


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


class GraphProvider:
    def __init__(
        self,
        account: Account,
        token_provider: Callable[[], str],
        *,
        max_body_chars: int = 20_000,
        client: httpx.Client | None = None,
    ) -> None:
        self.account = account
        self._max_body = max_body_chars
        self._http = client or httpx.Client(base_url=GRAPH_BASE, timeout=30.0)
        self._http.auth = _BearerAuth(token_provider)

    def _id(self, native: str) -> str:
        return format_id(self.account.alias, native)

    def _req(self, method: str, url: str, **kw: Any) -> dict[str, Any]:
        resp = self._http.request(method, url, **kw)
        if resp.status_code >= 400:
            try:
                err = resp.json().get("error", {})
                detail = f"{err.get('code')}: {err.get('message')}"
            except ValueError:
                detail = resp.text[:200]
            raise MailError(f"Graph {resp.status_code} en {method} {url.split('?')[0]}: {detail}")
        if not resp.content:
            return {}
        data: dict[str, Any] = resp.json()
        return data

    @staticmethod
    def _msg_path(message_id: str) -> str:
        return f"/me/messages/{quote(message_id, safe='')}"

    # -- lectura -----------------------------------------------------------
    def search(self, q: MailQuery, max_results: int) -> list[ThreadSummary]:
        gq = to_graph_query(q, max_results)
        params = {**gq.params, "$select": SUMMARY_SELECT}
        items = self._req("GET", "/me/messages", params=params).get("value", [])
        if gq.client_unread is not None:
            items = [m for m in items if bool(m.get("isRead")) is not gq.client_unread]
        items.sort(key=lambda m: m.get("receivedDateTime") or "", reverse=True)

        threads: dict[str, ThreadSummary] = {}
        for m in items:
            cid = m.get("conversationId") or m["id"]
            if cid in threads:
                threads[cid].message_count += 1
                threads[cid].unread = threads[cid].unread or not m.get("isRead", True)
                continue
            if len(threads) >= max_results:
                continue
            threads[cid] = ThreadSummary(
                id=self._id(cid),
                account=self.account.alias,
                subject=m.get("subject") or "",
                snippet=m.get("bodyPreview") or "",
                from_=_addr(m.get("from")),
                date=_dt(m.get("receivedDateTime")),
                unread=not m.get("isRead", True),
                has_attachment=bool(m.get("hasAttachments")),
            )
        return list(threads.values())

    def get_thread(self, thread_id: str) -> Thread:
        escaped = thread_id.replace("'", "''")
        params: dict[str, str] | None = {
            "$filter": f"conversationId eq '{escaped}'",
            "$select": FULL_SELECT,
            "$top": "50",
        }
        headers = {"Prefer": 'outlook.body-content-type="text"'}
        url = "/me/messages"
        raw: list[dict[str, Any]] = []
        for _ in range(MAX_THREAD_PAGES):
            data = self._req("GET", url, params=params, headers=headers)
            raw.extend(data.get("value", []))
            next_link = data.get("@odata.nextLink")
            if not next_link:
                break
            url, params = next_link, None
        if not raw:
            raise MailError(f"Hilo no encontrado: {thread_id}")
        raw.sort(key=lambda m: m.get("receivedDateTime") or "")
        messages = [self._message(m, thread_id) for m in raw]
        return Thread(
            id=self._id(thread_id),
            account=self.account.alias,
            subject=messages[0].subject,
            messages=messages,
        )

    def _message(self, m: dict[str, Any], thread_id: str) -> Message:
        body = m.get("body") or {}
        content = body.get("content") or ""
        if (body.get("contentType") or "").lower() == "html":
            content = html_to_text(content)
        content, truncated = truncate(content.strip(), self._max_body)
        return Message(
            id=self._id(m["id"]),
            thread_id=self._id(m.get("conversationId") or thread_id),
            from_=_addr(m.get("from")),
            to=_addrs(m.get("toRecipients")),
            cc=_addrs(m.get("ccRecipients")),
            subject=m.get("subject") or "",
            date=_dt(m.get("receivedDateTime")),
            unread=not m.get("isRead", True),
            body=content,
            body_truncated=truncated,
        )

    # -- escritura (siempre borradores) -------------------------------------
    def _draft_ref(self, d: dict[str, Any], body_text: str) -> DraftRef:
        cid = d.get("conversationId")
        return DraftRef(
            id=self._id(d["id"]),
            account=self.account.alias,
            thread_id=self._id(cid) if cid else None,
            to=[a.email for a in _addrs(d.get("toRecipients"))],
            cc=[a.email for a in _addrs(d.get("ccRecipients"))],
            subject=d.get("subject") or "",
            preview=preview(body_text),
        )

    def create_draft(self, d: DraftInput) -> DraftRef:
        payload = {
            "subject": d.subject,
            "body": {"contentType": "Text", "content": d.body},
            "toRecipients": [{"emailAddress": {"address": a}} for a in d.to],
            "ccRecipients": [{"emailAddress": {"address": a}} for a in d.cc],
        }
        created = self._req("POST", "/me/messages", json=payload)
        return self._draft_ref(created, d.body)

    def reply(self, message_id: str, r: ReplyInput) -> DraftRef:
        action = "createReplyAll" if r.reply_all else "createReply"
        draft = self._req("POST", f"{self._msg_path(message_id)}/{action}", json={})
        quoted = (draft.get("body") or {}).get("content") or ""
        ours = "<div>" + html.escape(r.body).replace("\n", "<br>") + "</div><br>"
        patched = self._req(
            "PATCH",
            self._msg_path(draft["id"]),
            json={"body": {"contentType": "HTML", "content": ours + quoted}},
        )
        return self._draft_ref({**draft, **patched}, r.body)

    def send_draft(self, draft_id: str) -> SentRef:
        self._req("POST", f"{self._msg_path(draft_id)}/send")
        return SentRef(id=self._id(draft_id), account=self.account.alias)
