"""Interfaz común de proveedores de correo.

Convención: los métodos reciben IDs *nativos* del proveedor (el router ya quitó
el alias) y devuelven modelos con IDs *compuestos* ``<alias>:<id>``.

Extensiones previstas (roadmap, no implementadas en el MVP): ``list_labels``,
``modify_labels``, ``move``, ``trash`` y ``download_attachment``. Se agregarán
como Protocols separados para que un proveedor pueda soportarlos parcialmente.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import DraftInput, DraftRef, MailQuery, ReplyInput, SentRef, Thread, ThreadSummary


@runtime_checkable
class MailProvider(Protocol):
    def search(self, q: MailQuery, max_results: int) -> list[ThreadSummary]: ...

    def get_thread(self, thread_id: str) -> Thread: ...

    def create_draft(self, d: DraftInput) -> DraftRef: ...

    def reply(self, message_id: str, r: ReplyInput) -> DraftRef: ...

    def send_draft(self, draft_id: str) -> SentRef: ...


@runtime_checkable
class AttachmentProvider(Protocol):  # pragma: no cover - roadmap
    def download_attachment(self, message_id: str, attachment_id: str) -> bytes: ...
