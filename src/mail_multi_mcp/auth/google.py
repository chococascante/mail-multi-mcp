"""OAuth de Google para apps de escritorio (loopback 127.0.0.1 + PKCE)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from ..models import MailError

# gmail.compose cubre crear/editar borradores y enviarlos. No pedimos mail.google.com.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]
AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"  # noqa: S105 - URL pública, no secreto


def client_config(client_id: str, client_secret: str) -> dict[str, Any]:
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": AUTH_URI,
            "token_uri": TOKEN_URI,
            "redirect_uris": ["http://127.0.0.1", "http://localhost"],
        }
    }


FlowFactory = Callable[[dict[str, Any], list[str]], Any]


def _default_flow_factory(config: dict[str, Any], scopes: list[str]) -> Any:
    return InstalledAppFlow.from_client_config(config, scopes, autogenerate_code_verifier=True)


def run_login(
    client_id: str,
    client_secret: str,
    *,
    flow_factory: FlowFactory = _default_flow_factory,
    open_browser: bool = True,
) -> Credentials:
    """Abre el navegador; el redirect vuelve a 127.0.0.1 en un puerto aleatorio."""
    flow = flow_factory(client_config(client_id, client_secret), SCOPES)
    creds: Credentials = flow.run_local_server(
        host="127.0.0.1",
        port=0,
        open_browser=open_browser,
        prompt="consent",
        access_type="offline",
        authorization_prompt_message="Abrí esta URL para autorizar: {url}",
        success_message="Listo. Ya podés cerrar esta pestaña.",
    )
    if not creds.refresh_token:
        raise MailError("Google no devolvió refresh token; revocá el acceso y reintentá.")
    return creds


def serialize(creds: Credentials) -> str:
    return json.dumps({"refresh_token": creds.refresh_token, "scopes": list(creds.scopes or [])})


def load_credentials(stored: str, client_id: str, client_secret: str) -> Credentials:
    data = json.loads(stored)
    return Credentials(  # type: ignore[no-untyped-call]
        token=None,
        refresh_token=data["refresh_token"],
        token_uri=TOKEN_URI,
        client_id=client_id,
        client_secret=client_secret,
        scopes=data.get("scopes") or SCOPES,
    )
