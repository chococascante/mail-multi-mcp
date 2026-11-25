from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import MemoryKeyring
from mail_multi_mcp.accounts.registry import (
    AccountRegistry,
    GoogleClient,
    MicrosoftClient,
    OAuthClients,
)
from mail_multi_mcp.accounts.router import AccountRouter
from mail_multi_mcp.auth.token_store import CHUNK_SIZE, TokenStore, account_key
from mail_multi_mcp.config import config_dir, data_dir, read_json
from mail_multi_mcp.ids import InvalidIdError
from mail_multi_mcp.models import Account, AccountMode, MailError, ProviderKind
from mail_multi_mcp.providers.base import MailProvider
from mail_multi_mcp.safety.guard import SafetyGuard


def test_dirs_from_env(tmp_path: Path) -> None:
    assert config_dir() == tmp_path / "config"
    assert data_dir() == tmp_path / "data"


def test_read_json_rejects_non_object(tmp_path: Path) -> None:
    p = tmp_path / "x.json"
    p.write_text("[1]")
    with pytest.raises(ValueError, match="objeto"):
        read_json(p, {})


class TestRegistry:
    def test_crud(
        self, registry: AccountRegistry, gmail_account: Account, graph_account: Account
    ) -> None:
        assert registry.list() == []
        registry.add(gmail_account)
        registry.add(graph_account)
        assert registry.get("PERSONAL") == gmail_account
        assert registry.get("alice@example.org") == graph_account
        with pytest.raises(MailError, match="alias"):
            registry.add(gmail_account)
        with pytest.raises(MailError, match="ya está registrada"):
            registry.add(
                Account(alias="otro", email="alice@example.com", provider=ProviderKind.GMAIL)
            )
        assert registry.set_mode("personal", AccountMode.READ_ONLY).mode is AccountMode.READ_ONLY
        assert registry.get("personal").mode is AccountMode.READ_ONLY
        assert registry.remove("outlook") == graph_account
        assert [a.alias for a in registry.list()] == ["personal"]
        for fn in (registry.remove, registry.get):
            with pytest.raises(MailError, match="desconocida"):
                fn("nada")
        with pytest.raises(MailError):
            registry.set_mode("nada", AccountMode.FULL)
        data = json.loads(registry.accounts_path.read_text())
        assert data["settings"]["max_sends_per_hour"] == 20

    def test_settings_and_oauth(self, registry: AccountRegistry) -> None:
        assert registry.settings().max_body_chars == 20_000
        assert registry.oauth_clients() == OAuthClients()
        registry.save_oauth_clients(
            OAuthClients(
                google=GoogleClient(client_id="gid.apps.example"),
                microsoft=MicrosoftClient(client_id="00000000-0000-0000-0000-000000000000"),
            )
        )
        clients = registry.oauth_clients()
        assert clients.google and clients.google.client_id == "gid.apps.example"
        assert clients.microsoft and clients.microsoft.authority.endswith("/consumers")
        assert "secret" not in registry.oauth_path.read_text()

    def test_account_validation(self) -> None:
        with pytest.raises(ValueError, match="email"):
            Account(alias="x", email="nope", provider=ProviderKind.GMAIL)
        assert (
            Account(alias="x", email=" A@Example.COM ", provider="gmail").email == "a@example.com"
        )


class TestTokenStore:
    def test_small_and_chunked(self, store: TokenStore, memory_keyring: MemoryKeyring) -> None:
        key = account_key("Alice@Example.com")
        assert key == "mail-multi-mcp:alice@example.com"
        assert store.get(key) is None and not store.has(key)
        store.set(key, "abc")
        assert store.get(key) == "abc" and store.has(key)
        big = "x" * (CHUNK_SIZE * 2 + 5)
        store.set(key, big)
        assert store.get(key) == big
        assert len(memory_keyring.data) == 4  # marcador + 3 trozos
        store.set(key, "small")  # limpia los trozos viejos
        assert len(memory_keyring.data) == 1
        store.set(key, big)
        del memory_keyring.data[(f"{key}#1", "token")]
        assert store.get(key) is None
        store.delete(key)
        store.delete(key)  # idempotente
        assert memory_keyring.data == {}


class TestGuard:
    def test_read_only_blocks(self, tmp_path: Path, readonly_account: Account) -> None:
        g = SafetyGuard(tmp_path / "audit.jsonl")
        for fn in (
            lambda: g.check_write(readonly_account, "reply"),
            lambda: g.check_send(readonly_account),
        ):
            with pytest.raises(MailError, match="solo lectura"):
                fn()

    def test_rate_limit_and_audit(self, tmp_path: Path, gmail_account: Account) -> None:
        now = [1_000_000.0]
        path = tmp_path / "data" / "audit.jsonl"
        g = SafetyGuard(path, max_sends_per_hour=2, clock=lambda: now[0])
        for _ in range(2):
            g.check_send(gmail_account)
            g.record(
                "send_draft",
                gmail_account,
                to=["bob@example.org"],
                subject="Hola",
                ok=True,
                ref="personal:d1",
            )
        g.record("send_draft", gmail_account, ok=False, error="x" * 900)
        with pytest.raises(MailError, match="Límite"):
            g.check_send(gmail_account)
        # Un reinicio no resetea el límite.
        g2 = SafetyGuard(path, max_sends_per_hour=2, clock=lambda: now[0])
        with pytest.raises(MailError, match="Límite"):
            g2.check_send(gmail_account)
        now[0] += 3601
        g.check_send(gmail_account)
        assert SafetyGuard(path, max_sends_per_hour=2, clock=lambda: now[0])._sends == deque_empty()

        lines = [json.loads(line) for line in path.read_text().splitlines()]
        assert lines[0]["to"] == ["bob@example.org"] and lines[0]["subject"] == "Hola"
        assert lines[0]["result"] == "ok" and "body" not in lines[0]
        assert lines[2]["result"] == "error" and len(lines[2]["error"]) == 500

    def test_corrupt_audit_lines_ignored(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        path.write_text("no json\n")
        assert len(SafetyGuard(path)._sends) == 0


def deque_empty() -> object:
    from collections import deque

    return deque()


class TestRouter:
    def make(self, registry: AccountRegistry, store: TokenStore) -> tuple[AccountRouter, list[str]]:
        built: list[str] = []

        def factory(acc: Account) -> MailProvider:
            built.append(acc.alias)
            return object()  # type: ignore[return-value]

        return AccountRouter(registry, store, factory), built

    def test_resolve_and_cache(
        self,
        registry: AccountRegistry,
        store: TokenStore,
        gmail_account: Account,
        graph_account: Account,
    ) -> None:
        router, built = self.make(registry, store)
        with pytest.raises(MailError, match="No hay cuentas"):
            router.resolve("all")
        registry.add(gmail_account)
        registry.add(graph_account)
        assert [a.alias for a in router.resolve(" ALL ")] == ["personal", "outlook"]
        assert router.resolve("alice@example.org") == [graph_account]
        acc, prov, native = router.for_id("personal:abc:def")
        assert acc == gmail_account and native == "abc:def"
        assert router.provider(gmail_account) is prov and built == ["personal"]
        router.forget("personal")
        router.provider(gmail_account)
        assert built == ["personal", "personal"]
        assert not router.has_credentials(gmail_account)
        store.set(account_key(gmail_account.email), "{}")
        assert router.has_credentials(gmail_account)
        with pytest.raises(InvalidIdError):
            router.for_id("sin-separador")
        with pytest.raises(MailError):
            router.for_id("desconocida:x")
