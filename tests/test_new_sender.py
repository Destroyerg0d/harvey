"""An email from someone Harvey never wrote to (2026-11-14: they were dropped with
only a log line). A person's lands on Needs you; a machine's is still left out."""

import pytest

from harvey.agents.handler import NEW_SENDER_SOURCE, automated_sender
from tests.test_auto_replies import handler
from tests.test_outbox_native import InboundMessage, state  # noqa: F401  (state is a fixture)


def mail(sender="ann@newshop.example", subject="Website rebuild?", body="Hi, can you quote a new site?",
         headers=None, provider_id="n1", **kw):
    h = {"from": f"Ann Sample <{sender}>"}
    h.update(headers or {})
    return InboundMessage(provider_id=provider_id, from_email=sender, subject=subject, body=body,
                          message_id=f"<{provider_id}@newshop.example>", thread_ref=f"t-{provider_id}",
                          headers=h, **kw)


@pytest.mark.asyncio
async def test_a_person_who_writes_first_lands_on_needs_you(state):
    h = handler(state, inbound=[mail()])
    assert await h._run_native() is True
    prospect = await state.get_prospect_by_email("ann@newshop.example")
    assert prospect is not None and prospect.status == "replied" and prospect.source == NEW_SENDER_SOURCE
    assert (prospect.first_name, prospect.last_name) == ("Ann", "Sample")
    [convo] = await state.get_conversations_by_status("needs_human")
    assert convo.prospect_id == prospect.id
    [message] = convo.thread
    assert message.sender == "prospect" and message.message_id == "<n1@newshop.example>"
    assert message.thread_ref == "t-n1" and "quote a new site" in message.content
    # Nobody drafts on it: it's yours to answer.
    assert await state.get_outbox(status="pending_review") == []
    assert await state.get_outbox(status="approved") == []


@pytest.mark.asyncio
async def test_their_next_email_joins_the_same_conversation(state):
    h = handler(state, inbound=[mail(), mail(provider_id="n2", subject="Re: Website rebuild?",
                                             body="Also, do you do Shopify?")])
    await h._run_native()
    [convo] = await state.get_conversations_by_status("needs_human")
    assert len(convo.thread) == 2


@pytest.mark.parametrize("message, why", [
    (mail(sender="no-reply@accounts.example"), "automated sender"),
    (mail(sender="alerts-noreply@service.example"), "automated sender"),
    (mail(sender="hello@tool.example", headers={"list-unsubscribe": "<mailto:x@tool.example>"}), "mailing list"),
    (mail(sender="team@tool.example", headers={"precedence": "bulk"}), "bulk mail"),
    (mail(sender="ann@newshop.example", headers={"auto-submitted": "auto-replied"}), "automatic reply"),
    (mail(sender="kim@ours.example"), "our own"),
])
def test_machines_and_our_own_mail_are_left_out(message, why):
    assert why in automated_sender(message, own_domains={"ours.example"})


def test_a_plain_person_is_not_a_machine():
    assert automated_sender(mail(), own_domains={"ours.example"}) == ""
    assert automated_sender(mail(sender="newsroom.lead@shop.example"), own_domains=()) == ""


@pytest.mark.asyncio
async def test_a_newsletter_makes_no_contact(state):
    h = handler(state, inbound=[mail(sender="hello@tool.example",
                                     headers={"list-unsubscribe": "<mailto:x@tool.example>"})])
    await h._run_native()
    assert await state.get_prospect_by_email("hello@tool.example") is None
    assert await state.get_conversations_by_status("needs_human") == []
