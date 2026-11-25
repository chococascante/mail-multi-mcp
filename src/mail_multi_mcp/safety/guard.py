"""Reglas de escritura/envío y log de auditoría local (JSONL, sin cuerpos)."""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..models import Account, AccountMode, MailError

WINDOW_SECONDS = 3600


class SafetyGuard:
    def __init__(
        self,
        audit_path: Path,
        *,
        max_sends_per_hour: int = 20,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.audit_path = audit_path
        self.max_sends_per_hour = max_sends_per_hour
        self._clock = clock
        self._lock = threading.Lock()
        self._sends: deque[float] = deque(self._load_recent_sends())

    # -- reglas --------------------------------------------------------------
    def check_write(self, account: Account, action: str) -> None:
        if account.mode is AccountMode.READ_ONLY:
            raise MailError(
                f"La cuenta {account.alias!r} es de solo lectura: '{action}' está bloqueado. "
                "Cambiala con `mail-multi-mcp-accounts set-mode <alias> full`."
            )

    def check_send(self, account: Account) -> None:
        self.check_write(account, "send_draft")
        with self._lock:
            self._prune()
            if len(self._sends) >= self.max_sends_per_hour:
                raise MailError(
                    f"Límite de {self.max_sends_per_hour} envíos por hora alcanzado; "
                    "esperá o ajustá settings.max_sends_per_hour."
                )

    # -- auditoría -----------------------------------------------------------
    def record(
        self,
        action: str,
        account: Account,
        *,
        to: Iterable[str] = (),
        cc: Iterable[str] = (),
        subject: str = "",
        ok: bool,
        ref: str | None = None,
        error: str | None = None,
    ) -> None:
        now = self._clock()
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(now, tz=UTC).isoformat(),
            "epoch": now,
            "action": action,
            "account": account.alias,
            "to": list(to),
            "cc": list(cc),
            "subject": subject,
            "result": "ok" if ok else "error",
        }
        if ref:
            entry["ref"] = ref
        if error:
            entry["error"] = error[:500]
        with self._lock:
            if ok and action == "send_draft":
                self._sends.append(now)
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _prune(self) -> None:
        cutoff = self._clock() - WINDOW_SECONDS
        while self._sends and self._sends[0] < cutoff:
            self._sends.popleft()

    def _load_recent_sends(self) -> list[float]:
        """Recupera los envíos de la última hora para que reiniciar no resetee el límite."""
        if not self.audit_path.exists():
            return []
        cutoff = self._clock() - WINDOW_SECONDS
        out: list[float] = []
        with self.audit_path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("action") == "send_draft" and e.get("result") == "ok":
                    ts = float(e.get("epoch", 0))
                    if ts >= cutoff:
                        out.append(ts)
        return sorted(out)
