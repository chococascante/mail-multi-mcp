from __future__ import annotations

import json
from typing import Any

import pytest

from mail_multi_mcp.accounts.registry import AccountRegistry
from mail_multi_mcp.auth import google as gauth
from mail_multi_mcp.auth.microsoft import SCOPES as MS_SCOPES
from mail_multi_mcp.auth.microsoft import MicrosoftAuth
from mail_multi_mcp.auth.token_store import TokenStore, account_key, client_secret_key
from mail_multi_mcp.cli import Deps, run
from mail_multi_mcp.models import AccountMode, MailError


class Recorder:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, line: str) -> None:
        self.lines.append(line)


def make_deps(
    registry: AccountRegistry, store: TokenStore, answers: list[str] | None = None, **kw: Any
) -> tuple[Deps, Recorder]:
    out = Recorder()
    queue = list(answers or [])
    deps = Deps(
        registry=registry,
        store=store,
        ask=lambda _p: queue.pop(0),
        ask_secret=lambda _p: queue.pop(0),
        out=out,
        **kw,
    )
    return deps, out


class TestCli:
    def test_full_lifecycle(self, registry: AccountRegistry, store: TokenStore) -> None:
        def g_login(cid: str, secret: str) -> tuple[str, str]:
            assert (cid, secret) == ("gid.apps.example", "gsecret-test")
            return "Alice@Example.com", '{"refresh_token": "r"}'

        def ms_login(cid: str, authority: str, st: TokenStore) -> str:
            assert authority.endswith("/common")
            st.set(account_key("alice@example.org"), "cache")
            return "alice@example.org"

        deps, out = make_deps(
            registry,
            store,
            ["gid.apps.example", "gsecret-test", "ms-client-id"],
            google_login=g_login,
            ms_login=ms_login,
        )
        assert run(["list"], deps) == 0 and "No hay cuentas" in out.lines[-1]
        with pytest.raises(MailError, match="setup-google"):
            run(["add", "--provider", "gmail", "--alias", "personal"], deps)
        with pytest.raises(MailError, match="setup-microsoft"):
            run(["add", "--provider", "graph", "--alias", "outlook"], deps)

        run(["setup-google"], deps)
        assert store.get(client_secret_key("google")) == "gsecret-test"
        assert "gsecret-test" not in registry.oauth_path.read_text()
        run(["setup-microsoft", "--tenant", "common"], deps)

        run(["add", "--provider", "gmail", "--alias", "personal", "--mode", "read_only"], deps)
        assert store.get(account_key("alice@example.com")) == '{"refresh_token": "r"}'
        run(["add", "--provider", "graph", "--alias", "outlook"], deps)
        with pytest.raises(MailError, match="Ya existe"):
            run(["add", "--provider", "graph", "--alias", "outlook"], deps)
        with pytest.raises(MailError, match="ya está registrada"):
            run(["add", "--provider", "graph", "--alias", "otra"], deps)
        assert store.get(account_key("alice@example.org")) is None  # rollback del token

        out.lines.clear()
        run(["list"], deps)
        assert "read_only" in out.lines[0] and "ok" in out.lines[0]
        assert "SIN CREDENCIALES" in out.lines[1]

        run(["set-mode", "personal", "full"], deps)
        assert registry.get("personal").mode is AccountMode.FULL
        run(["remove", "personal"], deps)
        assert store.get(account_key("alice@example.com")) is None
        assert [a.alias for a in registry.list()] == ["outlook"]

    def test_setup_validation(self, registry: AccountRegistry, store: TokenStore) -> None:
        deps, _ = make_deps(registry, store, ["", "", ""])
        with pytest.raises(MailError, match="obligatorios"):
            run(["setup-google"], deps)
        with pytest.raises(MailError, match="obligatorio"):
            run(["setup-microsoft"], deps)
        with pytest.raises(ValueError, match="Alias"):
            run(["add", "--provider", "gmail", "--alias", "Mal Alias"], deps)


class FakeMsalApp:
    def __init__(self, cache: Any, result: dict[str, Any]) -> None:
        self.cache = cache
        self.result = result
        self.accounts: list[dict[str, str]] = []

    def acquire_token_interactive(self, scopes: list[str], prompt: str) -> dict[str, Any]:
        assert "offline_access" not in scopes and prompt == "select_account"
        if "access_token" in self.result:
            self.cache.add(
                {
                    "client_id": "cid",
                    "scope": scopes,
                    "token_endpoint": "https://login.example.org/token",
                    "response": {
                        "access_token": "a",
                        "refresh_token": "r",
                        "token_type": "Bearer",
                        "expires_in": 3600,
                    },
                }
            )
        return self.result

    def get_accounts(self, username: str | None = None) -> list[dict[str, str]]:
        return self.accounts

    def acquire_token_silent(self, scopes: list[str], account: Any) -> dict[str, Any] | None:
        return {"access_token": "silent-tok"} if account else None


class TestMicrosoftAuth:
    def make(
        self, store: TokenStore, result: dict[str, Any], email: str | None = None
    ) -> tuple[MicrosoftAuth, list[FakeMsalApp]]:
        apps: list[FakeMsalApp] = []

        def factory(cid: str, authority: str, cache: Any) -> FakeMsalApp:
            apps.append(FakeMsalApp(cache, result))
            return apps[-1]

        return MicrosoftAuth(
            "cid", "https://login.example.org/consumers", store, email=email, app_factory=factory
        ), apps

    def test_login_persists_cache(self, store: TokenStore) -> None:
        auth, _ = self.make(
            store,
            {"access_token": "a", "id_token_claims": {"preferred_username": "Alice@Example.org"}},
        )
        assert auth.login_interactive() == "alice@example.org"
        cached = store.get(account_key("alice@example.org"))
        assert cached and "AccessToken" in json.loads(cached)

        # Una instancia nueva recupera la cache desde keyring.
        auth2, apps = self.make(store, {}, email="alice@example.org")
        apps[0].accounts = [{"username": "alice@example.org"}]
        assert auth2.access_token() == "silent-tok"

    def test_errors(self, store: TokenStore) -> None:
        auth, _ = self.make(store, {"error_description": "denegado"})
        with pytest.raises(MailError, match="denegado"):
            auth.login_interactive()
        auth, _ = self.make(store, {"access_token": "a", "id_token_claims": {}})
        with pytest.raises(MailError, match="email"):
            auth.login_interactive()
        auth, _ = self.make(store, {}, email="nadie@example.org")
        with pytest.raises(MailError, match="Sin credenciales"):
            auth.access_token()

    def test_scopes(self) -> None:
        assert MS_SCOPES == ["Mail.ReadWrite", "Mail.Send", "User.Read"]


class FakeFlow:
    def __init__(self, refresh: str | None) -> None:
        self.refresh = refresh
        self.kwargs: dict[str, Any] = {}

    def run_local_server(self, **kw: Any) -> Any:
        self.kwargs = kw
        return gauth.Credentials(token="t", refresh_token=self.refresh, scopes=gauth.SCOPES)  # type: ignore[no-untyped-call]


class TestGoogleAuth:
    def test_scopes_are_minimal(self) -> None:
        assert gauth.SCOPES == [
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.compose",
        ]
        assert all("mail.google.com" not in s for s in gauth.SCOPES)

    def test_login_and_roundtrip(self) -> None:
        flow = FakeFlow("refresh-test")
        seen: dict[str, Any] = {}

        def factory(config: dict[str, Any], scopes: list[str]) -> FakeFlow:
            seen.update(config=config, scopes=scopes)
            return flow

        creds = gauth.run_login("gid", "gsecret", flow_factory=factory, open_browser=False)
        assert seen["config"]["installed"]["client_id"] == "gid"
        assert flow.kwargs["host"] == "127.0.0.1" and flow.kwargs["port"] == 0
        stored = gauth.serialize(creds)
        loaded = gauth.load_credentials(stored, "gid", "gsecret")
        assert loaded.refresh_token == "refresh-test" and loaded.client_id == "gid"
        assert loaded.token_uri == gauth.TOKEN_URI

    def test_login_without_refresh_token(self) -> None:
        with pytest.raises(MailError, match="refresh token"):
            gauth.run_login("gid", "s", flow_factory=lambda c, s: FakeFlow(None))

    def test_default_flow_factory_uses_pkce(self) -> None:
        flow = gauth._default_flow_factory(gauth.client_config("gid", "s"), gauth.SCOPES)
        assert flow.autogenerate_code_verifier is True
