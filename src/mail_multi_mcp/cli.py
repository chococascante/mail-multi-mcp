"""CLI `mail-multi-mcp-accounts`: configura clientes OAuth y cuentas."""

from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .accounts.registry import (
    DEFAULT_MS_AUTHORITY,
    AccountRegistry,
    GoogleClient,
    MicrosoftClient,
)
from .auth.token_store import TokenStore, account_key, client_secret_key
from .ids import validate_alias
from .models import Account, AccountMode, MailError, ProviderKind

AUTHORITIES = {
    "consumers": DEFAULT_MS_AUTHORITY,
    "common": "https://login.microsoftonline.com/common",
    "organizations": "https://login.microsoftonline.com/organizations",
}


def _google_login(client_id: str, secret: str) -> tuple[str, str]:  # pragma: no cover - red
    """Devuelve (email, token serializado) tras el OAuth en el navegador."""
    from .auth.google import run_login, serialize
    from .providers.gmail import build_service

    creds = run_login(client_id, secret)
    profile = build_service(creds).users().getProfile(userId="me").execute()
    return str(profile["emailAddress"]), serialize(creds)


def _ms_login(client_id: str, authority: str, store: TokenStore) -> str:  # pragma: no cover - red
    from .auth.microsoft import MicrosoftAuth

    return MicrosoftAuth(client_id, authority, store).login_interactive()


@dataclass
class Deps:
    registry: AccountRegistry = field(default_factory=AccountRegistry)
    store: TokenStore = field(default_factory=TokenStore)
    google_login: Callable[[str, str], tuple[str, str]] = _google_login
    ms_login: Callable[[str, str, TokenStore], str] = _ms_login
    ask: Callable[[str], str] = input
    ask_secret: Callable[[str], str] = getpass.getpass
    out: Callable[[str], Any] = print


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mail-multi-mcp-accounts", description="Gestiona las cuentas de mail-multi-mcp."
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup-google", help="Guarda el client ID/secret de la app OAuth de Google")
    ms = sub.add_parser("setup-microsoft", help="Guarda el client ID de la app de Entra ID")
    ms.add_argument("--tenant", choices=sorted(AUTHORITIES), default="consumers")
    add = sub.add_parser("add", help="Agrega una cuenta (abre el navegador)")
    add.add_argument("--provider", choices=[k.value for k in ProviderKind], required=True)
    add.add_argument("--alias", required=True)
    add.add_argument("--mode", choices=[m.value for m in AccountMode], default="full")
    sub.add_parser("list", help="Lista las cuentas")
    rm = sub.add_parser("remove", help="Elimina una cuenta y su token")
    rm.add_argument("alias")
    sm = sub.add_parser("set-mode", help="Cambia el modo de una cuenta")
    sm.add_argument("alias")
    sm.add_argument("mode", choices=[m.value for m in AccountMode])
    return p


def run(argv: Sequence[str] | None, deps: Deps) -> int:
    args = _parser().parse_args(argv)
    reg, store, out = deps.registry, deps.store, deps.out

    if args.cmd == "setup-google":
        client_id = deps.ask("Google client ID: ").strip()
        secret = deps.ask_secret("Google client secret (no se muestra): ").strip()
        if not client_id or not secret:
            raise MailError("client ID y secret son obligatorios.")
        clients = reg.oauth_clients()
        clients.google = GoogleClient(client_id=client_id)
        reg.save_oauth_clients(clients)
        store.set(client_secret_key("google"), secret)
        out(f"Cliente de Google guardado ({reg.oauth_path}); el secret quedó en el keychain.")
        return 0

    if args.cmd == "setup-microsoft":
        client_id = deps.ask("Microsoft application (client) ID: ").strip()
        if not client_id:
            raise MailError("client ID obligatorio.")
        clients = reg.oauth_clients()
        clients.microsoft = MicrosoftClient(client_id=client_id, authority=AUTHORITIES[args.tenant])
        reg.save_oauth_clients(clients)
        out(f"Cliente de Microsoft guardado ({reg.oauth_path}), authority={args.tenant}.")
        return 0

    if args.cmd == "add":
        alias = validate_alias(args.alias)
        if any(a.alias == alias for a in reg.list()):
            raise MailError(f"Ya existe una cuenta con alias {alias!r}")
        clients = reg.oauth_clients()
        provider = ProviderKind(args.provider)
        if provider is ProviderKind.GMAIL:
            google_secret = store.get(client_secret_key("google"))
            if not clients.google or not google_secret:
                raise MailError("Primero corré `mail-multi-mcp-accounts setup-google`.")
            email, token = deps.google_login(clients.google.client_id, google_secret)
            account = Account(alias=alias, email=email, provider=provider, mode=args.mode)
            reg.add(account)
            store.set(account_key(account.email), token)
        else:
            if not clients.microsoft:
                raise MailError("Primero corré `mail-multi-mcp-accounts setup-microsoft`.")
            email = deps.ms_login(clients.microsoft.client_id, clients.microsoft.authority, store)
            account = Account(alias=alias, email=email, provider=provider, mode=args.mode)
            try:
                reg.add(account)
            except MailError:
                store.delete(account_key(account.email))
                raise
        out(f"Cuenta agregada: {account.alias} <{account.email}> ({provider.value}, {args.mode})")
        return 0

    if args.cmd == "list":
        accounts = reg.list()
        if not accounts:
            out("No hay cuentas. Agregá una con `mail-multi-mcp-accounts add`.")
        for a in accounts:
            cred = "ok" if store.has(account_key(a.email)) else "SIN CREDENCIALES"
            out(f"{a.alias:16} {a.email:32} {a.provider.value:6} {a.mode.value:10} {cred}")
        return 0

    if args.cmd == "remove":
        acc = reg.remove(args.alias)
        store.delete(account_key(acc.email))
        out(f"Cuenta {acc.alias} eliminada (y su token del keychain).")
        return 0

    # set-mode
    acc = reg.set_mode(args.alias, AccountMode(args.mode))
    out(f"{acc.alias}: modo {acc.mode.value}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - entrypoint
    try:
        return run(argv, Deps())
    except (MailError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
