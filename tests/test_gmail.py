from __future__ import annotations

import base64
import email
from datetime import date
from email import policy
from typing import Any, ClassVar

import pytest

from conftest import FakeGmail, b64, headers
from mail_multi_mcp.models import Account, DraftInput, MailQuery, ReplyInput
from mail_multi_mcp.providers.gmail import (
    GmailProvider,
    compute_reply_recipients,
    extract_body,
    to_gmail_query,
)
from mail_multi_mcp.providers.text import parse_addresses


def decode_raw(body: dict[str, Any]) -> email.message.EmailMessage:
    raw = base64.urlsafe_b64decode(body["message"]["raw"])
    return email.message_from_bytes(raw, policy=policy.default)  # type: ignore[return-value]


class TestQuery:
    def test_full(self) -> None:
        q = MailQuery(
            text="factura",
            from_="Carol Example",
            to="alice@example.com",
            subject="pago (marzo)",
            after=date(2026, 1, 2),
            before=date(2026, 2, 3),
            unread=True,
            has_attachment=True,
            raw="label:inbox",
        )
        assert to_gmail_query(q) == (
            'factura from:"Carol Example" to:alice@example.com subject:(pago  marzo) '
            "after:2026/01/02 before:2026/02/03 is:unread has:attachment (label:inbox)"
        )

    def test_negations_and_empty(self) -> None:
        assert to_gmail_query(MailQuery()) == ""
        assert to_gmail_query(MailQuery(unread=False, has_attachment=False)) == (
            "-is:unread -has:attachment"
        )

    def test_from_alias_accepted(self) -> None:
        assert MailQuery.model_validate({"from": "x@example.com"}).from_ == "x@example.com"


def _meta_thread(tid: str, *, unread: bool, mime: str = "multipart/alternative") -> dict[str, Any]:
    return {
        "id": tid,
        "messages": [
            {
                "id": f"{tid}-1",
                "labelIds": ["INBOX"],
                "internalDate": "1767225600000",
                "payload": {
                    "mimeType": mime,
                    "headers": headers(Subject="Hola", From="a@example.org"),
                },
            },
            {
                "id": f"{tid}-2",
                "labelIds": ["UNREAD"] if unread else [],
                "snippet": "último",
                "internalDate": "1767312000000",
                "payload": {
                    "mimeType": "multipart/mixed",
                    "headers": headers(From="Carol <carol@example.org>"),
                },
            },
        ],
    }


def test_search(fake_gmail: FakeGmail, gmail_account: Account) -> None:
    fake_gmail.list_result = {"threads": [{"id": "t1"}, {"id": "t2"}, {"id": "t3"}]}
    fake_gmail.threads_data = {
        "t1": _meta_thread("t1", unread=True),
        "t2": _meta_thread("t2", unread=False),
    }
    p = GmailProvider(gmail_account, fake_gmail)
    out = p.search(MailQuery(text="hola"), 2)
    assert [t.id for t in out] == ["personal:t1", "personal:t2"]
    first = out[0]
    assert first.subject == "Hola" and first.snippet == "último"
    assert first.from_ is not None and first.from_.email == "carol@example.org"
    assert first.unread and first.has_attachment and first.message_count == 2
    assert first.date is not None and first.date.year == 2026
    assert not out[1].unread
    name, kw = fake_gmail.calls[0]
    assert name == "threads.list" and kw["q"] == "hola" and kw["maxResults"] == 2
    assert fake_gmail.calls[1][1]["format"] == "metadata"


def test_get_thread_and_body(fake_gmail: FakeGmail, gmail_account: Account) -> None:
    payload = {
        "mimeType": "multipart/mixed",
        "headers": headers(
            From="Carol <carol@example.org>",
            To="alice@example.com",
            Cc="dan@example.org",
            Subject="Factura",
        ),
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {
                        "mimeType": "text/plain",
                        "headers": [
                            {"name": "Content-Type", "value": 'text/plain; charset="utf-8"'}
                        ],
                        "body": {"data": b64("Adjunto la factura. " * 20)},
                    },
                    {"mimeType": "text/html", "body": {"data": b64("<p>ignorado</p>")}},
                ],
            },
            {
                "mimeType": "application/pdf",
                "filename": "factura.pdf",
                "body": {"attachmentId": "att1", "size": 1234},
            },
        ],
    }
    fake_gmail.threads_data = {
        "t1": {
            "id": "t1",
            "messages": [
                {
                    "id": "m1",
                    "threadId": "t1",
                    "labelIds": ["UNREAD"],
                    "internalDate": "1767225600000",
                    "payload": payload,
                }
            ],
        }
    }
    thread = GmailProvider(gmail_account, fake_gmail, max_body_chars=200).get_thread("t1")
    assert thread.id == "personal:t1" and thread.subject == "Factura"
    msg = thread.messages[0]
    assert msg.id == "personal:m1" and msg.thread_id == "personal:t1"
    assert msg.body.startswith("Adjunto la factura.") and msg.body_truncated
    assert [a.email for a in msg.cc] == ["dan@example.org"] and msg.unread
    assert msg.attachments[0].filename == "factura.pdf" and msg.attachments[0].size == 1234


def test_extract_body_html_fallback_and_bad_charset() -> None:
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {
                "mimeType": "text/html",
                "headers": [{"name": "Content-Type", "value": "text/html; charset=bogus-xx"}],
                "body": {"data": b64("<p>Hola <b>mundo</b></p>")},
            },
            {"mimeType": "image/png", "filename": "inline.png", "body": {}},
            {"mimeType": "text/plain", "body": {}},
        ],
    }
    body, atts = extract_body(payload)
    assert body == "Hola mundo" and atts == []


def test_empty_thread(fake_gmail: FakeGmail, gmail_account: Account) -> None:
    fake_gmail.threads_data = {"t0": {"id": "t0", "messages": []}}
    assert GmailProvider(gmail_account, fake_gmail).get_thread("t0").subject == ""


class TestReply:
    ORIGINAL: ClassVar[dict[str, Any]] = {
        "id": "m1",
        "threadId": "t1",
        "payload": {
            "headers": headers(
                from_="Carol <carol@example.org>",
                To="alice@example.com, dan@example.org",
                Cc="ALICE@example.com, erin@example.org, dan@example.org",
                Subject="Re: Factura marzo",
                message_id="<orig-2@example.org>",
                References="<orig-0@example.org> <orig-1@example.org>",
            )
        },
    }

    def provider(self, fake: FakeGmail, account: Account) -> GmailProvider:
        fake.messages_data = {"m1": self.ORIGINAL}
        return GmailProvider(account, fake)

    def test_reply_threading_headers(self, fake_gmail: FakeGmail, gmail_account: Account) -> None:
        ref = self.provider(fake_gmail, gmail_account).reply(
            "m1", ReplyInput(body="Gracias\nSaludos")
        )
        body = fake_gmail.created[0]
        assert body["message"]["threadId"] == "t1"
        msg = decode_raw(body)
        assert msg["In-Reply-To"] == "<orig-2@example.org>"
        assert msg["References"] == "<orig-0@example.org> <orig-1@example.org> <orig-2@example.org>"
        assert msg["Subject"] == "Re: Factura marzo"  # sin "Re: Re:"
        assert msg["To"] == "Carol <carol@example.org>"
        assert msg["Cc"] is None
        assert msg["From"] == "alice@example.com"
        assert msg.get_content().strip() == "Gracias\nSaludos"
        assert ref.id == "personal:d1" and ref.thread_id == "personal:t1"
        assert ref.to == ["carol@example.org"] and ref.preview == "Gracias\nSaludos"

    def test_reply_all_excludes_self(self, fake_gmail: FakeGmail, gmail_account: Account) -> None:
        ref = self.provider(fake_gmail, gmail_account).reply(
            "m1", ReplyInput(body="ok", reply_all=True)
        )
        assert ref.to == ["carol@example.org", "dan@example.org"]
        assert ref.cc == ["erin@example.org"]
        msg = decode_raw(fake_gmail.created[0])
        assert "alice@example.com" not in (msg["To"] + msg["Cc"]).lower()

    def test_reply_without_message_id(self, fake_gmail: FakeGmail, gmail_account: Account) -> None:
        fake_gmail.messages_data = {
            "m2": {
                "id": "m2",
                "payload": {"headers": headers(From="z@example.org", Subject="Hola")},
            }
        }
        ref = GmailProvider(gmail_account, fake_gmail).reply("m2", ReplyInput(body="x"))
        msg = decode_raw(fake_gmail.created[0])
        assert msg["In-Reply-To"] is None and msg["Subject"] == "Re: Hola"
        assert ref.thread_id is not None  # el fake asigna uno


@pytest.mark.parametrize(
    ("reply_all", "exp_to", "exp_cc"),
    [
        (False, ["bob@example.org"], []),
        (True, ["bob@example.org", "carol@example.org"], ["dan@example.org"]),
    ],
)
def test_reply_to_own_message(reply_all: bool, exp_to: list[str], exp_cc: list[str]) -> None:
    to, cc = compute_reply_recipients(
        "alice@example.com",
        parse_addresses("alice@example.com"),
        [],
        parse_addresses("bob@example.org, carol@example.org" if reply_all else "bob@example.org"),
        parse_addresses("dan@example.org, alice@example.com"),
        reply_all,
    )
    assert [a.email for a in to] == exp_to and [a.email for a in cc] == exp_cc


def test_reply_to_header_and_self_only() -> None:
    to, _ = compute_reply_recipients(
        "alice@example.com",
        parse_addresses("c@example.org"),
        parse_addresses("lista@example.org"),
        [],
        [],
        False,
    )
    assert [a.email for a in to] == ["lista@example.org"]
    to, _ = compute_reply_recipients(
        "alice@example.com",
        parse_addresses("alice@example.com"),
        [],
        parse_addresses("alice@example.com"),
        [],
        False,
    )
    assert [a.email for a in to] == ["alice@example.com"]


def test_create_and_send(fake_gmail: FakeGmail, gmail_account: Account) -> None:
    p = GmailProvider(gmail_account, fake_gmail)
    ref = p.create_draft(
        DraftInput(to=["bob@example.org"], cc=["carol@example.org"], subject="Hola", body="Cuerpo")
    )
    msg = decode_raw(fake_gmail.created[0])
    assert msg["To"] == "bob@example.org" and msg["Cc"] == "carol@example.org"
    assert "threadId" not in fake_gmail.created[0]["message"]
    assert ref.id == "personal:d1" and ref.thread_id == "personal:t-new"
    sent = p.send_draft("d1")
    assert fake_gmail.sent == ["d1"]
    assert sent.id == "personal:m-sent" and sent.thread_id == "personal:t-sent"


def test_create_draft_without_cc(fake_gmail: FakeGmail, gmail_account: Account) -> None:
    GmailProvider(gmail_account, fake_gmail).create_draft(
        DraftInput(to=["bob@example.org"], subject="s", body="b")
    )
    assert decode_raw(fake_gmail.created[0])["Cc"] is None
