"""Resuelve alias/email/"all" a proveedores autenticados (cache por cuenta)."""

from __future__ import annotations

import threading
from collections.abc import Callable

from ..auth.token_store import TokenStore, account_key, client_secret_key
from ..ids import parse_id
from ..models import Account, MailError, ProviderKind, Settings
from ..providers.base import MailProvider
from .registry import AccountRegistry

ProviderFactory = Callable[[Account], MailProvider]

ALL = "all"


def default_provider_factory(
    registry: AccountRegistry, store: TokenStore, settings: Settings
) -> ProviderFactory:  # pragma: no cover - requiere credenciales y red reales
    def factory(account: Account) -> MailProvider:
        clients = registry.oauth_clients()
        if account.provider is ProviderKind.GMAIL:
            from ..auth.google import load_credentials
            from ..providers.gmail import GmailProvider, build_service

            secret = store.get(client_secret_key("google"))
            stored = store.get(account_key(account.email))
            if not clients.google or not secret:
                raise MailError("Falta configurar Google: `mail-multi-mcp-accounts setup-google`.")
            if not stored:
                raise MailError(
                    f"Sin credenciales para {account.alias}; volvé a agregar la cuenta."
                )
            creds = load_credentials(stored, clients.google.client_id, secret)
            return GmailProvider(
                account, build_service(creds), max_body_chars=settings.max_body_chars
            )

        from ..auth.microsoft import MicrosoftAuth
        from ..providers.graph import GraphProvider

        if not clients.microsoft:
            raise MailError(
                "Falta configurar Microsoft: `mail-multi-mcp-accounts setup-microsoft`."
            )
        auth = MicrosoftAuth(
            clients.microsoft.client_id,
            clients.microsoft.authority,
            store,
            email=account.email,
        )
        return GraphProvider(account, auth.access_token, max_body_chars=settings.max_body_chars)

    return factory


class AccountRouter:
    def __init__(
        self,
        registry: AccountRegistry,
        store: TokenStore,
        factory: ProviderFactory,
    ) -> None:
        self.registry = registry
        self.store = store
        self._factory = factory
        self._cache: dict[str, MailProvider] = {}
        self._lock = threading.Lock()

    def accounts(self) -> list[Account]:
        return self.registry.list()

    def resolve(self, selector: str) -> list[Account]:
        if selector.strip().lower() == ALL:
            accounts = self.accounts()
            if not accounts:
                raise MailError("No hay cuentas configuradas.")
            return accounts
        return [self.registry.get(selector)]

    def has_credentials(self, account: Account) -> bool:
        return self.store.has(account_key(account.email))

    def provider(self, account: Account) -> MailProvider:
        with self._lock:
            if account.alias not in self._cache:
                self._cache[account.alias] = self._factory(account)
            return self._cache[account.alias]

    def for_id(self, composite_id: str) -> tuple[Account, MailProvider, str]:
        """La cuenta dueña se deriva del ID: no existe cuenta por defecto."""
        cid = parse_id(composite_id)
        account = self.registry.get(cid.alias)
        return account, self.provider(account), cid.provider_id

    def forget(self, alias: str) -> None:
        with self._lock:
            self._cache.pop(alias, None)
