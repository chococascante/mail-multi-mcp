from __future__ import annotations

import json
from datetime import date

import httpx
import pytest
import respx

from mail_multi_mcp.models import Account, DraftInput, MailError, MailQuery, ReplyInput
from mail_multi_mcp.providers.graph import GRAPH_BASE, GraphProvider, to_graph_query


def _ea(addr: str, name: str | None = None) -> dict[str, dict[str, str | None]]:
    return {"emailAddress": {"address": addr, "name": name}}


@pytest.fixture
def provider(graph_account: Account) -> GraphProvider:
    return GraphProvider(graph_account, lambda: "tok-test", max_body_chars=500)


class TestQuery:
    def test_search_mode_uses_kql_only(self) -> None:
        gq = to_graph_query(
            MailQuery(
                text='factura "urgente"',
                from_="carol@example.org",
                subject="pago marzo",
                after=date(2026, 1, 1),
                before=date(2026, 2, 1),
                has_attachment=True,
                raw="kind:email",
            ),
            10,
        )
        assert "$filter" not in gq.params and "$orderby" not in gq.params
        assert gq.params["$search"] == (
            '"factura AND urgente AND from:carol@example.org AND subject:pago AND '
            "subject:marzo AND kind:email AND received>=2026-01-01 AND received<2026-02-01 "
            'AND hasAttachments:true"'
        )
        assert gq.params["$top"] == "10" and gq.client_unread is None

    def test_search_mode_unread_is_client_side(self) -> None:
        gq = to_graph_query(MailQuery(to="bob@example.org", unread=True), 20)
        assert gq.client_unread is True and gq.params["$top"] == "60"

    def test_filter_mode(self) -> None:
        gq = to_graph_query(
            MailQuery(
                after=date(2026, 3, 1), before=date(2026, 4, 1), unread=True, has_attachment=False
            ),
            5,
        )
        assert gq.params == {
            "$filter": "receivedDateTime ge 2026-03-01T00:00:00Z and receivedDateTime lt "
            "2026-04-01T00:00:00Z and isRead eq false and hasAttachments eq false",
            "$orderby": "receivedDateTime desc",
            "$top": "10",
        }

    def test_filter_mode_default_has_orderby_property_first(self) -> None:
        gq = to_graph_query(MailQuery(), 20)
        assert gq.params["$filter"].startswith("receivedDateTime ge 1900-01-01")


@respx.mock
def test_search_groups_by_conversation(provider: GraphProvider) -> None:
    route = respx.get(f"{GRAPH_BASE}/me/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": "m1",
                        "conversationId": "c1",
                        "subject": "Hola",
                        "isRead": True,
                        "receivedDateTime": "2026-01-01T10:00:00Z",
                        "from": _ea("carol@example.org", "Carol"),
                        "bodyPreview": "viejo",
                    },
                    {
                        "id": "m2",
                        "conversationId": "c1",
                        "subject": "RE: Hola",
                        "isRead": False,
                        "receivedDateTime": "2026-01-02T10:00:00Z",
                        "from": _ea("dan@example.org"),
                        "bodyPreview": "nuevo",
                        "hasAttachments": True,
                    },
                    {
                        "id": "m3",
                        "conversationId": "c2",
                        "subject": "Otro",
                        "isRead": False,
                        "receivedDateTime": "2025-12-01T10:00:00Z",
                        "from": {},
                    },
                    {
                        "id": "m4",
                        "conversationId": "c3",
                        "subject": "Extra",
                        "isRead": True,
                        "receivedDateTime": "2025-11-01T10:00:00Z",
                    },
                ]
            },
        )
    )
    out = provider.search(MailQuery(subject="hola"), 2)
    req = route.calls.last.request
    assert req.headers["Authorization"] == "Bearer tok-test"
    assert "%24search" in str(req.url) and "%24select" in str(req.url)
    assert [t.id for t in out] == ["outlook:c1", "outlook:c2"]
    c1 = out[0]
    assert c1.subject == "RE: Hola" and c1.snippet == "nuevo" and c1.message_count == 2
    assert c1.unread and c1.has_attachment and c1.from_ is not None
    assert out[1].from_ is None


@respx.mock
def test_search_client_side_unread(provider: GraphProvider) -> None:
    respx.get(f"{GRAPH_BASE}/me/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [
                    {"id": "m1", "conversationId": "c1", "isRead": True},
                    {"id": "m2", "conversationId": "c2", "isRead": False},
                ]
            },
        )
    )
    out = provider.search(MailQuery(text="x", unread=True), 10)
    assert [t.id for t in out] == ["outlook:c2"]


@respx.mock
def test_get_thread_paginates_and_sorts(provider: GraphProvider) -> None:
    next_link = f"{GRAPH_BASE}/me/messages?$skiptoken=abc"
    respx.get(next_link).mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": "m1",
                        "conversationId": "c'1",
                        "subject": "Hola",
                        "isRead": True,
                        "receivedDateTime": "2026-01-01T10:00:00Z",
                        "from": _ea("carol@example.org"),
                        "toRecipients": [_ea("alice@example.org")],
                        "ccRecipients": [],
                        "body": {"contentType": "text", "content": "primero"},
                    },
                ]
            },
        )
    )
    first = respx.get(f"{GRAPH_BASE}/me/messages", params={"$top": "50"}).mock(
        return_value=httpx.Response(
            200,
            json={
                "@odata.nextLink": next_link,
                "value": [
                    {
                        "id": "m2",
                        "conversationId": "c'1",
                        "subject": "RE: Hola",
                        "isRead": False,
                        "receivedDateTime": "2026-01-02T10:00:00Z",
                        "body": {"contentType": "html", "content": "<p>" + "largo " * 200 + "</p>"},
                    },
                ],
            },
        )
    )
    thread = provider.get_thread("c'1")
    req = first.calls.last.request
    assert req.headers["Prefer"] == 'outlook.body-content-type="text"'
    assert "c%27%271" in str(req.url)  # comilla escapada en OData
    assert [m.id for m in thread.messages] == ["outlook:m1", "outlook:m2"]
    assert thread.subject == "Hola" and thread.id == "outlook:c'1"
    assert thread.messages[0].to[0].email == "alice@example.org"
    assert thread.messages[1].body_truncated and thread.messages[1].unread


@respx.mock
def test_get_thread_not_found(provider: GraphProvider) -> None:
    respx.get(f"{GRAPH_BASE}/me/messages").mock(return_value=httpx.Response(200, json={}))
    with pytest.raises(MailError, match="no encontrado"):
        provider.get_thread("c9")


@respx.mock
def test_reply_flow_create_patch_send(provider: GraphProvider) -> None:
    create = respx.post(f"{GRAPH_BASE}/me/messages/m%3D1/createReplyAll").mock(
        return_value=httpx.Response(
            201,
            json={
                "id": "d/1",
                "conversationId": "c1",
                "subject": "RE: Hola",
                "toRecipients": [_ea("carol@example.org")],
                "ccRecipients": [_ea("dan@example.org")],
                "body": {"contentType": "html", "content": "<blockquote>original</blockquote>"},
            },
        )
    )
    patch = respx.patch(f"{GRAPH_BASE}/me/messages/d%2F1").mock(
        return_value=httpx.Response(200, json={"id": "d/1", "conversationId": "c1"})
    )
    send = respx.post(f"{GRAPH_BASE}/me/messages/d%2F1/send").mock(return_value=httpx.Response(202))
    ref = provider.reply("m=1", ReplyInput(body="Hola <Carol>\nGracias", reply_all=True))
    assert create.called and patch.called
    sent_body = json.loads(patch.calls.last.request.content)["body"]
    assert sent_body["contentType"] == "HTML"
    assert sent_body["content"].startswith("<div>Hola &lt;Carol&gt;<br>Gracias</div>")
    assert sent_body["content"].endswith("<blockquote>original</blockquote>")
    assert ref.id == "outlook:d/1" and ref.thread_id == "outlook:c1"
    assert ref.to == ["carol@example.org"] and ref.cc == ["dan@example.org"]

    sent = provider.send_draft("d/1")
    assert send.called and sent.id == "outlook:d/1"


@respx.mock
def test_reply_single_uses_create_reply(provider: GraphProvider) -> None:
    route = respx.post(f"{GRAPH_BASE}/me/messages/m1/createReply").mock(
        return_value=httpx.Response(201, json={"id": "d2"})
    )
    respx.patch(f"{GRAPH_BASE}/me/messages/d2").mock(return_value=httpx.Response(200, json={}))
    ref = provider.reply("m1", ReplyInput(body="ok"))
    assert route.called and ref.thread_id is None


@respx.mock
def test_create_draft(provider: GraphProvider) -> None:
    route = respx.post(f"{GRAPH_BASE}/me/messages").mock(
        return_value=httpx.Response(
            201,
            json={
                "id": "d3",
                "conversationId": "c3",
                "subject": "Hola",
                "toRecipients": [_ea("bob@example.org")],
            },
        )
    )
    ref = provider.create_draft(DraftInput(to=["bob@example.org"], subject="Hola", body="Texto"))
    payload = json.loads(route.calls.last.request.content)
    assert payload["body"] == {"contentType": "Text", "content": "Texto"}
    assert payload["toRecipients"] == [{"emailAddress": {"address": "bob@example.org"}}]
    assert ref.id == "outlook:d3" and ref.preview == "Texto"


@respx.mock
def test_errors_are_mapped(provider: GraphProvider) -> None:
    respx.get(f"{GRAPH_BASE}/me/messages").mock(
        return_value=httpx.Response(
            400, json={"error": {"code": "InefficientFilter", "message": "bad"}}
        )
    )
    with pytest.raises(MailError, match="InefficientFilter: bad"):
        provider.search(MailQuery(), 5)
    respx.post(f"{GRAPH_BASE}/me/messages/x/send").mock(
        return_value=httpx.Response(500, text="boom")
    )
    with pytest.raises(MailError, match="boom"):
        provider.send_draft("x")


def test_default_client(graph_account: Account) -> None:
    p = GraphProvider(graph_account, lambda: "t")
    assert str(p._http.base_url).startswith(GRAPH_BASE)
