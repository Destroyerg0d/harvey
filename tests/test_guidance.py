"""Guiding Harvey on a conversation: a held draft, and a lesson you keep or skip."""

import os
import tempfile

import aiosqlite
import pytest
import pytest_asyncio

from harvey import guidance as G
from harvey import lessons
from harvey.models.conversation import Conversation, Message
from harvey.models.prospect import Prospect
from harvey.owner_reply import ReplyRefused
from harvey.state import HELD_FOR_REVIEW, StateManager

RULE = "When someone asks about cost before saying what they need, ask what they want built first."


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


class Brain:
    """The reply for generate_response, the rule for guidance_lesson."""

    def __init__(self, reply="Happy to help. What would you like built?", rule=RULE):
        self.reply, self.rule = reply, rule
        self.prompts = {}
        self.paused_reason = ""

    def load_skills_for_agent(self, name):
        return ""

    def load_prompt(self, name, **kw):
        return "PROMPT"

    def load_skill(self, name):
        return ""

    async def think(self, prompt, task="", **kw):
        self.prompts.setdefault(task, []).append(prompt)
        return self.reply if task == "generate_response" else self.rule

    async def think_json(self, prompt, **kw):
        return None


def handler(state, brain):
    from harvey.agents.handler import Handler
    from tests.test_outbox_native import Cfg, Env
    h = Handler(brain=brain, state=state, config=Cfg(), env=Env())
    return h


async def setup(state, thread=None, status="needs_human", **person):
    pid = await state.add_prospect(Prospect(first_name="Ann", last_name="Lee", company="Acme",
                                            email="ann@acme-shop.example", email_status="verified",
                                            status="replied", **person))
    oid = await state.add_outbox_item(prospect_id=pid, campaign_id="c1", to_email="ann@acme-shop.example",
                                      subject="a faster checkout", body="Hi", send_at="2026-11-01T10:00:00",
                                      status="sent")
    await state.update_outbox_item(oid, message_id="<ours@ours.example>", thread_ref="t-1",
                                   sent_at="2026-11-01T10:00:00")
    thread = thread if thread is not None else [
        Message(sender="prospect", content="How much does it cost?", message_id="<theirs@acme-shop.example>",
                thread_ref="t-1", subject="Re: a faster checkout")]
    cid = await state.add_conversation(Conversation(id="", prospect_id=pid, thread=thread,
                                                    status=status, intent="question"))
    return pid, cid


async def outbox(state, oid):
    async with aiosqlite.connect(state.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM outbox WHERE id = ?", (oid,)) as cur:
            return dict(await cur.fetchone())


@pytest.mark.asyncio
async def test_guidance_becomes_a_held_draft_on_the_thread_and_a_proposed_lesson(state):
    _, cid = await setup(state)
    begun = await G.start(state, cid, "Ask what they want built before any price.")
    brain = Brain()
    row = await G.draft_from(state, handler(state, brain), begun["id"])
    assert row["status"] == G.DRAFTED
    item = await outbox(state, row["outbox_id"])
    assert item["status"] == "pending_review" and item["kind"] == "reply"
    assert item["error"].startswith(HELD_FOR_REVIEW)          # "Approve all" passes it by
    assert item["thread_ref"] == "t-1" and item["in_reply_to"] == "<theirs@acme-shop.example>"
    prompt = brain.prompts["generate_response"][0]
    assert "THE OWNER'S INSTRUCTION" in prompt and "Ask what they want built" in prompt
    lesson = await lessons.get_lesson(state, row["lesson_id"])
    assert lesson["kind"] == "replies" and lesson["status"] == "proposed" and lesson["text"] == RULE
    latest = (await G.latest_for(state, [cid]))[cid]
    assert G.summary(latest)["lesson_text"] == RULE


@pytest.mark.asyncio
async def test_a_kept_lesson_reaches_the_handlers_prompt(state):
    _, cid = await setup(state)
    begun = await G.start(state, cid, "Ask what they want built before any price.")
    row = await G.draft_from(state, handler(state, Brain()), begun["id"])
    await lessons.set_status(state, [row["lesson_id"]], "active")
    assert RULE in await lessons.lessons_block(state, "handler")


@pytest.mark.asyncio
@pytest.mark.parametrize("rule", [G.NO_RULE, "Offer them 20 percent off."])
async def test_a_one_off_or_unsafe_rule_proposes_nothing(state, rule):
    _, cid = await setup(state)
    begun = await G.start(state, cid, "Call them Tuesday at 3.")
    row = await G.draft_from(state, handler(state, Brain(rule=rule)), begun["id"])
    assert row["status"] == G.DRAFTED and row["lesson_id"] == ""
    assert row["lesson_note"].startswith("no lesson")


@pytest.mark.asyncio
async def test_guidance_replaces_harveys_waiting_draft(state):
    pid, cid = await setup(state)
    old = await state.add_outbox_item(prospect_id=pid, conversation_id=cid, kind="reply",
                                      to_email="ann@acme-shop.example", subject="Re: a faster checkout",
                                      body="Harvey's first try", send_at="2026-11-02T09:00:00")
    begun = await G.start(state, cid, "Keep it short and ask for their current site.")
    await G.draft_from(state, handler(state, Brain()), begun["id"])
    assert (await outbox(state, old))["status"] == "cancelled"


@pytest.mark.asyncio
async def test_a_failed_draft_is_recorded_and_queues_nothing(state):
    _, cid = await setup(state)
    begun = await G.start(state, cid, "Answer their question.")
    row = await G.draft_from(state, handler(state, Brain(reply="")), begun["id"])
    assert row["status"] == G.FAILED and row["error"]
    assert await state.get_outbox(status="pending_review") == []


@pytest.mark.asyncio
async def test_what_start_refuses(state):
    _, cid = await setup(state)
    with pytest.raises(ReplyRefused, match="first"):
        await G.start(state, cid, "  ")
    await G.start(state, cid, "Answer it.")
    with pytest.raises(ReplyRefused, match="still drafting"):
        await G.start(state, cid, "Answer it differently.")


@pytest.mark.asyncio
async def test_nothing_to_answer_or_an_opt_out_is_refused(state):
    pid, cid = await setup(state, thread=[Message(sender="harvey", content="Hi")])
    with pytest.raises(ReplyRefused, match="haven't written"):
        await G.start(state, cid, "Answer it.")
    await state.update_prospect_status(pid, "opted_out")
    with pytest.raises(ReplyRefused, match="opted out"):
        await G.start(state, cid, "Answer it.")
