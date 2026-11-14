"""Automatic replies don't stop a sequence, and replies from addresses Harvey
doesn't know aren't lost.

Most "replies" to cold email are helpdesk acknowledgements and
out-of-offices. Each one used to mark the prospect replied and cancel both
follow-ups, before its intent was even known; and a reply from any address
Harvey hadn't emailed (a director from their own address, the helpdesk, an
opt-out) was dropped unread. All data here is fictional (.example, .test).
"""

import argparse
from datetime import date, datetime, timedelta, timezone

import aiosqlite
import pytest

from harvey.agents.handler import (
    AUTO_ACK, LEFT_COMPANY, Handler, automatic_reply, referenced_ids, requeue_auto_acks,
    return_date,
)
from harvey.brain import BrainUnavailable
from harvey.models.company import Company
from harvey.models.conversation import Conversation, Message
from harvey.models.prospect import Prospect
from harvey.state import (
    AUTO_CONVERSATION, COLLEAGUE_REPLIED, LEFT_THE_COMPANY, POLICY_AUTO_REPLY, STOP_ON_REPLY,
)
from tests.test_dashboard_signals import client  # noqa: F401  (fixture)
from tests.test_outbox_native import (  # noqa: F401  (state is a fixture)
    Cfg, Env, FakeProvider, InboundMessage, state,
)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Brain:
    """Answers the classifier with ``intent`` and records every call, so a
    test can say the model was never asked."""

    def __init__(self, intent="question", reply="Happy to help. When suits you?"):
        self.intent, self.reply, self.calls = intent, reply, []

    def load_skills_for_agent(self, *_):
        return ""

    def load_prompt(self, *_a, **_k):
        return ""

    async def think(self, prompt, session_id=None, agent="", task=""):
        self.calls.append(task)
        return self.intent if task == "classify_intent" else self.reply


def handler(state, intent="question", inbound=()):
    provider = FakeProvider()
    provider.inbound = list(inbound)
    h = Handler(brain=Brain(intent), state=state, config=Cfg(), env=Env())
    h.provider = provider
    return h


async def person(state, email, company_id="", status="contacted", **kw):
    return await state.add_prospect(Prospect(
        first_name=email.split("@")[0].title(), last_name="Doe", title="Owner",
        company="Shop", company_id=company_id, email=email, email_status="verified",
        email_verified=True, status=status, **kw))


async def company(state, domain="shop.example"):
    return await state.add_company(Company(name="Shop", domain=domain))


async def sent(state, pid, email, message_id="<s1@mail.example>", step=1):
    item = await state.add_outbox_item(
        prospect_id=pid, to_email=email, subject="quick question", body="Hi there.",
        send_at=_now().isoformat(), status="approved", campaign_id="c1", step=step)
    await state.update_outbox_item(item, status="sent", sent_at=_now().isoformat(),
                                   message_id=message_id)
    return item


async def queued(state, pid, email, step=2, days=3, campaign="c1"):
    return await state.add_outbox_item(
        prospect_id=pid, to_email=email, subject="following up", body="Any thoughts?",
        send_at=(_now() + timedelta(days=days)).isoformat(), status="approved",
        campaign_id=campaign, step=step)


def message(sender, body, n=1, subject="Re: quick question", **kw):
    return InboundMessage(provider_id=f"in-{n}", from_email=sender, subject=subject,
                          body=body, message_id=f"<in-{n}@their.example>", **kw)


async def run(state, *messages, intent="question"):
    h = handler(state, intent, messages)
    assert await h._run_native() is True
    return h


async def conversations(state):
    found = []
    for status in ("open", "needs_human", "closed", AUTO_CONVERSATION):
        found += await state.get_conversations_by_status(status)
    return found


async def row(state, item_id):
    return await state.get_outbox_item(item_id)


# ── Replies from addresses Harvey doesn't know ──


@pytest.mark.asyncio
async def test_an_opt_out_from_an_unknown_address_in_the_thread_opts_them_out(state):
    pid = await person(state, "info@shop.example")
    await sent(state, pid, "info@shop.example")
    follow_up = await queued(state, pid, "info@shop.example")
    await run(state, message("director@home.example", "Please remove me from your list.",
                             in_reply_to="<s1@mail.example>"))
    assert (await state.get_prospect(pid)).status == "opted_out"
    assert (await row(state, follow_up))["status"] == "cancelled"
    [convo] = await conversations(state)
    assert convo.thread[-1].from_email == "director@home.example"
    assert convo.thread[-1].in_reply_to == "<s1@mail.example>"


@pytest.mark.asyncio
async def test_the_references_header_matches_too(state):
    pid = await person(state, "info@shop.example")
    await sent(state, pid, "info@shop.example", message_id="<s1@mail.example>")
    await run(state, message("someone@elsewhere.example", "Not interested, sorry.",
                             headers={"references": "<older@x.example> <s1@mail.example>"}),
              intent="not_interested")
    assert (await state.get_prospect(pid)).status == "lost"


@pytest.mark.asyncio
async def test_an_unknown_address_at_the_company_domain_is_that_company(state):
    cid = await company(state)
    pid = await person(state, "info@shop.example", cid)
    await sent(state, pid, "info@shop.example")
    follow_up = await queued(state, pid, "info@shop.example")
    h = await run(state, message("owner@shop.example", "Who are you exactly? What do you charge?"))
    assert (await state.get_prospect(pid)).status == "replied"
    assert (await row(state, follow_up))["status"] == "cancelled"
    [convo] = await conversations(state)
    # Harvey only answers the address it emailed: the owner's answer is yours.
    assert convo.status == "needs_human" and convo.thread[-1].from_email == "owner@shop.example"
    assert "generate_response" not in h.brain.calls
    assert [r for r in await state.get_outbox() if r["kind"] == "reply"] == []


@pytest.mark.asyncio
async def test_the_company_domain_matches_exactly_and_only_someone_emailed(state):
    cid = await company(state, "shop.example")
    pid = await person(state, "info@shop.example", cid)
    await sent(state, pid, "info@shop.example")
    never = await person(state, "ann@other.example")          # never emailed
    await run(state, message("x@mail.shop.example", "Tell me more.", n=1),
              message("y@other.example", "Tell me more.", n=2))
    # Neither is taken for someone Harvey emailed: each is someone new, on Needs you.
    found = await conversations(state)
    assert {c.prospect_id for c in found}.isdisjoint({pid, never})
    assert all(c.status == "needs_human" for c in found)
    assert (await state.get_prospect(pid)).status == "contacted"
    assert (await state.get_prospect(never)).status == "contacted"


@pytest.mark.asyncio
async def test_a_free_mail_domain_is_never_a_company(state):
    pid = await person(state, "jane@gmail.com")
    await sent(state, pid, "jane@gmail.com")
    follow_up = await queued(state, pid, "jane@gmail.com")
    await run(state, message("stranger@gmail.com", "Please remove me from your list."))
    assert (await state.get_prospect(pid)).status == "contacted"
    assert (await row(state, follow_up))["status"] == "approved"
    # The stranger is someone new (on Needs you), never Jane.
    assert all(c.prospect_id != pid and c.status == "needs_human" for c in await conversations(state))


@pytest.mark.asyncio
async def test_harvey_s_own_address_is_never_a_prospect(state):
    pid = await person(state, "info@shop.example")
    await sent(state, pid, "info@shop.example")
    await run(state, message("harvey@x.co", "Please remove me.", in_reply_to="<s1@mail.example>"))
    assert (await state.get_prospect(pid)).status == "contacted"


def test_referenced_ids_are_nearest_first_with_and_without_brackets():
    assert referenced_ids("<a@x>", "<c@x> <b@x>") == [
        "<a@x>", "a@x", "<b@x>", "b@x", "<c@x>", "c@x"]
    assert referenced_ids("", "") == []


# ── Classify before cancelling ──


@pytest.mark.asyncio
async def test_nothing_is_cancelled_while_the_model_is_down(state):
    pid = await person(state, "jane@shop.example")
    follow_up = await queued(state, pid, "jane@shop.example")
    h = handler(state, inbound=[message("jane@shop.example", "Could you send pricing?")])

    async def down(*a, **k):
        raise BrainUnavailable("usage limit")
    h._classify_intent = down
    assert await h._run_native() is False
    assert (await state.get_prospect(pid)).status == "contacted"
    assert (await row(state, follow_up))["status"] == "approved"
    assert not await state.is_reply_processed("in-1")


@pytest.mark.asyncio
async def test_a_classifier_error_still_stops_their_emails(state):
    pid = await person(state, "jane@shop.example")
    follow_up = await queued(state, pid, "jane@shop.example")
    h = handler(state, inbound=[message("jane@shop.example", "Could you send pricing?")])

    async def broken(*a, **k):
        raise RuntimeError("bad output")
    h._classify_intent = broken
    assert await h._run_native() is False
    assert (await state.get_prospect(pid)).status == "replied"
    assert (await row(state, follow_up))["error"] == STOP_ON_REPLY


# ── Strict auto-ack detection ──


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [
    {"auto-submitted": "auto-replied"},
    {"auto-submitted": "auto-generated; owner-email=help@shop.example"},
    {"x-autoreply": "yes"},
    {"x-autorespond": "true"},
    {"precedence": "auto_reply"},
])
async def test_headers_that_say_auto_reply_keep_the_sequence_going(state, headers):
    cid = await company(state)
    pid = await person(state, "jane@shop.example", cid)
    colleague = await person(state, "bob@shop.example", cid)
    follow_up = await queued(state, pid, "jane@shop.example")
    theirs = await queued(state, colleague, "bob@shop.example")
    h = await run(state, message("jane@shop.example", "Thanks, we'll get back to you soon.",
                                 headers=headers))
    assert h.brain.calls == []                                   # never asked the model
    assert (await state.get_prospect(pid)).status == "contacted"
    assert (await row(state, follow_up))["status"] == "approved"
    assert (await row(state, theirs))["status"] == "approved"
    assert await state.colleague_who_replied(colleague) is None
    [convo] = await conversations(state)
    assert (convo.status, convo.intent) == (AUTO_CONVERSATION, AUTO_ACK)
    assert [m.sender for m in convo.thread] == ["auto"]
    assert [r for r in await state.get_outbox() if r["kind"] == "reply"] == []


@pytest.mark.asyncio
async def test_auto_submitted_no_is_a_person(state):
    pid = await person(state, "jane@shop.example")
    h = await run(state, message("jane@shop.example", "Thanks, what would it cost?",
                                 headers={"auto-submitted": "no"}))
    assert "classify_intent" in h.brain.calls
    assert (await state.get_prospect(pid)).status == "replied"


@pytest.mark.parametrize("body", [
    "Your request (#48213) has been received. A member of our team will reply shortly.",
    "Ticket #4521 has been created. We aim to reply within one working day.",
    "Thank you for contacting us. We have received your email. Your reference number is ABC-55120.",
    "This is an automated response to let you know your message arrived.",
    "This message was sent automatically. Please do not reply.",
])
def test_acknowledgement_text_is_automatic_without_headers(body):
    assert automatic_reply({}, "Re: quick question", body) == "ack"


@pytest.mark.parametrize("body", [
    # A helpdesk's footprint is not enough: people answer from the same tools.
    "##- Please type your reply above this line -##\nHi, thanks. Can you send pricing?",
    "Hi, Sam from the support team here. [Ticket ABC-1234] What are you offering exactly?",
    "We have received your email and the owner would like a call.",   # no reference number
    "Thanks for your note. Who else do you work with?",
])
def test_a_helpdesk_marker_alone_goes_to_the_model(body):
    assert automatic_reply({}, "Re: quick question", body) == ""


@pytest.mark.asyncio
async def test_a_helpdesk_agent_s_question_is_answered_as_a_person(state):
    pid = await person(state, "help@shop.example")
    h = await run(state, message(
        "help@shop.example",
        "##- Please type your reply above this line -##\nHi, Sam here. What exactly do you offer?"))
    assert "classify_intent" in h.brain.calls and "generate_response" in h.brain.calls
    assert (await state.get_prospect(pid)).status == "replied"
    assert [r["kind"] for r in await state.get_outbox()] == ["reply"]


@pytest.mark.asyncio
async def test_opt_outs_and_escalations_win_over_auto_reply_rules(state):
    a = await person(state, "a@one.example")
    b = await person(state, "b@two.example")
    await run(state,
              message("a@one.example", "Ticket #881 has been created. To unsubscribe reply STOP.",
                      n=1, headers={"auto-submitted": "auto-replied"}),
              message("b@two.example", "This is an automated response. Our lawyer has been copied.",
                      n=2))
    assert (await state.get_prospect(a)).status == "opted_out"
    by_pid = {c.prospect_id: c for c in await conversations(state)}
    assert by_pid[b].status == "needs_human" and by_pid[b].intent == "escalate"


# ── Policy auto-replies, surveys, closed tickets ──


@pytest.mark.asyncio
@pytest.mark.parametrize("body,headers", [
    ("Thank you for your email. We do not accept unsolicited sales emails.", {}),
    ("Sales and marketing emails will be deleted unread. No solicitation.", {}),
    ("If this is a vendor enquiry, please remove this address from your list.",
     {"auto-submitted": "auto-replied"}),
])
async def test_a_policy_auto_reply_stops_the_company_without_a_reply(state, body, headers):
    cid = await company(state)
    pid = await person(state, "jane@shop.example", cid)
    colleague = await person(state, "bob@shop.example", cid)
    own = await queued(state, pid, "jane@shop.example")
    theirs = await queued(state, colleague, "bob@shop.example")
    h = await run(state, message("jane@shop.example", body, headers=headers), intent="interested")
    assert h.brain.calls == []
    assert (await state.get_prospect(pid)).status == "lost"
    assert ((await row(state, own))["status"], (await row(state, own))["error"]) == (
        "cancelled", POLICY_AUTO_REPLY)
    assert (await row(state, theirs))["status"] == "cancelled"
    assert COLLEAGUE_REPLIED in (await row(state, theirs))["error"]
    [convo] = await conversations(state)
    assert (convo.status, convo.intent, convo.stage) == (
        AUTO_CONVERSATION, "not_interested", "closed_lost")
    assert [r for r in await state.get_outbox() if r["kind"] == "reply"] == []


def test_please_remove_from_a_person_goes_to_the_model():
    assert automatic_reply({}, "Re: hi", "Please remove us, we're not interested.") == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    "How would you rate the support you received? Good / Bad",
    "Your request #5521 has been solved. Reply to reopen it.",
    "Ticket 7781 was closed. Take our customer satisfaction survey.",
])
async def test_a_survey_or_a_closed_ticket_is_never_answered(state, body):
    pid = await person(state, "help@shop.example")
    follow_up = await queued(state, pid, "help@shop.example")
    h = await run(state, message("help@shop.example", body), intent="interested")
    assert h.brain.calls == []
    assert await state.get_outbox(status="pending_review") == []
    assert (await row(state, follow_up))["status"] == "approved"
    [convo] = await conversations(state)
    assert (convo.status, convo.intent) == (AUTO_CONVERSATION, AUTO_ACK)


@pytest.mark.asyncio
async def test_an_auto_conversation_is_never_answered(state):
    h = handler(state)
    convo = Conversation(id="x", prospect_id="p", status=AUTO_CONVERSATION,
                         thread=[Message(sender="auto", content="Ticket #1 has been created.")])
    assert await h._generate_response("question", "?", Prospect(email="a@b.example"), convo) == ""
    assert h.brain.calls == []


# ── Out of office ──


def _day(d: date) -> str:
    return f"{d.day} {d.strftime('%B %Y')}"


@pytest.mark.asyncio
async def test_an_out_of_office_with_a_return_date_moves_the_next_email(state):
    pid = await person(state, "jane@shop.example")
    follow_up = await queued(state, pid, "jane@shop.example", days=2)
    last = await queued(state, pid, "jane@shop.example", step=3, days=6)
    before = datetime.fromisoformat((await row(state, follow_up))["send_at"])
    back = _now().date() + timedelta(days=10)
    h = await run(state, message(
        "jane@shop.example",
        f"Thank you for your email. I am out of the office until {_day(back)} "
        "with limited access to email. For urgent matters please contact sam@shop.example."))
    assert h.brain.calls == []
    moved = datetime.fromisoformat((await row(state, follow_up))["send_at"])
    assert moved.date() == back + timedelta(days=1) and moved.time() == before.time()
    assert (await row(state, follow_up))["status"] == "approved"
    assert (await row(state, last))["status"] == "approved"     # waits for step 2 as always
    assert (await state.get_prospect(pid)).status == "contacted"
    [convo] = await conversations(state)
    assert (convo.status, convo.intent) == (AUTO_CONVERSATION, "ooo")
    # "For urgent matters contact X" is not a referral: nobody new, nobody answered.
    assert await state.get_prospect_by_email("sam@shop.example") is None
    assert [r for r in await state.get_outbox() if r["kind"] == "reply"] == []


@pytest.mark.asyncio
async def test_a_far_return_date_moves_the_email_at_most_three_weeks(state):
    pid = await person(state, "jane@shop.example")
    follow_up = await queued(state, pid, "jane@shop.example", days=2)
    back = _now().date() + timedelta(days=60)
    await run(state, message("jane@shop.example", f"I am on maternity leave until {_day(back)}.",
                             headers={"auto-submitted": "auto-replied"}))
    moved = datetime.fromisoformat((await row(state, follow_up))["send_at"])
    assert timedelta(days=20) < moved - _now() <= timedelta(days=21)


@pytest.mark.asyncio
async def test_an_out_of_office_without_a_date_leaves_the_schedule_alone(state):
    pid = await person(state, "jane@shop.example")
    follow_up = await queued(state, pid, "jane@shop.example", days=2)
    before = (await row(state, follow_up))["send_at"]
    await run(state, message("jane@shop.example", "I'm away at the moment.",
                             subject="Out of office: quick question"))
    assert (await row(state, follow_up))["send_at"] == before
    assert (await row(state, follow_up))["status"] == "approved"


@pytest.mark.asyncio
async def test_the_model_s_ooo_keeps_the_sequence_going_too(state):
    pid = await person(state, "jane@shop.example")
    follow_up = await queued(state, pid, "jane@shop.example")
    h = await run(state, message("jane@shop.example", "Away for a bit, sorry."), intent="ooo")
    assert "classify_intent" in h.brain.calls
    assert (await row(state, follow_up))["status"] == "approved"
    assert (await state.get_prospect(pid)).status == "contacted"


def test_a_person_mentioning_their_holiday_still_reaches_the_model():
    # No boilerplate and a question: they may be keen, so the model reads it.
    assert automatic_reply({}, "Re: hi", "I'm on holiday until Monday, can we talk then?") == ""


@pytest.mark.parametrize("text,expected", [
    ("I am away until 12 November 2026.", date(2026, 11, 12)),
    ("Back on Nov 12th.", date(2026, 11, 12)),
    ("I'm away from 6 November to 14 November.", date(2026, 11, 14)),
    ("Returning 2026-11-20.", date(2026, 11, 20)),
    ("Out until 10/11.", date(2026, 11, 10)),          # 10 November comes before October 11
    ("Back in the office on Monday.", date(2026, 11, 9)),
    ("Closed until 2 January.", date(2027, 1, 2)),
    ("I am out of the office.", None),
    ("I was away on 1 November.", None),
])
def test_return_dates(text, expected):
    assert return_date(text, date(2026, 11, 4)) == expected   # a Wednesday


# ── Someone who has left ──


@pytest.mark.asyncio
@pytest.mark.parametrize("body,headers", [
    ("Ann is no longer with Shop. Please contact Bob instead.", {"auto-submitted": "auto-replied"}),
    ("I have left the company. This mailbox is not monitored.", {}),
    ("This email address is no longer in use.", {}),
])
async def test_someone_who_has_left_stops_only_their_address(state, body, headers):
    cid = await company(state)
    ann = await person(state, "ann@shop.example", cid)
    bob = await person(state, "bob@shop.example", cid)
    hers = await queued(state, ann, "ann@shop.example")
    his = await queued(state, bob, "bob@shop.example")
    await run(state, message("ann@shop.example", body, headers=headers))
    assert ((await row(state, hers))["status"], (await row(state, hers))["error"]) == (
        "cancelled", LEFT_THE_COMPANY)
    assert (await state.get_prospect(ann)).email_status == "invalid"
    assert (await row(state, his))["status"] == "approved"
    assert await state.colleague_who_replied(bob) is None
    [convo] = await conversations(state)
    assert (convo.status, convo.intent) == (AUTO_CONVERSATION, LEFT_COMPANY)


@pytest.mark.asyncio
async def test_another_address_s_no_longer_monitored_is_about_that_address(state):
    pid = await person(state, "ann@shop.example")
    await sent(state, pid, "ann@shop.example")
    hers = await queued(state, pid, "ann@shop.example")
    await run(state, message("old-desk@helpdesk.example", "This mailbox is no longer monitored.",
                             in_reply_to="<s1@mail.example>",
                             headers={"auto-submitted": "auto-generated"}))
    assert (await state.get_prospect(pid)).email_status == "verified"
    assert (await row(state, hers))["status"] == "approved"


# ── A person writing after an auto-reply ──


@pytest.mark.asyncio
async def test_a_person_writing_after_an_auto_reply_is_handled_normally(state):
    pid = await person(state, "jane@shop.example")
    follow_up = await queued(state, pid, "jane@shop.example")
    h = handler(state, "interested", [message("jane@shop.example", "Ticket #3301 has been created.",
                                              n=1)])
    await h._run_native()
    h.provider.inbound = [message("jane@shop.example", "Jane here, the owner. Tell me more.", n=2)]
    await h._run_native()
    [convo] = await conversations(state)
    assert convo.status == "open" and convo.intent == "interested"
    assert [m.sender for m in convo.thread] == ["auto", "prospect"]
    assert (await row(state, follow_up))["status"] == "cancelled"
    assert (await state.get_prospect(pid)).status == "replied"
    assert [r["kind"] for r in await state.get_outbox(status="pending_review")] == ["reply"]


@pytest.mark.asyncio
async def test_an_auto_reply_in_a_person_s_thread_changes_nothing(state):
    pid = await person(state, "jane@shop.example")
    h = handler(state, "interested", [message("jane@shop.example", "Tell me more.", n=1)])
    await h._run_native()
    h.provider.inbound = [message("jane@shop.example", "I'm away.", n=2,
                                  headers={"auto-submitted": "auto-replied"})]
    await h._run_native()
    [convo] = await conversations(state)
    assert (convo.status, convo.intent) == ("open", "interested")
    assert [m.sender for m in convo.thread] == ["prospect", "auto"]
    assert (await state.get_prospect(pid)).status == "replied"


# ── Counts leave automatic replies out ──


@pytest.mark.asyncio
async def test_counts_leave_auto_conversations_out(state):
    pid = await person(state, "jane@shop.example")
    other = await person(state, "ann@two.example")
    await run(state, message("jane@shop.example", "Ticket #5501 has been created.", n=1),
              message("ann@two.example", "Sounds good, tell me more.", n=2), intent="interested")
    summary = await state.get_state_summary()
    assert (summary["open_conversations"], summary["auto_reply_handoffs"],
            summary["auto_conversations"]) == (1, 0, 1)
    assert await state.get_intent_distribution() == {"interested": 1}
    assert (await state.get_prospect(pid)).status == "contacted"
    assert (await state.get_prospect(other)).status == "replied"


def test_the_replies_sparkline_leaves_auto_conversations_out(client):
    import asyncio
    import sqlite3
    from harvey.state import StateManager
    asyncio.run(StateManager(str(client.db_path)).init_db())
    today = datetime.now(timezone.utc).date().isoformat() + "T10:00:00"
    with sqlite3.connect(client.db_path) as conn:
        for cid, status in (("c1", "open"), ("c2", AUTO_CONVERSATION), ("c3", "needs_human")):
            conn.execute("INSERT INTO conversations (id, prospect_id, status, created_at) "
                         "VALUES (?, 'p', ?, ?)", (cid, status, today))
    conn.close()
    assert client.get("/api/trend?days=3").json()["replies"][-1] == 2


# ── Backfill: harvey replies requeue-auto-acks ──


async def _legacy(state, email, intent="ooo", body="Ticket #1201 has been created.",
                  status="replied", answers=0):
    """What the old Handler left behind: 'replied', both follow-ups cancelled
    (stop_on_reply), the auto-reply handed to you."""
    pid = await person(state, email, status=status)
    await sent(state, pid, email, message_id=f"<{email}>")
    steps = [await queued(state, pid, email, step=2, days=3),
             await queued(state, pid, email, step=3, days=7)]
    await state.cancel_pending_outbox_for_prospect(pid)
    await state.add_conversation(Conversation(
        id="", prospect_id=pid, intent=intent, status="needs_human",
        thread=[Message(sender="prospect", content=body)]))
    for n in range(answers):
        await state.add_outbox_item(prospect_id=pid, to_email=email, subject="Re: hi",
                                    body="Sure.", send_at=_now().isoformat(), kind="reply",
                                    status="sent")
    return pid, steps


@pytest.mark.asyncio
async def test_requeue_auto_acks_puts_their_follow_ups_back(state):
    pid, (two, three) = await _legacy(state, "jane@shop.example")
    ooo, _ = await _legacy(state, "ann@two.example", intent="ooo",
                           body="I am out of the office until next week.")
    human, _ = await _legacy(state, "bob@three.example", intent="interested", body="Tell me more")
    answered, _ = await _legacy(state, "cat@four.example", answers=1)
    policy, _ = await _legacy(state, "dan@five.example",
                              body="We do not accept unsolicited sales emails.")
    opted, _ = await _legacy(state, "eve@six.example", status="opted_out")

    dry = await requeue_auto_acks(state, dry_run=True)
    assert (dry["found"], dry["reset_to_contacted"], dry["follow_ups_requeued"],
            dry["conversations_marked_auto"]) == (5, 2, 4, 2)
    assert dry["skipped"] == {"Harvey answered them": 1, "their auto-reply was a stop": 1,
                              "status opted_out": 1}
    assert (await state.get_prospect(pid)).status == "replied"            # nothing changed
    assert (await row(state, two))["status"] == "cancelled"

    done = await requeue_auto_acks(state)
    assert done == dry
    assert (await state.get_prospect(pid)).status == "contacted"
    assert (await state.get_prospect(ooo)).status == "contacted"
    for untouched in (human, answered, policy):
        assert (await state.get_prospect(untouched)).status == "replied"
    a, b = await row(state, two), await row(state, three)
    assert (a["status"], a["error"], b["status"]) == ("approved", "", "approved")
    first, second = datetime.fromisoformat(a["send_at"]), datetime.fromisoformat(b["send_at"])
    assert timedelta(hours=23) < first - _now() <= timedelta(days=1)
    assert timedelta(days=3, hours=23) < second - first < timedelta(days=4, hours=1)
    convo = next(c for c in await conversations(state) if c.prospect_id == pid)
    assert convo.status == AUTO_CONVERSATION and [m.sender for m in convo.thread] == ["auto"]
    # Run twice: nothing more to do.
    again = await requeue_auto_acks(state)
    assert (again["reset_to_contacted"], again["follow_ups_requeued"],
            again["conversations_marked_auto"]) == (0, 0, 0)


@pytest.mark.asyncio
async def test_the_requeue_command_prints_counts(state, monkeypatch, capsys):
    import harvey.state as state_mod
    from harvey.cli import cmd_replies
    await _legacy(state, "jane@shop.example")
    monkeypatch.setattr(state_mod, "DB_PATH", state.db_path)

    import asyncio
    # cmd_replies runs its own event loop, so off this one.
    await asyncio.to_thread(
        cmd_replies, argparse.Namespace(replies_action="requeue-auto-acks", dry_run=True))
    out = capsys.readouterr().out
    assert "Would set back to contacted:        1" in out and "Dry run" in out
    async with aiosqlite.connect(state.db_path) as db:
        async with db.execute("SELECT status FROM prospects") as cursor:
            assert [r[0] for r in await cursor.fetchall()] == ["replied"]
    await asyncio.to_thread(
        cmd_replies, argparse.Namespace(replies_action="requeue-auto-acks", dry_run=False))
    assert "Did re-queue follow-ups:          2" in capsys.readouterr().out
    assert [a for a in await _actions(state) if a == "requeue_auto_acks"]


async def _actions(state):
    async with aiosqlite.connect(state.db_path) as db:
        async with db.execute("SELECT action_type FROM actions") as cursor:
            return [r[0] for r in await cursor.fetchall()]


def test_the_cli_knows_the_command(monkeypatch):
    import sys
    from harvey import cli
    seen = {}
    monkeypatch.setattr(cli, "cmd_replies", lambda args: seen.update(vars(args)))
    monkeypatch.setattr(sys, "argv", ["harvey", "replies", "requeue-auto-acks", "--dry-run"])
    cli.main()
    assert seen["replies_action"] == "requeue-auto-acks" and seen["dry_run"] is True
