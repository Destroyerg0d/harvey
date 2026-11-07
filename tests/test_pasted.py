"""A client chat pasted into the dashboard: parsed, researched, triaged, emailed."""

import pytest

from harvey import inbound as I
from harvey import pasted as P
from harvey.collectors.erp_chat import ASSISTANT, NOTES, VISITOR
from harvey.models.prospect import Prospect
from tests.test_inbound import TRIAGE, Brain, config, state, verified  # noqa: F401  (fixtures)

INBOX = """Ann SampleUnited Kingdom
Leeds, England, United Kingdom

ann@acme-shop.example

Live Chat
came from
oursite.example/services/shopify/
· opened the chat on
/
· via search.example

read
Translate: On

AI: On
Take over
Resolve
Saved
Today

You

What are you looking to get built?

10:01

Sam Rep
Visitor shared contact details: Ann Sample (ann@acme-shop.example)
?

Ann Sample

We sell candles and want a faster Shopify store.
Open to ideas on the checkout too.

10:02


You

Got it. How many products do you have?

10:02

Sam Rep
?

Ann Sample

About 120.

10:03

Faster ways to reach them
Shared by the visitor in chat

WhatsApp

+44 7700 900123


Open
"""


def test_the_inbox_copy_is_read_as_a_conversation():
    p = P.parse_pasted_chat(INBOX)
    assert p.shape == "inbox" and p.name == "Ann Sample" and p.email == "ann@acme-shop.example"
    assert (p.city, p.country) == ("Leeds", "United Kingdom")
    assert p.came_from == "oursite.example/services/shopify/" and p.via == "search.example"
    assert [line.who for line in p.lines] == [ASSISTANT, VISITOR, ASSISTANT, VISITOR]
    assert p.lines[1].text == "We sell candles and want a faster Shopify store. Open to ideas on the checkout too."
    assert all("Sam Rep" not in line.text and "shared contact" not in line.text for line in p.lines)
    assert p.phone == "+44 7700 900123"


def test_a_name_and_message_chat():
    p = P.parse_pasted_chat("[09:00] Ann: hi, we need a new site\nYou: sure, what kind?\n"
                            "Ann: a booking site\nfor our clinic\nYou: great", visitor="Ann")
    assert p.shape == "named"
    assert [(l.who, l.text) for l in p.lines] == [
        (VISITOR, "hi, we need a new site"), (ASSISTANT, "sure, what kind?"),
        (VISITOR, "a booking site for our clinic"), (ASSISTANT, "great")]


def test_notes_with_labels_stay_notes():
    p = P.parse_pasted_chat("Budget: about five thousand\nTimeline: two months\nWants a WooCommerce store")
    assert p.shape == "notes" and p.lines[0].who == NOTES and "WooCommerce" in p.lines[0].text


def lead_from(text=INBOX, **kw):
    return P.build_lead(P.parse_pasted_chat(text), email=kw.pop("email", "ann@acme-shop.example"),
                        note=kw.pop("note", "Mention we can keep their current theme."), **kw)


@pytest.fixture(autouse=True)
def no_site_reading(monkeypatch):
    async def facts(state, config, company_id, domain):
        facts.asked.append(domain)
        return "SITE_PLATFORM: shopify"
    facts.asked = []
    monkeypatch.setattr(P, "site_facts", facts)
    return facts


@pytest.mark.asyncio
async def test_a_pasted_lead_is_researched_drafted_and_sends_on_its_own(state, no_site_reading):
    brain = Brain()
    result = await P.run_pasted(state, brain, config(), env=None, lead=lead_from())
    assert result["status"] == "drafted" and not result["estimate"]
    item = await state.get_outbox_item(result["outbox_id"])
    assert item["status"] == "approved" and item["to_email"] == "ann@acme-shop.example"
    assert no_site_reading.asked == ["acme-shop.example"]
    prompt = brain.prompts["inbound_email"][0]
    assert "SITE_PLATFORM: shopify" in prompt and "keep their current theme" in prompt
    record = await I.get_record(state, "ann@acme-shop.example")
    assert record["status"] == I.DRAFTED and "pasted" in record["why"]
    prospect = await state.get_prospect_by_email("ann@acme-shop.example")
    assert prospect.source == P.SOURCE


@pytest.mark.asyncio
async def test_an_estimate_still_waits(state):
    brain = Brain(triage={**TRIAGE, "estimate_ready": True})
    result = await P.run_pasted(state, brain, config(price_list=["$7,300", "$900"]), env=None,
                                lead=lead_from())
    assert result["status"] == "drafted" and result["estimate"]
    assert (await state.get_outbox_item(result["outbox_id"]))["status"] == "pending_review"


@pytest.mark.asyncio
async def test_not_a_lead_asks_first_and_can_be_sent_anyway(state):
    brain = Brain(triage={**TRIAGE, "category": "vendor"})
    first = await P.run_pasted(state, brain, config(), env=None, lead=lead_from())
    assert first["status"] == "not_project" and await state.get_outbox(status="approved") == []
    again = await P.run_pasted(state, brain, config(), env=None, lead=lead_from(), send_anyway=True)
    assert again["status"] == "drafted"


@pytest.mark.asyncio
async def test_someone_already_emailed_asks_first(state):
    await state.add_prospect(Prospect(first_name="Ann", email="ann@acme-shop.example",
                                      email_status="verified", status="contacted"))
    result = await P.run_pasted(state, Brain(), config(), env=None, lead=lead_from())
    assert result["status"] == "confirm" and "dealt with them before" in result["why"]
    forced = await P.run_pasted(state, Brain(), config(), env=None, lead=lead_from(), send_anyway=True)
    assert forced["status"] == "drafted"


@pytest.mark.asyncio
@pytest.mark.parametrize("email, change, why", [
    ("ann@blocked.example", None, "do-not-contact"),
    ("ann@acme-shop.example", "opted_out", "opted out"),
])
async def test_what_is_never_sent_even_when_you_say_so(state, email, change, why):
    if change:
        pid = await state.add_prospect(Prospect(first_name="Ann", email=email, email_status="verified"))
        await state.update_prospect_status(pid, change)
    result = await P.run_pasted(state, Brain(), config(), env=None, lead=lead_from(email=email),
                                send_anyway=True)
    assert result["status"] == "refused" and why in result["why"]


@pytest.mark.asyncio
async def test_a_number_joins_the_whatsapp_queue(state):
    cfg = config()
    cfg.channels.whatsapp.number = "+447700900999"
    result = await P.run_pasted(state, Brain(), cfg, env=None, lead=lead_from())
    assert result["whatsapp"].startswith(("queued", "needs_review"))
