"""Fixtures comunes. Todos los datos son sintéticos (example.com / example.org)."""

from __future__ import annotations

import base64
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import keyring
import keyring.backend
import keyring.errors
import pytest

from mail_multi_mcp.accounts.registry import AccountRegistry
from mail_multi_mcp.auth.token_store import TokenStore
from mail_multi_mcp.models import Account, AccountMode, ProviderKind


class MemoryKeyring(keyring.backend.KeyringBackend):
    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.data: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.data.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.data[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.data:
            raise keyring.errors.PasswordDeleteError(service)
        del self.data[(service, username)]


@pytest.fixture(autouse=True)
def memory_keyring(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[MemoryKeyring]:
    previous = keyring.get_keyring()
    kr = MemoryKeyring()
    keyring.set_keyring(kr)
    monkeypatch.setenv("MAIL_MULTI_MCP_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("MAIL_MULTI_MCP_DATA_DIR", str(tmp_path / "data"))
    yield kr
    keyring.set_keyring(previous)


@pytest.fixture
def store() -> TokenStore:
    return TokenStore()


@pytest.fixture
def registry(tmp_path: Path) -> AccountRegistry:
    return AccountRegistry(tmp_path / "config")


@pytest.fixture
def gmail_account() -> Account:
    return Account(alias="personal", email="alice@example.com", provider=ProviderKind.GMAIL)


@pytest.fixture
def graph_account() -> Account:
    return Account(alias="outlook", email="alice@example.org", provider=ProviderKind.GRAPH)


@pytest.fixture
def readonly_account() -> Account:
    return Account(
        alias="archivo",
        email="bob@example.com",
        provider=ProviderKind.GMAIL,
        mode=AccountMode.READ_ONLY,
    )


# --------------------------------------------------------------------------- Gmail fake


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def headers(**kw: str) -> list[dict[str, str]]:
    names = {"message_id": "Message-ID", "reply_to": "Reply-To", "from_": "From"}
    return [{"name": names.get(k, k.capitalize()), "value": v} for k, v in kw.items()]


class _Req:
    def __init__(self, fn: Callable[[], Any]) -> None:
        self._fn = fn

    def execute(self) -> Any:
        return self._fn()


class FakeGmail:
    """Imita la interfaz encadenada de googleapiclient para users().*."""

    def __init__(self) -> None:
        self.threads_data: dict[str, dict[str, Any]] = {}
        self.messages_data: dict[str, dict[str, Any]] = {}
        self.list_result: dict[str, Any] = {"threads": []}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.created: list[dict[str, Any]] = []
        self.sent: list[str] = []

    def users(self) -> FakeGmail:
        return self

    def threads(self) -> FakeGmail:
        self._kind = "threads"
        return self

    def messages(self) -> FakeGmail:
        self._kind = "messages"
        return self

    def drafts(self) -> FakeGmail:
        self._kind = "drafts"
        return self

    def list(self, **kw: Any) -> _Req:
        self.calls.append(("threads.list", kw))
        return _Req(lambda: self.list_result)

    def get(self, **kw: Any) -> _Req:
        kind = self._kind
        self.calls.append((f"{kind}.get", kw))
        source = self.threads_data if kind == "threads" else self.messages_data
        return _Req(lambda: source[kw["id"]])

    def create(self, **kw: Any) -> _Req:
        self.calls.append(("drafts.create", kw))
        self.created.append(kw["body"])
        thread = kw["body"]["message"].get("threadId", "t-new")
        return _Req(lambda: {"id": f"d{len(self.created)}", "message": {"threadId": thread}})

    def send(self, **kw: Any) -> _Req:
        self.calls.append(("drafts.send", kw))
        self.sent.append(kw["body"]["id"])
        return _Req(lambda: {"id": "m-sent", "threadId": "t-sent"})

    def getProfile(self, **kw: Any) -> _Req:
        return _Req(lambda: {"emailAddress": "alice@example.com"})


@pytest.fixture
def fake_gmail() -> FakeGmail:
    return FakeGmail()
