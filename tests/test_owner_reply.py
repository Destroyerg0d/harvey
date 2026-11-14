"""An email you write on the dashboard goes out on the conversation's own thread."""

import os
import tempfile

import aiosqlite
import pytest
import pytest_asyncio

from harvey.models.conversation import Conversation, Message
from harvey.models.prospect import Prospect
from harvey.owner_reply import OWNER_ANGLE, ReplyRefused, queue_your_reply, thread_headers
from harvey.state import StateManager
from tests.test_inbound import config

BOX = "kim@ours.example"


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


async def person(state, email="ann@acme-shop.example", **fields):
    fields.setdefault("email_status", "verified")
    return await state.add_prospect(Prospect(first_name="Ann", last_name="Lee", company="Acme",
                                             email=email, status="contacted", mailbox=BOX, **fields))


async def sent_email(state, pid, message_id="<ours-1@ours.example>", thread_ref="t-100",
                     subject="a faster checkout", when="2026-11-01T10:00:00"):
    oid = await state.add_outbox_item(prospect_id=pid, campaign_id="c1", to_email="ann@acme-shop.example",
                                      subject=subject, body="Hi Ann", send_at=when, status="sent",
                                      mailbox=BOX)
    await state.update_outbox_item(oid, message_id=message_id, thread_ref=thread_ref, sent_at=when)
    return oid


async def conversation(state, pid, thread=(), status="open"):
    cid = await state.add_conversation(Conversation(id="", prospect_id=pid, thread=list(thread),
                                                    status=status, mailbox=BOX))
    return await state.get_conversation(cid)


async def outbox(state, oid):
    async with aiosqlite.connect(state.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM outbox WHERE id = ?", (oid,)) as cur:
            return dict(await cur.fetchone())


@pytest.mark.asyncio
async def test_it_answers_their_latest_message_in_their_thread(state):
    pid = await person(state)
    await sent_email(state, pid)
    theirs = Message(sender="prospect", content="How much would it cost?",
                     message_id="<theirs-2@acme-shop.example>", thread_ref="t-100",
                     subject="Re: a faster checkout")
    convo = await conversation(state, pid, [theirs])
    result = await queue_your_reply(state, config(), body="About two weeks of work. Call Tuesday?",
                                    convo_id=convo.id)
    row = await outbox(state, result["outbox_id"])
    assert row["kind"] == "reply" and row["status"] == "approved" and row["approved_by"] == "you"
    assert row["angle"] == OWNER_ANGLE and row["mailbox"] == BOX
    assert row["thread_ref"] == "t-100" and row["in_reply_to"] == "<theirs-2@acme-shop.example>"
    assert row["subject"] == "Re: a faster checkout"
    assert row["conversation_id"] == convo.id


@pytest.mark.asyncio
async def test_an_older_conversation_uses_harveys_reply_for_the_thread(state):
    """Conversations from before Message-IDs were kept: the reply Harvey queued
    to them carries the message it answered and its thread."""
    pid = await person(state)
    await sent_email(state, pid, when="2026-11-01T10:00:00")
    convo = await conversation(state, pid, [Message(sender="prospect", content="Tell me more")])
    await state.add_outbox_item(prospect_id=pid, conversation_id=convo.id, kind="reply",
                                to_email="ann@acme-shop.example", subject="Re: a faster checkout",
                                body="Happy to.", send_at="2026-11-02T09:00:00", status="rejected",
                                thread_ref="t-100", in_reply_to="<theirs-1@acme-shop.example>",
                                mailbox=BOX)
    headers = await thread_headers(state, pid, convo)
    assert headers.in_reply_to == "<theirs-1@acme-shop.example>" and headers.thread_ref == "t-100"


@pytest.mark.asyncio
async def test_someone_who_hasnt_replied_gets_it_on_the_email_we_sent(state):
    pid = await person(state)
    await sent_email(state, pid)
    result = await queue_your_reply(state, config(), body="Just checking this reached you.",
                                    convo_id="awaiting-" + pid)
    row = await outbox(state, result["outbox_id"])
    assert row["in_reply_to"] == "<ours-1@ours.example>" and row["thread_ref"] == "t-100"
    convo = await state.get_conversation(result["conversation_id"])
    assert convo.prospect_id == pid and convo.status == "open" and convo.campaign_id == "c1"


@pytest.mark.asyncio
async def test_your_version_replaces_harveys_waiting_draft_as_an_edit(state):
    pid = await person(state)
    await sent_email(state, pid)
    convo = await conversation(state, pid, [Message(sender="prospect", content="Price?",
                                                    message_id="<theirs@acme-shop.example>")])
    draft = await state.add_outbox_item(prospect_id=pid, conversation_id=convo.id, kind="reply",
                                        to_email="ann@acme-shop.example", subject="Re: a faster checkout",
                                        body="Harvey's version", send_at="2026-11-02T09:00:00",
                                        status="pending_review", thread_ref="t-100",
                                        in_reply_to="<theirs@acme-shop.example>", mailbox=BOX)
    result = await queue_your_reply(state, config(), body="My version", convo_id=convo.id)
    assert result["outbox_id"] == draft and result["replaced_draft"]
    row = await outbox(state, draft)
    assert row["body"] == "My version" and row["status"] == "approved" and row["approved_by"] == "you"
    async with aiosqlite.connect(state.db_path) as db:
        async with db.execute("SELECT details_json FROM actions WHERE action_type = 'outbox_edited'") as cur:
            edits = [r[0] for r in await cur.fetchall()]
    assert len(edits) == 1 and "Harvey's version" in edits[0]


@pytest.mark.asyncio
async def test_taking_it_over_hands_the_conversation_to_you(state):
    pid = await person(state)
    await sent_email(state, pid)
    convo = await conversation(state, pid, [Message(sender="prospect", content="Hello")])
    await queue_your_reply(state, config(), body="I'll handle this one.", convo_id=convo.id, take_over=True)
    # You're handling it: Harvey stays out, and it's off Needs you until they write.
    assert (await state.get_conversation(convo.id)).status == "with_you"


@pytest.mark.asyncio
@pytest.mark.parametrize("change, why", [
    ({"status": "opted_out"}, "opted out"),
    ({"email_status": "invalid"}, "bounced"),
    ({"email": "ann@blocked.example"}, "do-not-contact"),
])
async def test_who_it_is_refused_for(state, change, why):
    email = change.pop("email", "ann@acme-shop.example")
    pid = await person(state, email=email, **{k: v for k, v in change.items() if k == "email_status"})
    if "status" in change:
        await state.update_prospect_status(pid, change["status"])
    await sent_email(state, pid)
    with pytest.raises(ReplyRefused, match=why):
        await queue_your_reply(state, config(), body="Hello", convo_id="awaiting-" + pid)


@pytest.mark.asyncio
async def test_no_thread_and_no_words_are_refused(state):
    pid = await person(state)
    with pytest.raises(ReplyRefused, match="no thread"):
        await queue_your_reply(state, config(), body="Hello", convo_id="awaiting-" + pid)
    await sent_email(state, pid)
    with pytest.raises(ReplyRefused, match="Write the email"):
        await queue_your_reply(state, config(), body="   ", convo_id="awaiting-" + pid)


@pytest.mark.asyncio
async def test_a_message_you_wrote_reads_as_ours_in_the_history():
    from harvey.agents.handler import conversation_history
    text = conversation_history([Message(sender="prospect", content="Price?"),
                                 Message(sender="you", content="Call Tuesday?")], "Ann")
    assert "from the owner, writing for us" in text and "from Ann]" in text


@pytest.mark.asyncio
async def test_the_sender_files_your_email_in_the_thread_as_yours(tmp_path):
    from datetime import datetime, timezone
    from tests.test_outbox_native import FakeProvider, make_sender, seed_prospect
    sm = StateManager(str(tmp_path / "s.db"))
    await sm.init_db()
    pid = await seed_prospect(sm, email="jane@other.example")
    convo = await conversation(sm, pid, [Message(sender="prospect", content="Price?")])
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    await sm.add_outbox_item(prospect_id=pid, conversation_id=convo.id, kind="reply",
                             to_email="jane@other.example", subject="Re: hello there",
                             body="A short, plain note. Question?", send_at=now, status="approved",
                             approved_by="you", angle=OWNER_ANGLE)
    provider = FakeProvider()
    await make_sender(sm, provider)._drain_due()
    assert len(provider.sent) == 1
    assert [m.sender for m in (await sm.get_conversation(convo.id)).thread] == ["prospect", "you"]
