"""Modelos normalizados, comunes a todos los proveedores."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .ids import validate_alias


class MailError(Exception):
    """Error esperado (se muestra al modelo/usuario tal cual)."""


class ProviderKind(StrEnum):
    GMAIL = "gmail"
    GRAPH = "graph"


class AccountMode(StrEnum):
    FULL = "full"
    READ_ONLY = "read_only"


class _Model(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    def dump(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


class Account(_Model):
    alias: str
    email: str
    provider: ProviderKind
    mode: AccountMode = AccountMode.FULL

    @field_validator("alias")
    @classmethod
    def _alias(cls, v: str) -> str:
        return validate_alias(v)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip().lower()
        if "@" not in v:
            raise ValueError("email inválido")
        return v


class MailQuery(_Model):
    """Búsqueda estructurada; cada proveedor la traduce a su dialecto."""

    text: str | None = Field(None, description="Texto libre (cuerpo, asunto, etc.)")
    from_: str | None = Field(None, alias="from", description="Remitente (dirección o nombre)")
    to: str | None = Field(None, description="Destinatario")
    subject: str | None = Field(None, description="Palabras en el asunto")
    after: date | None = Field(None, description="Recibido en o después de (YYYY-MM-DD)")
    before: date | None = Field(None, description="Recibido antes de (YYYY-MM-DD)")
    unread: bool | None = Field(None, description="true: solo no leídos; false: solo leídos")
    has_attachment: bool | None = Field(None, description="true: solo con adjuntos")
    raw: str | None = Field(
        None,
        description="Sintaxis nativa del proveedor (Gmail q / KQL de Graph). Se combina con AND.",
    )


class Address(_Model):
    email: str
    name: str | None = None


class AttachmentRef(_Model):
    """Metadatos de un adjunto (la descarga queda para el roadmap)."""

    id: str
    filename: str
    mime_type: str | None = None
    size: int | None = None


class ThreadSummary(_Model):
    id: str = Field(description="ID compuesto del hilo '<alias>:<id>'")
    account: str
    subject: str = ""
    snippet: str = ""
    from_: Address | None = Field(None, alias="from")
    date: datetime | None = None
    unread: bool = False
    has_attachment: bool = False
    message_count: int = 1


class Message(_Model):
    id: str = Field(description="ID compuesto del mensaje '<alias>:<id>'")
    thread_id: str
    from_: Address | None = Field(None, alias="from")
    to: list[Address] = Field(default_factory=list)
    cc: list[Address] = Field(default_factory=list)
    subject: str = ""
    date: datetime | None = None
    unread: bool = False
    body: str = ""
    body_truncated: bool = False
    attachments: list[AttachmentRef] = Field(default_factory=list)


class Thread(_Model):
    id: str
    account: str
    subject: str = ""
    messages: list[Message] = Field(default_factory=list)


class DraftInput(_Model):
    to: list[str]
    subject: str
    body: str
    cc: list[str] = Field(default_factory=list)


class ReplyInput(_Model):
    body: str
    reply_all: bool = False


class DraftRef(_Model):
    id: str = Field(description="ID compuesto del borrador")
    account: str
    thread_id: str | None = None
    to: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    subject: str = ""
    preview: str = ""


class SentRef(_Model):
    id: str
    account: str
    thread_id: str | None = None


class Settings(_Model):
    max_body_chars: int = Field(20_000, ge=200)
    max_sends_per_hour: int = Field(20, ge=0)
    search_concurrency: int = Field(4, ge=1, le=16)
