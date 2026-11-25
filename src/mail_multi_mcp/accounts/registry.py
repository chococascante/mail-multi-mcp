"""Registro de cuentas y clientes OAuth (solo metadatos, sin secretos)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..config import config_dir, read_json, write_json
from ..models import Account, AccountMode, MailError, Settings

ACCOUNTS_FILE = "accounts.json"
OAUTH_CLIENTS_FILE = "oauth_clients.json"

DEFAULT_MS_AUTHORITY = "https://login.microsoftonline.com/consumers"


class GoogleClient(BaseModel):
    client_id: str


class MicrosoftClient(BaseModel):
    client_id: str
    authority: str = DEFAULT_MS_AUTHORITY


class OAuthClients(BaseModel):
    google: GoogleClient | None = None
    microsoft: MicrosoftClient | None = None


class _AccountsFile(BaseModel):
    accounts: list[Account] = Field(default_factory=list)
    settings: Settings = Field(default_factory=Settings)


class AccountRegistry:
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or config_dir()

    @property
    def accounts_path(self) -> Path:
        return self.directory / ACCOUNTS_FILE

    @property
    def oauth_path(self) -> Path:
        return self.directory / OAUTH_CLIENTS_FILE

    # --- cuentas -------------------------------------------------------
    def _load(self) -> _AccountsFile:
        return _AccountsFile.model_validate(read_json(self.accounts_path, {}))

    def _save(self, data: _AccountsFile) -> None:
        payload: dict[str, Any] = data.model_dump(mode="json")
        write_json(self.accounts_path, payload)

    def list(self) -> list[Account]:
        return self._load().accounts

    def settings(self) -> Settings:
        return self._load().settings

    def get(self, alias_or_email: str) -> Account:
        key = alias_or_email.strip().lower()
        for acc in self.list():
            if key in (acc.alias, acc.email):
                return acc
        raise MailError(f"Cuenta desconocida: {alias_or_email!r}")

    def add(self, account: Account) -> None:
        data = self._load()
        for acc in data.accounts:
            if acc.alias == account.alias:
                raise MailError(f"Ya existe una cuenta con alias {account.alias!r}")
            if acc.email == account.email:
                raise MailError(f"La cuenta {account.email} ya está registrada como {acc.alias!r}")
        data.accounts.append(account)
        self._save(data)

    def remove(self, alias: str) -> Account:
        data = self._load()
        for i, acc in enumerate(data.accounts):
            if acc.alias == alias:
                del data.accounts[i]
                self._save(data)
                return acc
        raise MailError(f"Cuenta desconocida: {alias!r}")

    def set_mode(self, alias: str, mode: AccountMode) -> Account:
        data = self._load()
        for acc in data.accounts:
            if acc.alias == alias:
                acc.mode = mode
                self._save(data)
                return acc
        raise MailError(f"Cuenta desconocida: {alias!r}")

    # --- clientes OAuth -----------------------------------------------
    def oauth_clients(self) -> OAuthClients:
        return OAuthClients.model_validate(read_json(self.oauth_path, {}))

    def save_oauth_clients(self, clients: OAuthClients) -> None:
        write_json(self.oauth_path, clients.model_dump(mode="json", exclude_none=True))
