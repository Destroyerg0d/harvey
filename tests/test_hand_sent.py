"""Email you or a coworker writes by hand from one of Harvey's mailboxes
(owner, 2026-11-14): it shows in the thread, Harvey steps back, and after a
hand back Harvey answers with your words as context. All data is fictional."""

import base64
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from harvey import hand_sent
from harvey.integrations.gmail import GmailProvider
from harvey.integrations.mail_provider import InboundMessage
from harvey.models.conversation import Conversation, Message
from harvey.state import MANUAL_SEND_KEY, WITH_YOU
from tests.test_auto_replies import Brain, handler, person, sent
from tests.test_outbox_native import state  # noqa: F401  (fixture)

BOX = "sam@studio.example"


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# Your email is written after the drafts each test queues: the scan runs on a
# clock two hours ahead, and the email is "sent" 30 minutes from now.
LATER = timedelta(hours=2)


def mine(gid, to="ann@shop.example", body="Hi Ann, Thursday at 3pm works. I'll send an invite.",
         in_reply_to="", thread="", message_id=None, minutes_ago=-30, references=""):
    when = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return InboundMessage(provider_id=gid, from_email=BOX, subject="Re: quick question", body=body,
                          message_id=message_id if message_id is not None else f"<{gid}@studio.example>",
                          in_reply_to=in_reply_to, thread_ref=thread, date=format_datetime(when),
                          headers={"to": to, "references": references}, mailbox=BOX)


class SentBox:
    """A mailbox whose Sent folder holds ``messages``."""

    def __init__(self, messages):
        self.messages = list(messages)
        self.asked_with = []

    def is_configured(self):
        return True

    async def get_sent(self, days=3, known=frozenset()):
        self.asked_with.append(set(known))
        return [m for m in self.messages if m.provider_id not in known]


async def needs_you(state, pid):
    return await state.add_conversation(Conversation(
        id="", prospect_id=pid, channel="email", status="needs_human", intent="question",
        thread=[Message(sender="prospect", content="Can we talk next week?",
                        message_id="<their1@shop.example>", thread_ref="t-1",
                        timestamp=_now() - timedelta(hours=2))]))


async def scan(state, *messages):
    return await hand_sent.scan(state, [(BOX, SentBox(messages))], own_addresses={BOX},
                                now=_now() + LATER)


@pytest.mark.asyncio
async def test_your_reply_on_a_needs_you_thread_shows_and_harvey_steps_back(state):
    pid = await person(state, "ann@shop.example")
    convo_id = await needs_you(state, pid)
    draft = await state.add_outbox_item(prospect_id=pid, conversation_id=convo_id, kind="reply",
                                        to_email="ann@shop.example", subject="Re: q", body="Harvey's",
                                        send_at=_now().isoformat(), status="pending_review")
    yours = await state.add_outbox_item(prospect_id=pid, conversation_id=convo_id, kind="reply",
                                        to_email="ann@shop.example", subject="Re: q", body="Mine",
                                        send_at=_now().isoformat(), status="approved", approved_by="you")
    counts = await scan(state, mine("g1", in_reply_to="<their1@shop.example>"))
    assert counts["hand"] == 1
    convo = await state.get_conversation(convo_id)
    assert convo.status == WITH_YOU
    last = convo.thread[-1]
    assert last.sender == "you" and last.from_email == BOX and "Thursday at 3pm" in last.content
    assert (await state.get_outbox_item(draft))["status"] == "cancelled"
    assert (await state.get_outbox_item(yours))["status"] == "approved"   # yours stays
    assert await state.get_setting(MANUAL_SEND_KEY + pid)


@pytest.mark.asyncio
async def test_harveys_own_sends_are_not_yours(state):
    pid = await person(state, "ann@shop.example")
    await sent(state, pid, "ann@shop.example", message_id="<h1@studio.example>")
    counts = await scan(state, mine("g2", message_id="<h1@studio.example>"))
    assert counts == {"harvey": 1, "hand": 0, "other": 0, "unreadable": 0}
    assert await state.get_conversations_by_status(WITH_YOU) == []


@pytest.mark.asyncio
async def test_a_fresh_email_to_a_contact_stops_their_sequence(state):
    pid = await person(state, "ann@shop.example")
    await sent(state, pid, "ann@shop.example")
    queued = await state.add_outbox_item(prospect_id=pid, to_email="ann@shop.example", subject="s",
                                         body="Email 2", send_at=_now().isoformat(), status="approved",
                                         campaign_id="c1", step=2)
    await scan(state, mine("g3", to="Ann Sample <ann@shop.example>"))
    [convo] = await state.get_conversations_by_status(WITH_YOU)
    assert convo.prospect_id == pid and convo.thread[0].sender == "you"
    assert (await state.get_outbox_item(queued))["status"] == "cancelled"


@pytest.mark.asyncio
async def test_mail_to_strangers_is_left_alone_and_never_fetched_again(state):
    box = SentBox([mine("g4", to="friend@elsewhere.example")])
    await hand_sent.scan(state, [(BOX, box)], own_addresses={BOX}, now=_now() + LATER)
    await hand_sent.scan(state, [(BOX, box)], own_addresses={BOX}, now=_now() + LATER)
    assert "g4" in box.asked_with[1]
    assert await state.get_conversations_by_status(WITH_YOU) == []


@pytest.mark.asyncio
async def test_a_message_this_new_waits_a_cycle(state):
    await person(state, "ann@shop.example")
    counts = await hand_sent.scan(state, [(BOX, SentBox([mine("g5", minutes_ago=1)]))],
                                  own_addresses={BOX})
    assert counts == {"harvey": 0, "hand": 0, "other": 0, "unreadable": 0}
    assert "g5" not in await hand_sent.seen_ids(state, BOX)


@pytest.mark.asyncio
async def test_an_email_read_after_the_thread_moved_on_is_only_added(state):
    pid = await person(state, "ann@shop.example")
    convo_id = await state.add_conversation(Conversation(
        id="", prospect_id=pid, channel="email", status="open", intent="question",
        thread=[Message(sender="prospect", content="Thanks, see you then",
                        timestamp=_now() + timedelta(hours=1))]))
    draft = await state.add_outbox_item(prospect_id=pid, conversation_id=convo_id, kind="reply",
                                        to_email="ann@shop.example", subject="Re: q", body="Harvey's",
                                        send_at=_now().isoformat(), status="pending_review")
    await scan(state, mine("g8"))                    # your email: 30 minutes from now
    convo = await state.get_conversation(convo_id)
    assert convo.status == "open" and [m.sender for m in convo.thread] == ["you", "prospect"]
    assert (await state.get_outbox_item(draft))["status"] == "pending_review"


@pytest.mark.asyncio
async def test_the_same_email_is_added_once(state):
    pid = await person(state, "ann@shop.example")
    convo_id = await needs_you(state, pid)
    msg = mine("g6", in_reply_to="<their1@shop.example>")
    prospect = await state.get_prospect(pid)
    await hand_sent.record(state, prospect, msg, BOX)
    again = await hand_sent.record(state, prospect, msg, BOX)
    assert again["added"] is False
    assert len((await state.get_conversation(convo_id)).thread) == 2


@pytest.mark.asyncio
async def test_their_answer_comes_to_you_and_a_hand_back_gives_harvey_the_context(state):
    from tests.test_outbox_native import InboundMessage as Inbound
    pid = await person(state, "ann@shop.example")
    convo_id = await needs_you(state, pid)
    await scan(state, mine("g7", in_reply_to="<their1@shop.example>"))

    def reply(n, body):
        return Inbound(provider_id=f"r{n}", from_email="ann@shop.example", subject="Re: quick question",
                       body=body, message_id=f"<r{n}@shop.example>", thread_ref="t-1",
                       headers={"from": "Ann <ann@shop.example>"})
    h = handler(state, "question", [reply(1, "Thursday is good. Who will join?")])
    await h._run_native()
    convo = await state.get_conversation(convo_id)
    assert convo.status == "needs_human"                          # their answer is yours
    assert await state.get_outbox(status="pending_review") == []  # nobody drafted
    await state.update_conversation(convo_id, status="open")      # Hand back to Harvey
    brain = Brain("question", reply="Sam and a developer will join on Thursday.")
    prompts = []
    original = brain.think

    async def think(prompt, session_id=None, agent="", task=""):
        prompts.append((task, prompt))
        return await original(prompt, session_id, agent, task)
    brain.think = think
    h = handler(state, "question", [reply(2, "Great, and how long is the call?")])
    h.brain = brain
    await h._run_native()
    [draft] = await state.get_outbox(status="pending_review")
    assert draft["conversation_id"] == convo_id
    [prompt] = [p for t, p in prompts if t == "generate_response"]
    assert "the owner, writing for us" in prompt and "Thursday at 3pm works" in prompt


# ── Gmail's Sent folder ──

def raw(gid, labels=("SENT",)):
    return {"id": gid, "threadId": "t9", "labelIds": list(labels), "payload": {
        "mimeType": "text/plain",
        "headers": [{"name": "From", "value": BOX}, {"name": "To", "value": "ann@shop.example"},
                    {"name": "Message-ID", "value": f"<{gid}@studio.example>"}],
        "body": {"data": base64.urlsafe_b64encode(b"Hello Ann").decode()}}}


@pytest.mark.asyncio
async def test_gmail_lists_sent_mail_and_skips_what_it_knows():
    p = GmailProvider(SimpleNamespace(), SimpleNamespace())
    p._token = AsyncMock(return_value="synthetic-token")
    p._check_identity = AsyncMock(return_value=True)
    p._request = AsyncMock(side_effect=[
        {"messages": [{"id": "s1"}, {"id": "s2"}, {"id": "s3"}]}, raw("s2"), raw("s3", ("SENT", "DRAFT"))])
    got = await p.get_sent(3, known={"s1"})
    assert [m.provider_id for m in got] == ["s2"]
    assert got[0].headers["to"] == "ann@shop.example" and got[0].thread_ref == "t9"
    assert p._request.call_args_list[0].kwargs["params"]["q"] == "in:sent newer_than:3d"
