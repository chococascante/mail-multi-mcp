from __future__ import annotations

import pytest

from mail_multi_mcp.ids import CompositeId, InvalidIdError, format_id, parse_id, validate_alias
from mail_multi_mcp.models import Address
from mail_multi_mcp.providers.text import (
    dedupe_excluding,
    format_address,
    html_to_text,
    parse_addresses,
    preview,
    reply_subject,
    truncate,
)


class TestIds:
    def test_roundtrip(self) -> None:
        cid = parse_id("personal:18c9f0a")
        assert cid == CompositeId("personal", "18c9f0a")
        assert str(cid) == "personal:18c9f0a"

    def test_provider_id_may_contain_colons(self) -> None:
        assert parse_id("work:AAMk:x=").provider_id == "AAMk:x="

    @pytest.mark.parametrize(
        "value", ["", "noseparator", ":abc", "personal:", "UPPER:abc", "all:abc", "a b:c"]
    )
    def test_invalid(self, value: str) -> None:
        with pytest.raises(InvalidIdError):
            parse_id(value)

    def test_non_string(self) -> None:
        with pytest.raises(InvalidIdError):
            parse_id(123)  # type: ignore[arg-type]

    def test_format_validates(self) -> None:
        assert format_id("a-1", "x") == "a-1:x"
        with pytest.raises(InvalidIdError):
            format_id("ok", "")
        with pytest.raises(ValueError, match="Alias inválido"):
            format_id("Bad", "x")

    def test_all_reserved(self) -> None:
        with pytest.raises(ValueError, match="reservada"):
            validate_alias("all")


class TestText:
    def test_html_to_text(self) -> None:
        markup = (
            "<html><head><style>p{}</style><title>t</title></head><body>"
            "<p>Hola&nbsp;<b>Alice</b></p><script>evil()</script><div>Línea 2<br>3</div>"
            "</body></html>"
        )
        assert html_to_text(markup) == "Hola Alice\n\nLínea 2\n3"

    def test_truncate(self) -> None:
        assert truncate("abc", 10) == ("abc", False)
        text, cut = truncate("x" * 50, 10)
        assert cut and text.startswith("x" * 10) and text.endswith("[…truncado]")

    def test_preview(self) -> None:
        assert preview("\n1\n2\n3\n4\n5\n6\n", lines=3) == "1\n2\n3"

    @pytest.mark.parametrize(
        ("subject", "expected"),
        [
            ("Factura", "Re: Factura"),
            ("Re: Factura", "Re: Factura"),
            ("RE: Factura", "RE: Factura"),
            ("  re:Factura", "re:Factura"),
            ("AW: Rechnung", "AW: Rechnung"),
            ("Regalo", "Re: Regalo"),
        ],
    )
    def test_reply_subject(self, subject: str, expected: str) -> None:
        assert reply_subject(subject) == expected

    def test_addresses(self) -> None:
        addrs = parse_addresses("Alice <ALICE@example.com>, bob@example.org", None)
        assert addrs == [
            Address(email="alice@example.com", name="Alice"),
            Address(email="bob@example.org"),
        ]
        assert format_address(addrs[0]) == "Alice <alice@example.com>"
        deduped = dedupe_excluding(addrs + addrs, {"BOB@example.org"})
        assert deduped == [addrs[0]]
