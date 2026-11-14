"""A WhatsApp message to the owner's own phone when a client gets close to a
meeting, from any channel (owner, 2026-11-14). Every number, name and address
here is fictional."""

import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from harvey import meeting_alert as ma
from harvey.whatsapp.schema import OWNER_STAGE
from tests.test_auto_replies import Brain, handler, person, sent
from tests.test_outbox_native import InboundMessage, state  # noqa: F401  (state is a fixture)

OWNER = "+15555550199"
LINKED = "+15555550100"
CLOSE = {"close_to_meeting": True, "when": "Thursday 3pm EST", "service": "Shopify store redesign",
         "summary": "They want a call to scope a redesign.", "next_step": "Confirm Thursday 3pm."}


class MeetingBrain(Brain):
    """Classifies as ``intent``; reads the conversation as ``read``."""

    def __init__(self, intent="interested", read=None, reply="Happy to. When suits you?"):
        super().__init__(intent, reply)
        self.read = CLOSE if read is None else read

    async def think(self, prompt, session_id=None, agent="", task=""):
        if task == "meeting_alert":
            self.calls.append(task)
            return json.dumps(self.read)
        return await super().think(prompt, session_id, agent, task)


def cfg(alert=OWNER, linked=LINKED):
    return NS(channels=NS(whatsapp=NS(alert_number=alert, number=linked)),
              persona=NS(company="Studio Example"))


async def alert(state, brain, config=None, latest="Can we do a call on Thursday at 3pm?", key="email:c1"):
    return await ma.maybe_alert(state, brain, config or cfg(), channel="Email", key=key,
                                name="Ann Sample", company="Shop Example", latest=latest,
                                thread="Them: " + latest, contacts=["ann@shop.example"],
                                where="Harvey dashboard, Conversations tab, Ann Sample")


async def alerts(state):
    contact = await state.wa_get_contact_by_phone(OWNER)
    return [] if contact is None else await state.wa_messages(contact_id=contact["id"])


def test_the_word_check_only_lets_meeting_talk_through():
    for text in ("Can we talk Thursday?", "Free at 3pm EST", "Send me a calendar invite",
                 "Let's book a call", "What time works for you?"):
        assert ma.might_be_meeting(text), text
    for text in ("Sure I'd like to get your opinion", "How much does it cost?", "Not interested"):
        assert not ma.might_be_meeting(text), text


@pytest.mark.asyncio
async def test_a_meeting_close_queues_one_alert_to_your_phone(state):
    assert await alert(state, MeetingBrain())
    [message] = await alerts(state)
    assert message["kind"] == "alert" and message["status"] == "approved"
    body = message["body"]
    for part in ("Ann Sample (Shop Example)", "Channel: Email", "Read it: Harvey dashboard, Conversations tab",
                 "They want: Shopify store redesign", "When: Thursday 3pm EST", "ann@shop.example",
                 "Next: Confirm Thursday 3pm."):
        assert part in body, part
    contact = await state.wa_get_contact_by_phone(OWNER)
    assert contact["stage"] == OWNER_STAGE
    # The same conversation doesn't alert again for a while; another does.
    assert not await alert(state, MeetingBrain())
    assert await alert(state, MeetingBrain(), key="linkedin:l2")
    assert len(await alerts(state)) == 2


@pytest.mark.asyncio
async def test_no_alert_when_the_model_says_not_yet_and_it_can_alert_later(state):
    assert not await alert(state, MeetingBrain(read={"close_to_meeting": False}))
    assert await alerts(state) == []
    assert await alert(state, MeetingBrain())                 # a later message can still alert


@pytest.mark.asyncio
@pytest.mark.parametrize("config", [cfg(alert=""), cfg(alert=LINKED), cfg(alert="1234")])
async def test_no_alert_without_a_usable_number_of_your_own(state, config):
    assert not await alert(state, MeetingBrain(), config=config)
    assert await alerts(state) == []


@pytest.mark.asyncio
async def test_plain_interest_never_asks_the_model(state):
    brain = MeetingBrain()
    assert not await alert(state, brain, latest="Sure, I'd like your opinion")
    assert "meeting_alert" not in brain.calls


# ── Email ──

@pytest.mark.asyncio
async def test_an_email_reply_asking_for_a_call_alerts_you(state):
    from harvey.agents.handler import Handler
    from tests.test_outbox_native import Cfg, Env, FakeProvider

    class AlertCfg(Cfg):
        class channels(Cfg.channels):
            class whatsapp:
                alert_number = OWNER
                number = LINKED

    pid = await person(state, "ann@shop.example")
    await sent(state, pid, "ann@shop.example")
    provider = FakeProvider()
    provider.inbound = [InboundMessage(
        provider_id="r1", from_email="ann@shop.example", subject="Re: quick question",
        body="Yes, can we do a call on Thursday at 3pm EST?", message_id="<r1@shop.example>",
        thread_ref="t1", headers={"from": "Ann <ann@shop.example>"}, in_reply_to="<s1@mail.example>")]
    h = Handler(brain=MeetingBrain(), state=state, config=AlertCfg(), env=Env())
    h.provider = provider
    await h._run_native()
    [message] = await alerts(state)
    assert "Channel: Email" in message["body"] and "Conversations tab" in message["body"]


# ── WhatsApp: the alert goes, outside every rule a lead has ──

def test_whatsapp_sends_the_alert_and_never_reads_your_chat_as_a_lead(tmp_path):
    from tests.whatsapp_fakes import Person, World, run
    w = World(tmp_path, [Person(phone=OWNER, unread=True, thread=[("in", "Thanks, got it")])],
              alert_number=OWNER)
    message_id = run(ma.queue_to_owner(w.state, w.config, "Harvey: a meeting is close with Ann"))
    w.go()
    assert w.get(message_id)["status"] == "sent"
    contact = run(w.state.wa_get_contact_by_phone(OWNER))
    assert contact["stage"] == OWNER_STAGE
    assert w.msgs(contact["id"], direction="in") == []        # your reply isn't a lead's


# ── LinkedIn ──

def test_linkedin_a_reply_proposing_a_time_alerts_you(tmp_path):
    from tests.test_referrals import linkedin_world
    st, agent = linkedin_world(tmp_path, MeetingBrain())
    agent.config.channels.whatsapp.alert_number = OWNER

    async def go():
        lead, _ = await st.li_upsert_lead("ann-doe-1", stage="messaged", name="Ann Doe", first_name="Ann",
                                          company="Shop Example",
                                          profile_url="https://www.linkedin.com/in/ann-doe-1/")
        await st.li_add_message(lead, direction="in", kind="message", status="sent",
                                body="Sounds good, are you free for a quick call tomorrow at 11am?")
        await agent.handle_inbound()
        return await alerts(st)
    [message] = asyncio.run(go())
    assert "Channel: LinkedIn" in message["body"] and "LinkedIn tab, Ann Doe" in message["body"]
    assert "linkedin.com/in/ann-doe-1" in message["body"]
