"""Utilidades de texto compartidas: HTML→texto, truncado, direcciones y asunto."""

from __future__ import annotations

import html
import re
from email.utils import getaddresses
from html.parser import HTMLParser

from ..models import Address

_BLOCK_TAGS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"}
_SKIP_TAGS = {"script", "style", "head", "title"}
_RE_PREFIX = re.compile(r"^\s*(re|aw|sv|rv)\s*:", re.IGNORECASE)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    parser = _TextExtractor()
    parser.feed(markup)
    parser.close()
    text = html.unescape("".join(parser.parts)).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit].rstrip() + "\n[…truncado]", True


def preview(text: str, lines: int = 5) -> str:
    return "\n".join(text.strip().splitlines()[:lines])


def parse_addresses(*values: str | None) -> list[Address]:
    pairs = getaddresses([v for v in values if v])
    return [Address(email=addr.lower(), name=name or None) for name, addr in pairs if addr]


def reply_subject(subject: str) -> str:
    subject = subject.strip()
    return subject if _RE_PREFIX.match(subject) else f"Re: {subject}"


def dedupe_excluding(addresses: list[Address], exclude: set[str]) -> list[Address]:
    seen = {e.lower() for e in exclude}
    out: list[Address] = []
    for a in addresses:
        key = a.email.lower()
        if key not in seen:
            seen.add(key)
            out.append(a)
    return out


def format_address(a: Address) -> str:
    from email.utils import formataddr

    return formataddr((a.name or "", a.email))
