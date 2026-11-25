"""Integración en proceso: cliente MCP en memoria contra el servidor con providers falsos."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from mail_multi_mcp.accounts.registry import AccountRegistry
from mail_multi_mcp.accounts.router import AccountRouter
from mail_multi_mcp.auth.token_store import TokenStore, account_key
from mail_multi_mcp.models import (
    Account,
    DraftInput,
    DraftRef,
    MailError,
    MailQuery,
    Message,
    ReplyInput,
    SentRef,
    Settings,
    Thread,
    ThreadSummary,
)
from mail_multi_mcp.providers.base import MailProvider
from mail_multi_mcp.safety.guard import SafetyGuard
from mail_multi_mcp.server import UNTRUSTED, build_server


class FakeProvider:
    def __init__(self, account: Account, day: int, fail: Exception | None = None) -> None:
        self.account = account
        self.day = day
        self.fail = fail
        self.queries: list[MailQuery] = []
        self.sent: list[str] = []

    def _check(self) -> None:
        if self.fail:
            raise self.fail

    def search(self, q: MailQuery, max_results: int) -> list[ThreadSummary]:
        self._check()
        self.queries.append(q)
        a = self.account.alias
        return [
            ThreadSummary(
                id=f"{a}:t{i}",
                account=a,
                subject=f"{a} {i}",
                date=datetime(2026, 1, self.day + i, tzinfo=UTC),
            )
            for i in range(max_results)
        ]

    def get_thread(self, thread_id: str) -> Thread:
        self._check()
        a = self.account.alias
        return Thread(
            id=f"{a}:{thread_id}",
            account=a,
            subject="Hola",
            messages=[Message(id=f"{a}:m1", thread_id=f"{a}:{thread_id}", body="hola")],
        )

    def create_draft(self, d: DraftInput) -> DraftRef:
        self._check()
        return DraftRef(
            id=f"{self.account.alias}:d1",
            account=self.account.alias,
            to=d.to,
            subject=d.subject,
            preview=d.body,
        )

    def reply(self, message_id: str, r: ReplyInput) -> DraftRef:
        self._check()
        return DraftRef(
            id=f"{self.account.alias}:d-{message_id}",
            account=self.account.alias,
            to=["carol@example.org"],
            subject="Re: Hola",
            preview=r.body,
        )

    def send_draft(self, draft_id: str) -> SentRef:
        self._check()
        self.sent.append(draft_id)
        return SentRef(id=f"{self.account.alias}:{draft_id}", account=self.account.alias)


@pytest.fixture
def env(
    tmp_path: Path,
    registry: AccountRegistry,
    store: TokenStore,
    gmail_account: Account,
    graph_account: Account,
    readonly_account: Account,
) -> dict[str, Any]:
    for a in (gmail_account, graph_account, readonly_account):
        registry.add(a)
    store.set(account_key(gmail_account.email), "{}")
    providers: dict[str, FakeProvider] = {
        "personal": FakeProvider(gmail_account, 1),
        "outlook": FakeProvider(graph_account, 10),
        "archivo": FakeProvider(readonly_account, 5, fail=MailError("token vencido")),
    }

    def factory(acc: Account) -> MailProvider:
        return providers[acc.alias]

    router = AccountRouter(registry, store, factory)
    audit = tmp_path / "audit.jsonl"
    guard = SafetyGuard(audit, max_sends_per_hour=1)
    return {
        "server": build_server(router, guard, Settings()),
        "providers": providers,
        "audit": audit,
    }


async def call(server: Any, name: str, args: dict[str, Any] | None = None) -> Any:
    async with Client(server) as client:
        return await client.call_tool(name, args or {})


def data(result: Any) -> dict[str, Any]:
    assert not result.is_error, result.content
    if result.structured_content is not None:
        sc: dict[str, Any] = result.structured_content
        return sc.get("result", sc) if set(sc) == {"result"} else sc
    parsed: dict[str, Any] = json.loads(result.content[0].text)
    return parsed


def error_text(result: Any) -> str:
    assert result.is_error
    return str(result.content[0].text)


async def test_list_tools_and_annotations(env: dict[str, Any]) -> None:
    async with Client(env["server"]) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {
        "list_accounts",
        "search_threads",
        "get_thread",
        "reply",
        "create_draft",
        "send_draft",
    }
    assert tools["search_threads"].annotations.read_only_hint is True
    assert tools["get_thread"].annotations.read_only_hint is True
    send = tools["send_draft"].annotations
    assert send.destructive_hint is True and send.idempotent_hint is False
    assert "NO CONFIABLE" in (tools["search_threads"].description or "")
    assert "explícitamente" in (tools["reply"].description or "")


async def test_list_accounts(env: dict[str, Any]) -> None:
    out = data(await call(env["server"], "list_accounts"))
    by_alias = {a["alias"]: a for a in out["accounts"]}
    assert by_alias["personal"]["has_credentials"] is True
    assert by_alias["outlook"]["has_credentials"] is False
    assert by_alias["archivo"]["mode"] == "read_only"


async def test_search_all_fanout_with_failure(env: dict[str, Any]) -> None:
    out = data(
        await call(
            env["server"],
            "search_threads",
            {"account": "all", "query": {"from": "carol@example.org"}, "max_results": 2},
        )
    )
    assert [t["id"] for t in out["threads"]] == ["outlook:t1", "outlook:t0"]
    assert out["errors"] == [{"account": "archivo", "error": "MailError: token vencido"}]
    assert env["providers"]["personal"].queries[0].from_ == "carol@example.org"


async def test_search_single_account(env: dict[str, Any]) -> None:
    out = data(
        await call(
            env["server"], "search_threads", {"account": "alice@example.com", "max_results": 3}
        )
    )
    assert [t["account"] for t in out["threads"]] == ["personal"] * 3
    assert "errors" not in out
    err = error_text(await call(env["server"], "search_threads", {"account": "archivo"}))
    assert "token vencido" in err
    err = error_text(await call(env["server"], "search_threads", {"account": "nadie"}))
    assert "desconocida" in err


async def test_get_thread(env: dict[str, Any]) -> None:
    out = data(await call(env["server"], "get_thread", {"thread_id": "outlook:c1"}))
    assert out["id"] == "outlook:c1" and out["warning"] == UNTRUSTED
    err = error_text(await call(env["server"], "get_thread", {"thread_id": "malo"}))
    assert "ID inválido" in err


async def test_reply_send_flow_and_audit(env: dict[str, Any]) -> None:
    server = env["server"]
    async with Client(server) as client:
        r = data(await client.call_tool("reply", {"message_id": "personal:m1", "body": "Gracias"}))
        assert r["draft_id"] == "personal:d-m1" and r["preview"] == "Gracias"
        s = data(await client.call_tool("send_draft", {"draft_id": r["draft_id"]}))
        assert s["sent"] is True and s["id"] == "personal:d-m1"
        # límite de 1 envío por hora
        err = error_text(await client.call_tool("send_draft", {"draft_id": "personal:d9"}))
        assert "Límite" in err
    assert env["providers"]["personal"].sent == ["d-m1"]
    entries = [json.loads(x) for x in env["audit"].read_text().splitlines()]
    assert [e["action"] for e in entries] == ["reply", "send_draft"]
    assert entries[1]["to"] == ["carol@example.org"] and entries[1]["subject"] == "Re: Hola"
    assert all("Gracias" not in json.dumps(e) for e in entries)


async def test_read_only_blocks_writes(env: dict[str, Any]) -> None:
    server = env["server"]
    for name, args in (
        ("reply", {"message_id": "archivo:m1", "body": "x"}),
        ("send_draft", {"draft_id": "archivo:d1"}),
        (
            "create_draft",
            {"account": "archivo", "to": ["bob@example.org"], "subject": "s", "body": "b"},
        ),
    ):
        assert "solo lectura" in error_text(await call(server, name, args))


async def test_create_draft(env: dict[str, Any]) -> None:
    out = data(
        await call(
            env["server"],
            "create_draft",
            {
                "account": "outlook",
                "to": ["bob@example.org"],
                "subject": "Hola",
                "body": "Texto",
                "cc": ["carol@example.org"],
            },
        )
    )
    assert out["draft_id"] == "outlook:d1"
    err = error_text(
        await call(
            env["server"],
            "create_draft",
            {"account": "outlook", "to": [], "subject": "s", "body": "b"},
        )
    )
    assert "destinatario" in err


async def test_provider_failures_are_audited(env: dict[str, Any]) -> None:
    prov = env["providers"]["outlook"]
    prov.fail = RuntimeError("caída")
    server = env["server"]
    assert "RuntimeError: caída" in error_text(
        await call(server, "reply", {"message_id": "outlook:m1", "body": "x"})
    )
    assert "caída" in error_text(
        await call(
            server,
            "create_draft",
            {"account": "outlook", "to": ["b@example.org"], "subject": "s", "body": "b"},
        )
    )
    assert "caída" in error_text(await call(server, "send_draft", {"draft_id": "outlook:d1"}))
    results = [json.loads(x)["result"] for x in env["audit"].read_text().splitlines()]
    assert results == ["error", "error", "error"]
