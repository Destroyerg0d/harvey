"""Outcome data from now on: each email is stamped with its segment, angle
and subject variant, a reply is tied to the email it answers, and a daily
read-only table counts what came back per kind of email.

All data here is fictional (example.test, .example)."""

import json
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest
import pytest_asyncio

from harvey.models.company import Company
from harvey.models.conversation import Conversation, Message
from harvey.models.prospect import Prospect
from harvey.outcomes import build_outcomes, email_answered, write_outcomes
from harvey.segment_stamp import clean_angle, segment_for_company, variant_for
from harvey.state import StateManager


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(str(tmp_path / "outcomes.db"))
    await s.init_db()
    return s


async def _company(state, domain, **kw):
    return await state.add_company(Company(name=domain.split(".")[0].title(), domain=domain, **kw))


async def _prospect(state, email, company_id="", status="contacted", **kw):
    return await state.add_prospect(Prospect(
        first_name=email.split("@")[0].title(), last_name="Doe", title="Owner",
        company="Shop", company_id=company_id, email=email, email_status="verified",
        email_verified=True, status=status, **kw))


async def _platform(state, company_id, value):
    await state.upsert_signal_code("SITE_PLATFORM", status="confirmed")
    await state.add_observation("SITE_PLATFORM", company_id=company_id, value_text=value)


async def _sent(state, pid, email, step=1, days_ago=1, message_id="", **stamp):
    item_id = await state.add_outbox_item(
        prospect_id=pid, to_email=email, subject="s", body="b",
        send_at=_now().isoformat(), status="approved", campaign_id="c1", step=step)
    sent_at = (_now() - timedelta(days=days_ago)).isoformat()
    await state.update_outbox_item(item_id, status="sent", sent_at=sent_at,
                                   message_id=message_id or f"<{item_id}@mail.example>", **stamp)
    return item_id


# ── segment, variant, angle ──


@pytest.mark.asyncio
@pytest.mark.parametrize("value,segment", [
    ("shopify", "shopify"), ("WordPress", "wordpress"), ("woocommerce", "wordpress"),
    ("osCommerce", "custom"), ("Zen Cart", "custom"), ("custom_php", "custom"),
    ("magento 1", "custom"), ("wix", "other"), ("bigcommerce", "other"),
])
async def test_the_site_platform_signal_decides_first(state, value, segment):
    cid = await _company(state, "shop.example", tech_stack=["Shopify"])
    await _platform(state, cid, value)
    assert await segment_for_company(state, cid) == segment


@pytest.mark.asyncio
async def test_without_the_signal_the_tools_then_the_industry_decide(state):
    shopify = await _company(state, "a.example", tech_stack=["Klaviyo", "Shopify"])
    wordpress = await _company(state, "b.example", tech_stack=["WooCommerce"])
    by_industry = await _company(state, "c.example", industry="Shopify stores")
    nothing = await _company(state, "d.example", industry="Roofing")
    assert await segment_for_company(state, shopify) == "shopify"
    assert await segment_for_company(state, wordpress) == "wordpress"
    assert await segment_for_company(state, by_industry) == "shopify"
    assert await segment_for_company(state, nothing) == "unknown"
    assert await segment_for_company(state, "", Prospect(industry="Shopify brands")) == "shopify"
    assert await segment_for_company(state, "") == "unknown"


@pytest.mark.asyncio
async def test_an_unknown_platform_reads_the_next_source(state):
    cid = await _company(state, "e.example", tech_stack=["WordPress"])
    await _platform(state, cid, "unknown")
    assert await segment_for_company(state, cid) == "wordpress"


def test_the_variant_is_stable_and_splits_people():
    ids = [f"p{n}" for n in range(40)]
    first = [variant_for(i) for i in ids]
    assert first == [variant_for(i) for i in ids]
    assert set(first) == {"a", "b"}


def test_the_angle_is_a_short_label():
    assert clean_angle("Tools detected") == "tools_detected"
    assert clean_angle(None) == "" and clean_angle(42) == ""
    assert len(clean_angle("x" * 200)) == 40


@pytest.mark.asyncio
async def test_the_new_outbox_columns_exist_and_update(state):
    pid = await _prospect(state, "ann@shop.example")
    item = await _sent(state, pid, "ann@shop.example", segment="shopify", angle="tools", variant="b")
    row = await state.get_outbox_item(item)
    assert (row["segment"], row["angle"], row["variant"]) == ("shopify", "tools", "b")


@pytest.mark.asyncio
async def test_the_sender_stamps_the_segment_when_it_sends(state):
    from tests.test_outbox_native import FakeProvider, make_sender, seed_campaign
    cid = await _company(state, "shop.example", tech_stack=["Shopify"])
    pid = await _prospect(state, "ann@shop.example", cid, status="new")
    campaign = await seed_campaign(state, [pid])
    sender = make_sender(state, FakeProvider(), require_approval=False)
    await sender._stage_campaign_native(campaign)
    staged = {r["step"]: r for r in await state.get_outbox()}
    # Staged first emails carry their variant already; nothing has a segment yet.
    assert staged[1]["variant"] == variant_for(pid) and staged[2]["variant"] == ""
    assert {r["segment"] for r in staged.values()} == {""}

    await sender._drain_due()
    sent = await state.get_outbox_item(staged[1]["id"])
    assert sent["status"] == "sent" and sent["segment"] == "shopify"
    assert sent["variant"] == variant_for(pid)


@pytest.mark.asyncio
async def test_a_queued_first_email_without_a_variant_gets_one_at_send(state):
    from tests.test_outbox_native import FakeProvider, make_sender
    pid = await _prospect(state, "bob@shop.example", status="new")
    item = await state.add_outbox_item(
        prospect_id=pid, to_email="bob@shop.example", subject="hello there",
        body="A short, plain note. Question?", send_at=_now().isoformat(),
        status="approved", campaign_id="c1", step=1, approved_by="you")
    await make_sender(state, FakeProvider())._drain_due()
    row = await state.get_outbox_item(item)
    assert row["status"] == "sent"
    assert (row["segment"], row["variant"]) == ("unknown", variant_for(pid))


@pytest.mark.asyncio
async def test_the_writer_keeps_the_angle_it_reports(state):
    from tests.test_writer_feedback import RecordingBrain, prospect, writer
    pid = await prospect(state, "Ana")
    w = writer(RecordingBrain({"subject": "hi", "body": "Hi Ana, who answers the phones?",
                               "angle": "Tools"}), state, "gmail")
    await w._personalize_first_emails("c1", [await state.get_prospect(pid)])
    [row] = await state.get_outbox()
    assert (row["angle"], row["variant"]) == ("tools", variant_for(pid))
    prompt = w.brain.prompts[0]
    assert '"angle"' in prompt


@pytest.mark.asyncio
async def test_a_writer_draft_without_an_angle_still_stages(state):
    from tests.test_writer_feedback import RecordingBrain, prospect, writer
    pid = await prospect(state, "Ben")
    w = writer(RecordingBrain({"subject": "hi", "body": "Hi Ben, who answers the phones?"}),
               state, "gmail")
    await w._personalize_first_emails("c1", [await state.get_prospect(pid)])
    [row] = await state.get_outbox()
    assert row["angle"] == "" and row["variant"] == variant_for(pid)


# ── which email a reply answers ──


@pytest.mark.asyncio
async def test_a_reply_is_tied_to_the_email_its_in_reply_to_names(state):
    pid = await _prospect(state, "ann@shop.example")
    first = await _sent(state, pid, "ann@shop.example", step=1, days_ago=5, message_id="<one@mail.example>")
    await _sent(state, pid, "ann@shop.example", step=2, days_ago=1)
    convo = Conversation(id="c", prospect_id=pid, thread=[
        Message(sender="prospect", content="Tell me more.", in_reply_to="<one@mail.example>")])
    assert (await email_answered(state, convo))["id"] == first


@pytest.mark.asyncio
async def test_without_in_reply_to_the_last_email_before_the_reply_answers(state):
    pid = await _prospect(state, "ann@shop.example")
    await _sent(state, pid, "ann@shop.example", step=1, days_ago=5)
    second = await _sent(state, pid, "ann@shop.example", step=2, days_ago=2)
    convo = Conversation(id="c", prospect_id=pid, thread=[
        Message(sender="prospect", content="Tell me more.", timestamp=_now() - timedelta(days=1))])
    assert (await email_answered(state, convo))["id"] == second
    early = Conversation(id="d", prospect_id=pid, thread=[
        Message(sender="prospect", content="?", timestamp=_now() - timedelta(days=30))])
    assert await email_answered(state, early) is None


# ── the outcome table ──


async def _bounce(state, details):
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute("INSERT INTO actions (id, action_type, agent, details_json) "
                         "VALUES (?, 'bounce', 'handler', ?)", (details["outbox_id"] or "b", json.dumps(details)))
        await db.commit()


@pytest.mark.asyncio
async def test_the_table_counts_what_came_back_per_kind_of_email(state):
    uk = await _company(state, "old.example", location="Leeds, United Kingdom")
    us = await _company(state, "new.example", location="Austin, TX", tech_stack=["Shopify"])
    await _platform(state, uk, "oscommerce")
    ann = await _prospect(state, "ann@old.example", uk)
    bob = await _prospect(state, "bob@old.example", uk)
    cat = await _prospect(state, "cat@new.example", us)
    dan = await _prospect(state, "dan@new.example", us)
    eve = await _prospect(state, "eve@new.example", us, mailbox="two@sender.example")

    a1 = await _sent(state, ann, "ann@old.example", step=1, days_ago=3, segment="custom", angle="age")
    b1 = await _sent(state, bob, "bob@old.example", step=1, days_ago=3, segment="custom", angle="age")
    c1 = await _sent(state, cat, "cat@new.example", step=1, days_ago=3)     # no stamp: derived
    d1 = await _sent(state, dan, "dan@new.example", step=1, days_ago=3)
    await state.update_outbox_item(d1, mailbox="two@sender.example")
    e2 = await _sent(state, eve, "eve@new.example", step=2, days_ago=2)
    old = await _sent(state, eve, "eve@new.example", step=1, days_ago=60)  # outside the window

    now = _now()
    for pid, intent, status, sender, answers in (
            (ann, "interested", "open", "prospect", a1),
            (bob, "auto_ack", "auto", "auto", b1),          # a helpdesk: no human reply
            (cat, "unsubscribe", "closed", "prospect", c1),
            (dan, "not_interested", "auto", "auto", d1),     # a policy auto-reply: still a no
            (eve, "question", "open", "prospect", old)):     # answers the email outside the window
        row = await state.get_outbox_item(answers)
        await state.add_conversation(Conversation(
            id="", prospect_id=pid, intent=intent, status=status,
            thread=[Message(sender=sender, content="...", timestamp=now - timedelta(hours=1),
                            in_reply_to=row["message_id"])]))
    await _bounce(state, {"outbox_id": e2, "prospect": "eve@new.example"})

    rows = {(r["segment"], r["angle"], r["step"], r["mailbox"], r["country"]): r
            for r in await build_outcomes(state, days=30)}
    custom = rows[("custom", "age", 1, "primary", "GB")]
    assert (custom["sent"], custom["human_replies"], custom["positive"]) == (2, 1, 1)
    assert custom["channel"] == "email"
    shop = rows[("shopify", "", 1, "primary", "US")]
    assert (shop["sent"], shop["human_replies"], shop["opt_outs"], shop["positive"]) == (1, 1, 1, 0)
    second_box = rows[("shopify", "", 1, "two@sender.example", "US")]
    assert (second_box["sent"], second_box["human_replies"], second_box["not_interested"]) == (1, 0, 1)
    follow_up = rows[("shopify", "", 2, "two@sender.example", "US")]   # eve's mailbox
    assert (follow_up["sent"], follow_up["human_replies"], follow_up["bounces"]) == (1, 0, 1)
    assert sum(r["sent"] for r in rows.values()) == 5     # the 60-day-old email is left out


@pytest.mark.asyncio
async def test_a_bounce_records_the_email_it_bounced_and_counts_there(state):
    from tests.test_outbox_native import FakeProvider, InboundMessage, make_handler
    pid = await _prospect(state, "ann@shop.example")
    first = await _sent(state, pid, "ann@shop.example", step=1, days_ago=3, message_id="<m1@x.example>")
    await _sent(state, pid, "ann@shop.example", step=2, days_ago=1, message_id="<m2@x.example>")
    provider = FakeProvider()
    provider.inbound = [InboundMessage(
        provider_id="b1", from_email="mailer-daemon@x.example", subject="Delivery Status Notification",
        body="Address not found: ann@shop.example", in_reply_to="<m1@x.example>", is_bounce=True)]
    await make_handler(state, provider)._run_native()
    async with aiosqlite.connect(state.db_path) as db:
        async with db.execute("SELECT details_json FROM actions WHERE action_type = 'bounce'") as cur:
            [(details,)] = await cur.fetchall()
    assert json.loads(details)["outbox_id"] == first
    by_step = {r["step"]: r["bounces"] for r in await build_outcomes(state)}
    assert by_step == {1: 1, 2: 0}


@pytest.mark.asyncio
async def test_an_older_bounce_without_an_email_id_counts_by_address(state):
    pid = await _prospect(state, "ann@shop.example")
    await _sent(state, pid, "ann@shop.example", step=1, days_ago=2)
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute("INSERT INTO actions (id, action_type, agent, details_json) "
                         "VALUES ('x', 'bounce', 'handler', ?)",
                         (json.dumps({"prospect": "ann@shop.example"}),))
        await db.commit()
    [row] = await build_outcomes(state)
    assert row["bounces"] == 1 and row["country"] == "unknown" and row["segment"] == "unknown"


@pytest.mark.asyncio
async def test_the_analyst_writes_the_table_next_to_analytics(state, tmp_path):
    from harvey.agents.analyst import Analyst
    pid = await _prospect(state, "ann@shop.example")
    await _sent(state, pid, "ann@shop.example")
    await Analyst(state).run()
    report = json.loads((tmp_path / "outcomes.json").read_text(encoding="utf-8"))
    assert report["days"] == 30 and report["rows"][0]["sent"] == 1
    assert (tmp_path / "analytics.json").exists()
    assert not (tmp_path / "outcomes.json.tmp").exists()


@pytest.mark.asyncio
async def test_writing_the_table_changes_nothing_in_the_database(state, tmp_path):
    pid = await _prospect(state, "ann@shop.example")
    item = await _sent(state, pid, "ann@shop.example")
    before = await state.get_outbox_item(item)
    path = await write_outcomes(state, path=tmp_path / "o.json")
    assert path.exists()
    assert await state.get_outbox_item(item) == before
