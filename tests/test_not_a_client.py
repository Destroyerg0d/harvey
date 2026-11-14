"""Job seekers get pointed to HR once, people selling to us get nothing, and
someone who writes to us first about a project gets an answer (owner,
2026-11-14). Every name, address and number here is fictional."""

import json
from types import SimpleNamespace as NS

import pytest

from harvey.agents.handler import NEW_SENDER_SOURCE, automatic_reply
from harvey.careers import (
    CAREERS_APPROVED, JOB_SEEKER, STOPPED_JOB, VENDOR, careers_core, careers_reply, is_careers_reply,
)
from harvey.gate import pre_send_check
from tests.test_auto_replies import Brain, person, sent
from tests.test_outbox_native import (  # noqa: F401  (state is a fixture)
    Cfg, Env, FakeProvider, InboundMessage, make_sender, state,
)

HR = "hr@studio.example"
PAGE = "https://www.linkedin.com/company/studio-example-1"


class CareersCfg(Cfg):
    class careers:
        email = HR
        linkedin_url = PAGE


def careers_cfg(email=HR, linkedin=PAGE):
    return NS(careers=NS(email=email, linkedin_url=linkedin), persona=NS(company="Studio Example"))


# ── The fixed reply ──

def test_the_reply_names_hr_and_the_linkedin_page_and_nothing_else():
    text = careers_reply(careers_cfg(), "Ann Sample")
    assert text.startswith("Hi Ann,\n\n") and HR in text and PAGE in text
    assert "—" not in text and "–" not in text            # no dashes
    assert pre_send_check("ann@shop.example", "Re: a job", text, kind="reply").ok


def test_with_only_one_contact_it_names_that_one():
    assert HR in careers_core(careers_cfg(linkedin="")) and "LinkedIn" not in careers_core(
        careers_cfg(linkedin=""))
    assert PAGE in careers_core(careers_cfg(email="")) and "@" not in careers_core(careers_cfg(email=""))


def test_with_no_careers_contact_there_is_no_reply():
    assert careers_reply(careers_cfg(email="", linkedin=""), "Ann") == ""
    assert careers_reply(NS(), "Ann") == ""


def test_only_the_exact_text_counts_as_the_careers_reply():
    cfg = careers_cfg()
    assert is_careers_reply(careers_reply(cfg, "Ann"), cfg)
    assert is_careers_reply(careers_core(cfg), cfg)
    assert not is_careers_reply(careers_core(cfg) + " Also see https://elsewhere.example", cfg)
    assert not is_careers_reply("Visit https://elsewhere.example. " + careers_core(cfg), cfg)
    assert not is_careers_reply(careers_core(cfg), careers_cfg(email="", linkedin=""))


# ── Email: a reply from someone Harvey wrote to ──

def handler_with(state, intent, inbound=(), brain=None):
    from harvey.agents.handler import Handler
    provider = FakeProvider()
    provider.inbound = list(inbound)
    h = Handler(brain=brain or Brain(intent), state=state, config=CareersCfg(), env=Env())
    h.provider = provider
    return h


def reply(sender="ann@shop.example", body="Do you have any openings for a junior developer?",
          provider_id="r1", **kw):
    return InboundMessage(provider_id=provider_id, from_email=sender, subject="Re: quick question",
                          body=body, message_id=f"<{provider_id}@shop.example>",
                          thread_ref=f"t-{provider_id}", headers={"from": f"Ann Sample <{sender}>"},
                          in_reply_to="<s1@mail.example>", **kw)


@pytest.mark.asyncio
async def test_a_job_seeker_gets_the_hr_reply_and_the_thread_closes(state):
    pid = await person(state, "ann@shop.example")
    await sent(state, pid, "ann@shop.example")
    h = handler_with(state, JOB_SEEKER, [reply()])
    await h._run_native()
    [item] = await state.get_outbox(status="approved")
    assert item["kind"] == "reply" and item["approved_by"] == CAREERS_APPROVED
    assert HR in item["body"] and PAGE in item["body"] and item["in_reply_to"] == "<r1@shop.example>"
    [convo] = await state.get_conversations_by_status("closed")
    assert convo.intent == JOB_SEEKER and convo.stage == "closed_lost"
    assert (await state.get_prospect(pid)).status == "lost"
    assert h.brain.calls.count("generate_response") == 0     # nothing written by the model


@pytest.mark.asyncio
async def test_the_sender_sends_the_hr_reply_without_waiting_for_you(state):
    pid = await person(state, "ann@shop.example")
    await sent(state, pid, "ann@shop.example")
    h = handler_with(state, JOB_SEEKER, [reply()])
    await h._run_native()
    [item] = await state.get_outbox(status="approved")
    sender = make_sender(state, FakeProvider(), require_approval=True)
    sender.config = CareersCfg()
    assert await sender._blocked(item, await state.get_prospect(pid), allow_risky=False) is None
    # Anything else marked the same way is still reviewed like autopilot.
    item = {**item, "body": item["body"].replace(PAGE, "https://elsewhere.example/apply")}
    held = await sender._blocked(item, await state.get_prospect(pid), allow_risky=False)
    assert held and held[0] == "pending_review"


@pytest.mark.asyncio
async def test_someone_selling_to_us_gets_no_reply(state):
    pid = await person(state, "ann@shop.example")
    await sent(state, pid, "ann@shop.example")
    h = handler_with(state, VENDOR, [reply(body="We build backlinks for agencies, 50 links for $99.")])
    await h._run_native()
    assert await state.get_outbox(status="approved") == []
    assert await state.get_outbox(status="pending_review") == []
    [convo] = await state.get_conversations_by_status("closed")
    assert convo.intent == VENDOR
    assert (await state.get_prospect(pid)).status == "lost"


# ── Email: someone who writes to us first ──

class SortingBrain(Brain):
    """Sorts a first email as ``category`` and classifies it as ``intent``."""

    def __init__(self, category, intent="question", reply="Happy to help. When suits you?"):
        super().__init__(intent, reply)
        self.category = category

    async def think(self, prompt, session_id=None, agent="", task=""):
        if task == "inbound_triage":
            self.calls.append(task)
            return json.dumps({"category": self.category, "size": "medium",
                               "summary": "wants a new online store", "language": "English",
                               "missing": "", "estimate_ready": False, "service_type": "shopify"})
        return await super().think(prompt, session_id, agent, task)


def first(sender="kim@newshop.example", subject="Quote for a Shopify store?",
          body="Hi, we sell candles and want a new Shopify store. Can you quote?"):
    return InboundMessage(provider_id="n1", from_email=sender, subject=subject, body=body,
                          message_id="<n1@newshop.example>", thread_ref="t-n1",
                          headers={"from": f"Kim Sample <{sender}>"})


@pytest.mark.asyncio
async def test_a_potential_client_who_writes_first_gets_an_answer_in_their_thread(state):
    h = handler_with(state, "question", [first()], brain=SortingBrain("project"))
    await h._run_native()
    prospect = await state.get_prospect_by_email("kim@newshop.example")
    assert prospect.source == NEW_SENDER_SOURCE and prospect.status == "replied"
    assert "wants a new online store" in prospect.personalization_notes
    [item] = await state.get_outbox(status="pending_review")        # require_approval: true
    assert item["kind"] == "reply" and item["in_reply_to"] == "<n1@newshop.example>"
    assert item["subject"] == "Re: Quote for a Shopify store?"
    [convo] = await state.get_conversations_by_status("open")
    assert convo.prospect_id == prospect.id and len(convo.thread) == 1


@pytest.mark.asyncio
async def test_a_job_seeker_who_writes_first_is_pointed_to_hr(state):
    h = handler_with(state, "question", [first(subject="Internship", body="I'd love an internship.")],
                     brain=SortingBrain("hiring"))
    await h._run_native()
    [item] = await state.get_outbox(status="approved")
    assert item["approved_by"] == CAREERS_APPROVED and HR in item["body"]
    [convo] = await state.get_conversations_by_status("closed")
    assert convo.intent == JOB_SEEKER
    assert await state.get_conversations_by_status("needs_human") == []
    assert "generate_response" not in h.brain.calls


@pytest.mark.asyncio
async def test_a_vendor_who_writes_first_is_kept_closed_with_no_reply(state):
    h = handler_with(state, "question", [first(subject="SEO services", body="We offer SEO.")],
                     brain=SortingBrain("vendor"))
    await h._run_native()
    assert await state.get_outbox(status="approved") == []
    assert await state.get_outbox(status="pending_review") == []
    [convo] = await state.get_conversations_by_status("closed")
    assert convo.intent == VENDOR


@pytest.mark.asyncio
async def test_spam_makes_no_contact(state):
    h = handler_with(state, "question", [first()], brain=SortingBrain("spam"))
    await h._run_native()
    assert await state.get_prospect_by_email("kim@newshop.example") is None


@pytest.mark.parametrize("category", ["client", "support", "other"])
@pytest.mark.asyncio
async def test_a_client_or_anything_unclear_goes_to_you(state, category):
    h = handler_with(state, "question", [first()], brain=SortingBrain(category))
    await h._run_native()
    [convo] = await state.get_conversations_by_status("needs_human")
    assert len(convo.thread) == 1
    assert await state.get_outbox(status="pending_review") == []
    assert "generate_response" not in h.brain.calls


# ── A helpdesk that says it passed us on is an automatic reply ──

def test_a_helpdesk_forwarding_note_is_an_automatic_acknowledgement():
    text = ("Hi there, thanks for reaching out! We'll forward your email to the respective team "
            "and they will reach out if necessary. Best, The Team")
    assert automatic_reply({}, "Re: quick question", text, text) == "ack"
    assert automatic_reply({}, "Re: quick question", "Yes please send the three changes",
                           "Yes please send the three changes") == ""


# ── WhatsApp ──

def test_whatsapp_a_job_seeker_gets_the_hr_reply_then_whatsapp_stops(tmp_path):
    from tests.whatsapp_fakes import JANE, FakeBrain, Person, World, run
    w = World(tmp_path, [Person(phone=JANE, unread=True, thread=[("in", "Any jobs for a designer?")])],
              brain=FakeBrain(intent=JOB_SEEKER))
    w.config.careers.email, w.config.careers.linkedin_url = HR, PAGE
    c = w.contact(stage="messaged")
    w.go()
    [out] = w.msgs(c, direction="out")
    assert out["approved_by"] == CAREERS_APPROVED and HR in out["body"] and out["status"] == "sent"
    contact = run(w.state.wa_get_contact(c))
    assert contact["stage"] == "declined" and contact["no_message_reason"] == STOPPED_JOB
    assert not any(task == "draft_reply" for task, _ in w.brain.calls)


def test_whatsapp_someone_selling_gets_nothing(tmp_path):
    from tests.whatsapp_fakes import JANE, FakeBrain, Person, World
    w = World(tmp_path, [Person(phone=JANE, unread=True, thread=[("in", "We sell leads, 1000 for $50")])],
              brain=FakeBrain(intent=VENDOR))
    w.config.careers.email = HR
    c = w.contact(stage="messaged")
    w.go()
    assert w.msgs(c, direction="out") == []
    assert w.stage(c) == "declined"


# ── LinkedIn ──

def test_linkedin_a_job_seeker_gets_the_hr_reply_and_a_vendor_nothing(tmp_path):
    import asyncio
    from tests.test_referrals import linkedin_world

    def go(intent):
        (tmp_path / intent).mkdir()
        st, agent = linkedin_world(tmp_path / intent, Brain(intent))
        agent.config.careers.email, agent.config.careers.linkedin_url = HR, PAGE

        async def inbound():
            lead, _ = await st.li_upsert_lead("ann-doe-1", stage="messaged", name="Ann Doe",
                                              first_name="Ann")
            await st.li_add_message(lead, direction="in", kind="message", body="Hello", status="sent")
            await agent.handle_inbound()
            return lead, await st.li_messages(lead_id=lead, direction="out"), await st.li_get_lead(lead)
        return asyncio.run(inbound())

    _, [out], _ = go(JOB_SEEKER)
    assert out["status"] == "approved" and out["approved_by"] == CAREERS_APPROVED
    assert out["body"].startswith("Hi Ann,") and PAGE in out["body"]
    _, outs, lead = go(VENDOR)
    assert outs == [] and lead["stage"] == "declined"
