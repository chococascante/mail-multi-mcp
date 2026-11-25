"""MSAL (public client) para Microsoft Graph, con la cache serializada en keyring."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import msal

from ..models import MailError
from .token_store import TokenStore, account_key

# MSAL agrega offline_access/openid/profile por su cuenta (y falla si se pasan
# explícitamente), así que offline_access se pide implícitamente.
SCOPES = ["Mail.ReadWrite", "Mail.Send", "User.Read"]

AppFactory = Callable[[str, str, Any], Any]


def _default_app_factory(client_id: str, authority: str, cache: Any) -> Any:
    return msal.PublicClientApplication(client_id, authority=authority, token_cache=cache)


class MicrosoftAuth:
    def __init__(
        self,
        client_id: str,
        authority: str,
        store: TokenStore,
        *,
        email: str | None = None,
        app_factory: AppFactory = _default_app_factory,
    ) -> None:
        self._store = store
        self._email = email.lower() if email else None
        self._cache = msal.SerializableTokenCache()
        if self._email:
            stored = store.get(account_key(self._email))
            if stored:
                self._cache.deserialize(stored)
        self._app = app_factory(client_id, authority, self._cache)
        self._lock = threading.Lock()

    def _persist(self) -> None:
        if self._email and self._cache.has_state_changed:
            self._store.set(account_key(self._email), self._cache.serialize())

    def login_interactive(self) -> str:
        """Abre el navegador (redirect a http://localhost) y devuelve el email de la cuenta."""
        result = self._app.acquire_token_interactive(SCOPES, prompt="select_account")
        if "access_token" not in result:
            raise MailError(f"Login de Microsoft falló: {result.get('error_description', result)}")
        claims = result.get("id_token_claims") or {}
        email = claims.get("preferred_username") or claims.get("email")
        if not email:
            raise MailError("No se pudo determinar el email de la cuenta de Microsoft.")
        self._email = str(email).lower()
        self._persist()
        return self._email

    def access_token(self) -> str:
        with self._lock:
            accounts = self._app.get_accounts(username=self._email) if self._email else []
            result = (
                self._app.acquire_token_silent(SCOPES, account=accounts[0]) if accounts else None
            )
            if not result or "access_token" not in result:
                raise MailError(
                    f"Sin credenciales válidas para {self._email}; corré "
                    "`mail-multi-mcp-accounts add --provider graph` de nuevo."
                )
            self._persist()
            return str(result["access_token"])
