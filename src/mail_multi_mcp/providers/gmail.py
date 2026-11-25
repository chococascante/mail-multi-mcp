"""Proveedor Gmail (Gmail API v1 vía google-api-python-client)."""

from __future__ import annotations

import base64
import re
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Any

from ..ids import format_id
from ..models import (
    Account,
    Address,
    AttachmentRef,
    DraftInput,
    DraftRef,
    MailQuery,
    Message,
    ReplyInput,
    SentRef,
    Thread,
    ThreadSummary,
)
from .text import (
    dedupe_excluding,
    format_address,
    html_to_text,
    parse_addresses,
    preview,
    reply_subject,
    truncate,
)

SUMMARY_HEADERS = ["From", "Subject", "Date"]
REPLY_HEADERS = ["From", "To", "Cc", "Reply-To", "Subject", "Message-ID", "References"]
_CHARSET_RE = re.compile(r'charset="?([\w.-]+)"?', re.IGNORECASE)


def build_service(credentials: Any) -> Any:  # pragma: no cover - requiere credenciales reales
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


# --------------------------------------------------------------------------- query


def _quote(value: str) -> str:
    value = value.replace('"', "").strip()
    return f'"{value}"' if re.search(r"\s", value) else value


def to_gmail_query(q: MailQuery) -> str:
    parts: list[str] = []
    if q.text:
        parts.append(q.text.strip())
    if q.from_:
        parts.append(f"from:{_quote(q.from_)}")
    if q.to:
        parts.append(f"to:{_quote(q.to)}")
    if q.subject:
        parts.append(f"subject:({q.subject.replace('(', ' ').replace(')', ' ').strip()})")
    if q.after:
        parts.append(f"after:{q.after:%Y/%m/%d}")
    if q.before:
        parts.append(f"before:{q.before:%Y/%m/%d}")
    if q.unread is not None:
        parts.append("is:unread" if q.unread else "-is:unread")
    if q.has_attachment is not None:
        parts.append("has:attachment" if q.has_attachment else "-has:attachment")
    if q.raw:
        parts.append(f"({q.raw.strip()})")
    return " ".join(parts)


# --------------------------------------------------------------------------- helpers


def _headers(payload: dict[str, Any]) -> dict[str, str]:
    return {h["name"].lower(): h.get("value", "") for h in payload.get("headers", [])}


def _ts(message: dict[str, Any]) -> datetime | None:
    raw = message.get("internalDate")
    return datetime.fromtimestamp(int(raw) / 1000, tz=UTC) if raw else None


def _decode(data: str, charset: str | None) -> str:
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    try:
        return raw.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _walk(part: dict[str, Any]) -> list[dict[str, Any]]:
    out = [part]
    for child in part.get("parts", []) or []:
        out.extend(_walk(child))
    return out


def extract_body(payload: dict[str, Any]) -> tuple[str, list[AttachmentRef]]:
    plain: list[str] = []
    htmls: list[str] = []
    attachments: list[AttachmentRef] = []
    for part in _walk(payload):
        body = part.get("body", {}) or {}
        mime = (part.get("mimeType") or "").lower()
        if part.get("filename"):
            if body.get("attachmentId"):
                attachments.append(
                    AttachmentRef(
                        id=body["attachmentId"],
                        filename=part["filename"],
                        mime_type=mime or None,
                        size=body.get("size"),
                    )
                )
            continue
        if not body.get("data"):
            continue
        m = _CHARSET_RE.search(_headers(part).get("content-type", ""))
        text = _decode(body["data"], m.group(1) if m else None)
        if mime == "text/plain":
            plain.append(text)
        elif mime == "text/html":
            htmls.append(text)
    if plain:
        return "\n".join(plain).strip(), attachments
    return html_to_text("\n".join(htmls)), attachments


def compute_reply_recipients(
    self_email: str,
    from_: list[Address],
    reply_to: list[Address],
    to: list[Address],
    cc: list[Address],
    reply_all: bool,
) -> tuple[list[Address], list[Address]]:
    me = {self_email.lower()}
    sent_by_me = any(a.email == self_email.lower() for a in from_)
    primary = to if sent_by_me else (reply_to or from_)
    to_list = dedupe_excluding(primary, me)
    cc_list: list[Address] = []
    if reply_all:
        if not sent_by_me:
            to_list = dedupe_excluding(to_list + to, me)
        cc_list = dedupe_excluding(cc, me | {a.email for a in to_list})
    if not to_list:
        to_list = [Address(email=self_email.lower())]
    return to_list, cc_list


# --------------------------------------------------------------------------- provider


class GmailProvider:
    def __init__(self, account: Account, service: Any, *, max_body_chars: int = 20_000) -> None:
        self.account = account
        self._svc = service
        self._max_body = max_body_chars

    def _id(self, native: str) -> str:
        return format_id(self.account.alias, native)

    # -- lectura -----------------------------------------------------------
    def search(self, q: MailQuery, max_results: int) -> list[ThreadSummary]:
        users = self._svc.users()
        resp = users.threads().list(userId="me", q=to_gmail_query(q), maxResults=max_results)
        listing = resp.execute()
        out: list[ThreadSummary] = []
        for item in listing.get("threads", [])[:max_results]:
            t = (
                users.threads()
                .get(userId="me", id=item["id"], format="metadata", metadataHeaders=SUMMARY_HEADERS)
                .execute()
            )
            out.append(self._summary(t))
        return out

    def _summary(self, t: dict[str, Any]) -> ThreadSummary:
        msgs = t.get("messages", []) or [{}]
        first, last = msgs[0], msgs[-1]
        first_h = _headers(first.get("payload", {}))
        last_h = _headers(last.get("payload", {}))
        sender = parse_addresses(last_h.get("from"))
        return ThreadSummary(
            id=self._id(t["id"]),
            account=self.account.alias,
            subject=first_h.get("subject", ""),
            snippet=last.get("snippet", "") or t.get("snippet", ""),
            from_=sender[0] if sender else None,
            date=_ts(last),
            unread=any("UNREAD" in (m.get("labelIds") or []) for m in msgs),
            # Heurística: con format=metadata no vemos las partes; multipart/mixed ≈ adjunto.
            has_attachment=any(
                (m.get("payload", {}).get("mimeType") or "").lower() == "multipart/mixed"
                for m in msgs
            ),
            message_count=len(t.get("messages", [])),
        )

    def get_thread(self, thread_id: str) -> Thread:
        t = self._svc.users().threads().get(userId="me", id=thread_id, format="full").execute()
        messages: list[Message] = []
        for m in t.get("messages", []):
            payload = m.get("payload", {})
            h = _headers(payload)
            body, attachments = extract_body(payload)
            body, truncated = truncate(body, self._max_body)
            sender = parse_addresses(h.get("from"))
            messages.append(
                Message(
                    id=self._id(m["id"]),
                    thread_id=self._id(m.get("threadId", thread_id)),
                    from_=sender[0] if sender else None,
                    to=parse_addresses(h.get("to")),
                    cc=parse_addresses(h.get("cc")),
                    subject=h.get("subject", ""),
                    date=_ts(m),
                    unread="UNREAD" in (m.get("labelIds") or []),
                    body=body,
                    body_truncated=truncated,
                    attachments=attachments,
                )
            )
        return Thread(
            id=self._id(t["id"]),
            account=self.account.alias,
            subject=messages[0].subject if messages else "",
            messages=messages,
        )

    # -- escritura (siempre borradores) -------------------------------------
    def _create(self, msg: EmailMessage, thread_id: str | None) -> dict[str, Any]:
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        message: dict[str, Any] = {"raw": raw}
        if thread_id:
            message["threadId"] = thread_id
        draft: dict[str, Any] = (
            self._svc.users().drafts().create(userId="me", body={"message": message}).execute()
        )
        return draft

    def create_draft(self, d: DraftInput) -> DraftRef:
        msg = EmailMessage()
        msg["From"] = self.account.email
        msg["To"] = ", ".join(d.to)
        if d.cc:
            msg["Cc"] = ", ".join(d.cc)
        msg["Subject"] = d.subject
        msg.set_content(d.body)
        draft = self._create(msg, None)
        thread = (draft.get("message") or {}).get("threadId")
        return DraftRef(
            id=self._id(draft["id"]),
            account=self.account.alias,
            thread_id=self._id(thread) if thread else None,
            to=list(d.to),
            cc=list(d.cc),
            subject=d.subject,
            preview=preview(d.body),
        )

    def reply(self, message_id: str, r: ReplyInput) -> DraftRef:
        orig = (
            self._svc.users()
            .messages()
            .get(userId="me", id=message_id, format="metadata", metadataHeaders=REPLY_HEADERS)
            .execute()
        )
        h = _headers(orig.get("payload", {}))
        to_list, cc_list = compute_reply_recipients(
            self.account.email,
            parse_addresses(h.get("from")),
            parse_addresses(h.get("reply-to")),
            parse_addresses(h.get("to")),
            parse_addresses(h.get("cc")),
            r.reply_all,
        )
        subject = reply_subject(h.get("subject", ""))
        msg = EmailMessage()
        msg["From"] = self.account.email
        msg["To"] = ", ".join(format_address(a) for a in to_list)
        if cc_list:
            msg["Cc"] = ", ".join(format_address(a) for a in cc_list)
        msg["Subject"] = subject
        message_id_hdr = h.get("message-id", "").strip()
        if message_id_hdr:
            msg["In-Reply-To"] = message_id_hdr
            refs = h.get("references", "").split()
            if message_id_hdr not in refs:
                refs.append(message_id_hdr)
            msg["References"] = " ".join(refs)
        msg.set_content(r.body)
        thread_id = orig.get("threadId")
        draft = self._create(msg, thread_id)
        thread_id = thread_id or (draft.get("message") or {}).get("threadId")
        return DraftRef(
            id=self._id(draft["id"]),
            account=self.account.alias,
            thread_id=self._id(thread_id) if thread_id else None,
            to=[a.email for a in to_list],
            cc=[a.email for a in cc_list],
            subject=subject,
            preview=preview(r.body),
        )

    def send_draft(self, draft_id: str) -> SentRef:
        sent = self._svc.users().drafts().send(userId="me", body={"id": draft_id}).execute()
        thread = sent.get("threadId")
        return SentRef(
            id=self._id(sent["id"]),
            account=self.account.alias,
            thread_id=self._id(thread) if thread else None,
        )
