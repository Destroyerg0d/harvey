"""Referrals: a "wrong person" reply that names who to write to instead.

The person it names (at the company's own domain) becomes a prospect whose
first email says who pointed us there and always waits for a human; the
company stop that follows leaves them out. Everyone here is invented
(.example and .test domains only).
"""

import json
import random
from datetime import datetime, timezone

import pytest

from harvey import referrals as ref
from harvey.agents.handler import Handler
from harvey.brain import BrainUnavailable
from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.state import COLLEAGUE_REPLIED, FREE_MAIL_DOMAINS, HELD_FOR_REVIEW, StateManager
from tests.test_outbox_native import (  # noqa: F401  (state is a fixture)
    Cfg, Env, FakeProvider, InboundMessage, make_sender, state,
)

ANN_REPLY = (
    "Not me I'm afraid. Sam Lee runs the shop now, write to sam.lee@shop.example.\n"
    "\n"
    "Thanks,\n"
    "Ann\n"
    "ann@shop.example | hello@shop.example\n"
)
SAM = {"name": "Sam Lee", "role": "Shop manager", "email": "sam.lee@shop.example"}
SAM_EMAIL = {"subject": "your website",
             "body": "Hi Sam,\n\nAnn suggested I get in touch about your website. "
                     "Your product pages are slow on phones. Worth a short list of fixes?\n\nHarvey"}


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Brain:
    """Answers each task the referral path asks, and keeps every prompt."""

    def __init__(self, intent="wrong_person", who=None, email=None, reply=None):
        self.intent = intent
        self.who = SAM if who is None else who
        self.email = SAM_EMAIL if email is None else email
        self.reply = reply or "Thanks Ann, I'll get in touch with Sam directly."
        self.prompts: dict[str, list[str]] = {}
        self.paused_reason = ""

    def load_skills_for_agent(self, *_):
        return ""

    def load_prompt(self, *_a, **_k):
        return ""

    def load_skill(self, *_):
        return ""

    async def think(self, prompt, session_id=None, agent="", task="", **_):
        self.prompts.setdefault(task, []).append(prompt)
        if task == "classify_intent":
            return self.intent
        if task == "find_referral":
            return json.dumps(self.who)
        if task == "referral_email":
            return json.dumps(self.email) if self.email else ""
        return self.reply


@pytest.fixture(autouse=True)
def approval_mode(monkeypatch):
    """Approval mode unless a test says otherwise: other modules flip the
    shared Cfg class's flag (make_sender) and never put it back."""
    monkeypatch.setattr(Cfg.channels.email, "require_approval", True)


@pytest.fixture
def graded(monkeypatch):
    """The verifier: a grade per address (verified unless told), with every
    call recorded (and whether state was passed, so free tiers are counted)."""
    grades, calls = {}, []

    async def fake(email, env=None, state=None):
        calls.append((email, state is not None))
        return grades.get(email, "verified")

    monkeypatch.setattr(ref, "verify_published_address", fake)
    return grades, calls


async def shop(state, location="Austin, TX"):
    return await state.add_company(Company(name="Shop", domain="shop.example", location=location,
                                           description="Handmade candles"))


async def person(state, email, company_id, first="Ann", status="contacted", **kw):
    return await state.add_prospect(Prospect(
        first_name=first, last_name="Doe", title="Owner", company="Shop", company_id=company_id,
        email=email, email_status="verified", email_verified=True, status=status, **kw))


async def queued(state, pid, email, step=2, status="approved"):
    return await state.add_outbox_item(
        prospect_id=pid, to_email=email, subject="following up", body="Any thoughts?",
        send_at=_now().isoformat(), status=status, campaign_id="c1", step=step)


def reply(sender, body, n=1):
    return InboundMessage(provider_id=f"in-{n}", from_email=sender, subject="Re: quick question",
                          body=body, message_id=f"<in-{n}@their.example>")


async def handle(state, brain, *messages, expect=True):
    provider = FakeProvider()
    provider.inbound = list(messages)
    h = Handler(brain=brain, state=state, config=Cfg(), env=Env())
    h.provider = provider
    assert await h._run_native() is expect
    return h, provider


async def outbox_for(state, email):
    return [r for r in await state.get_outbox(limit=500) if r["to_email"] == email]


async def actions(state, kind):
    async with state._connect() as db:
        async with db.execute("SELECT details_json FROM actions WHERE action_type = ?",
                              (kind,)) as cursor:
            return [r[0] for r in await cursor.fetchall()]


async def conversations(state):
    found = []
    for status in ("open", "needs_human", "closed", "auto"):
        found += await state.get_conversations_by_status(status)
    return found


# ── Reading the reply ──


def test_addresses_come_from_their_own_words_only():
    words = ref.their_own_words(ANN_REPLY)
    assert "hello@shop.example" not in words and "Thanks" not in words
    assert ref.addresses_in(words, exclude={"ann@shop.example"}) == ["sam.lee@shop.example"]
    # A "Thanks!" opening the reply isn't a signature.
    assert "sam@shop.example" in ref.their_own_words("Thanks!\nAsk sam@shop.example instead.")


def test_urgent_matter_lines_our_domains_and_their_own_address_are_never_a_referral():
    text = ("I'm away until Monday. For urgent matters contact jo@shop.example.\n"
            "You could also cc team@studio.example or ops@mail.studio.example, or reply to "
            "ann@shop.example.")
    assert ref.addresses_in(text, exclude={"ann@shop.example"},
                            own_domains={"studio.example"}) == []


@pytest.mark.parametrize("text,found", [
    ("Our support email is support@shop.example.", []),
    ("The right contact for the website is web@shop.example.", ["web@shop.example"]),
    ("Please email marketing@shop.example and they'll pick it up.", ["marketing@shop.example"]),
    ("Send it to info@shop.example, they look after that.", ["info@shop.example"]),
])
def test_a_generic_mailbox_counts_only_when_offered(text, found):
    assert ref.addresses_in(text) == found


def test_registrable_domains_and_free_mail():
    assert ref.registrable_domain("mail.shop.example") == "shop.example"
    assert ref.registrable_domain("www.shop.co.uk") == "shop.co.uk"
    assert ref.company_domains("ann@mail.shop.example", "https://www.shop.example/") == {"shop.example"}
    free = sorted(FREE_MAIL_DOMAINS)[0]
    assert ref.company_domains(f"someone@{free}", "") == set()


@pytest.mark.asyncio
async def test_find_referral_reads_an_address_a_name_and_a_role():
    found = await ref.find_referral(Brain(), ANN_REPLY, domains={"shop.example"},
                                    exclude={"ann@shop.example"})
    assert (found.kind, found.email, found.first_name, found.last_name, found.role) == (
        ref.ADDRESS, "sam.lee@shop.example", "Sam", "Lee", "Shop manager")


@pytest.mark.asyncio
async def test_a_made_up_address_from_the_model_is_never_used():
    brain = Brain(who={"name": "Sam Lee", "role": None, "email": "sam@invented.example"})
    found = await ref.find_referral(brain, "Talk to Sam Lee, he runs things.",
                                    domains={"shop.example"})
    assert found.kind == ref.NAME and found.email == ""


@pytest.mark.asyncio
async def test_a_generic_mailbox_also_needs_the_models_pick():
    text = "The right person is our team at hello@shop.example."
    no_pick = Brain(who={"name": None, "role": None, "email": None})
    assert (await ref.find_referral(no_pick, text, domains={"shop.example"})).kind == ref.NONE
    picked = Brain(who={"name": None, "role": None, "email": "hello@shop.example"})
    assert (await ref.find_referral(picked, text, domains={"shop.example"})).kind == ref.ADDRESS


def test_the_referral_line_is_put_in_when_the_draft_left_it_out():
    body = ref.ensure_opening("Hi Sam,\n\nYour pages are slow. Want fixes?", "Ann")
    assert body.split("\n")[2] == "Ann suggested I get in touch."
    assert ref.ensure_opening(SAM_EMAIL["body"], "Ann") == SAM_EMAIL["body"]


# ── The email path ──


@pytest.mark.asyncio
async def test_a_named_colleague_becomes_a_verified_prospect_with_a_held_first_email(state, graded):
    cid = await shop(state)
    ann = await person(state, "ann@shop.example", cid)
    await handle(state, Brain(), reply("ann@shop.example", ANN_REPLY))
    sam = await state.get_prospect_by_email("sam.lee@shop.example")
    assert sam is not None and sam.source == "referral" and sam.company_id == cid
    assert (sam.first_name, sam.last_name, sam.title, sam.email_status) == (
        "Sam", "Lee", "Shop manager", "verified")
    assert ann in sam.personalization_notes and "Ann Doe" in sam.personalization_notes
    assert graded[1] == [("sam.lee@shop.example", True)]      # counted verifiers: state passed
    [first] = await outbox_for(state, "sam.lee@shop.example")
    assert first["kind"] == "sequence" and first["step"] == 1
    assert first["status"] == "pending_review" and first["error"].startswith(HELD_FOR_REVIEW)
    assert "a referral" in first["error"]
    assert "Ann suggested I get in touch" in first["body"]
    assert (await state.get_prospect(sam.id)).status == "queued"      # off the Writer's list
    assert (await state.get_prospect(ann)).status == "replied"


@pytest.mark.asyncio
async def test_the_first_email_is_held_even_on_autopilot_and_approve_all_skips_it(
        state, graded, monkeypatch):
    monkeypatch.setattr(Cfg.channels.email, "require_approval", False)
    cid = await shop(state)
    await person(state, "ann@shop.example", cid)
    await handle(state, Brain(), reply("ann@shop.example", ANN_REPLY))
    [first] = await outbox_for(state, "sam.lee@shop.example")
    assert first["status"] == "pending_review" and first["approved_by"] == ""
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)
    await sender._run_native()
    await state.approve_outbox()                       # "Approve all"
    await sender._run_native()
    assert [m["to"] for m in provider.sent if m["to"] == "sam.lee@shop.example"] == []
    assert [m["to"] for m in provider.sent] == ["ann@shop.example"]     # the thank-you
    await state.approve_outbox(first["id"])            # you, one by one
    await sender._run_native()
    assert [m["to"] for m in provider.sent] == ["ann@shop.example", "sam.lee@shop.example"]


@pytest.mark.asyncio
@pytest.mark.parametrize("location,law", [("Toronto, ON, Canada", "CASL"),
                                          ("Sydney NSW, Australia", "Spam Act")])
async def test_canada_and_australia_say_a_referral_is_not_consent(state, graded, location, law):
    cid = await shop(state, location=location)
    await person(state, "ann@shop.example", cid)
    await handle(state, Brain(), reply("ann@shop.example", ANN_REPLY))
    [first] = await outbox_for(state, "sam.lee@shop.example")
    assert first["status"] == "pending_review" and law in first["error"]
    assert "not consent" in first["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("grade", ["invalid", "guess"])
async def test_an_address_that_isnt_verified_is_kept_and_never_sent(state, graded, grade):
    graded[0]["sam.lee@shop.example"] = grade
    cid = await shop(state)
    await person(state, "ann@shop.example", cid)
    brain = Brain()
    await handle(state, brain, reply("ann@shop.example", ANN_REPLY))
    sam = await state.get_prospect_by_email("sam.lee@shop.example")
    assert sam.email_status == grade and sam.status == "new"
    assert await outbox_for(state, "sam.lee@shop.example") == []
    assert "referral_email" not in brain.prompts                  # nothing written for them
    assert await state.count_writable_prospects() == 0             # and the Writer won't either
    assert "Promises nothing" in brain.prompts["generate_response"][0]


@pytest.mark.asyncio
async def test_a_catch_all_counts_only_on_the_confirmed_pattern(state, graded):
    graded[0]["sam.lee@shop.example"] = "risky"
    referral = ref.Referral(kind=ref.ADDRESS, email="sam.lee@shop.example",
                            first_name="Sam", last_name="Lee")
    assert await ref.grade_address(state, None, referral) == "guess"
    await state.save_email_pattern("shop.example", pattern="{f}.{l}",
                                   source="hunter_domain_search", confidence=0.9)
    assert await ref.grade_address(state, None, referral) == "risky"
    referral.email = "sammy@shop.example"
    graded[0]["sammy@shop.example"] = "risky"
    assert await ref.grade_address(state, None, referral) == "guess"


@pytest.mark.asyncio
async def test_someone_outside_the_company_goes_to_you_and_nothing_is_sent(state, graded):
    cid = await shop(state)
    await person(state, "ann@shop.example", cid)
    brain = Brain(who={"name": "Pat", "role": "our agency", "email": "pat@agency.test"})
    await handle(state, brain, reply("ann@shop.example",
                                     "Our agency handles that, email pat@agency.test."))
    assert await state.get_prospect_by_email("pat@agency.test") is None
    assert await state.get_outbox(limit=50) == []
    assert "generate_response" not in brain.prompts
    [convo] = await conversations(state)
    assert convo.status == "needs_human"
    [details] = await actions(state, "escalation")
    assert "agency.test" in details


@pytest.mark.asyncio
async def test_a_named_address_on_the_do_not_contact_list_goes_to_you(state, graded, monkeypatch):
    import harvey.agents.handler as handler_mod
    monkeypatch.setattr(handler_mod, "live_do_not_contact", lambda _c: ["sam.lee@shop.example"])
    cid = await shop(state)
    await person(state, "ann@shop.example", cid)
    await handle(state, Brain(), reply("ann@shop.example", ANN_REPLY))
    assert await state.get_prospect_by_email("sam.lee@shop.example") is None
    [convo] = await conversations(state)
    assert convo.status == "needs_human"


@pytest.mark.asyncio
async def test_the_thank_you_says_well_get_in_touch_with_them(state, graded):
    cid = await shop(state)
    await person(state, "ann@shop.example", cid)
    brain = Brain()
    await handle(state, brain, reply("ann@shop.example", ANN_REPLY))
    [prompt] = brain.prompts["generate_response"]
    assert "get in touch with Sam Lee directly" in prompt
    assert "Asks who the right person would be" not in prompt
    [thanks] = [r for r in await state.get_outbox(limit=50) if r["kind"] == "reply"]
    assert thanks["to_email"] == "ann@shop.example" and thanks["status"] == "pending_review"


@pytest.mark.asyncio
async def test_with_nobody_named_the_reply_still_asks_who_and_nothing_is_stopped(state, graded):
    cid = await shop(state)
    await person(state, "ann@shop.example", cid)
    bob = await person(state, "bob@shop.example", cid, first="Bob")
    bobs = await queued(state, bob, "bob@shop.example")
    brain = Brain(who={"name": None, "role": None, "email": None})
    await handle(state, brain, reply("ann@shop.example", "Not me, sorry."))
    assert "Asks who the right person would be" in brain.prompts["generate_response"][0]
    assert (await state.get_outbox_item(bobs))["status"] == "approved"


@pytest.mark.asyncio
async def test_a_name_without_an_address_asks_for_one(state, graded):
    cid = await shop(state)
    await person(state, "ann@shop.example", cid)
    brain = Brain(who={"name": "Sam Lee", "role": None, "email": None})
    await handle(state, brain, reply("ann@shop.example", "Not me, talk to Sam Lee."))
    assert "best email address to reach Sam Lee" in brain.prompts["generate_response"][0]
    assert graded[1] == []


@pytest.mark.asyncio
async def test_quoted_text_is_never_read_as_a_referral(state, graded):
    cid = await shop(state)
    await person(state, "ann@shop.example", cid)
    body = ("Not the right person, sorry.\n\n"
            "On Mon, Harvey wrote:\n> Hi Ann, I could also write to old.owner@shop.example\n")
    brain = Brain(who={"name": None, "role": None, "email": "old.owner@shop.example"})
    await handle(state, brain, reply("ann@shop.example", body))
    assert await state.get_prospect_by_email("old.owner@shop.example") is None
    assert "old.owner" not in brain.prompts["find_referral"][0]


@pytest.mark.asyncio
async def test_someone_already_emailed_gets_no_second_first_email_but_is_kept(state, graded):
    cid = await shop(state)
    await person(state, "ann@shop.example", cid)
    sam = await person(state, "sam.lee@shop.example", cid, first="Sam")
    follow_up = await queued(state, sam, "sam.lee@shop.example")
    brain = Brain()
    await handle(state, brain, reply("ann@shop.example", ANN_REPLY))
    assert [r["id"] for r in await outbox_for(state, "sam.lee@shop.example")] == [follow_up]
    assert (await state.get_outbox_item(follow_up))["status"] == "approved"
    assert "referral_email" not in brain.prompts


@pytest.mark.asyncio
async def test_a_provider_outage_leaves_the_reply_for_later_and_stores_no_referral(state, graded):
    cid = await shop(state)
    await person(state, "ann@shop.example", cid)

    class Down(Brain):
        async def think(self, prompt, session_id=None, agent="", task="", **_):
            if task == "referral_email":
                self.paused_reason = "usage limit"
                return ""
            return await super().think(prompt, session_id, agent, task)

    await handle(state, Down(), reply("ann@shop.example", ANN_REPLY), expect=False)
    assert await state.get_prospect_by_email("sam.lee@shop.example") is None
    assert await conversations(state) == []
    assert not await state.is_reply_processed("in-1")


# ── The company stop keeps the referral, in every reader ──


@pytest.mark.asyncio
async def test_stop_company_keep_is_honoured_everywhere(state, graded):
    cid = await shop(state)
    ann = await person(state, "ann@shop.example", cid)
    bob = await person(state, "bob@shop.example", cid, first="Bob", status="new")
    sam = await person(state, "sam.lee@shop.example", cid, first="Sam", status="new")
    bobs, sams = await queued(state, bob, "bob@shop.example"), await queued(
        state, sam, "sam.lee@shop.example", step=1, status="pending_review")

    assert await state.stop_company(ann, keep={sam}) == 1
    # the cancel
    assert (await state.get_outbox_item(bobs))["status"] == "cancelled"
    assert (await state.get_outbox_item(sams))["status"] == "pending_review"
    # colleague_who_replied (Sender staging, Writer.run, LinkedIn, the backfill)
    assert await state.colleague_who_replied(bob) and not await state.colleague_who_replied(sam)
    # _company_answered (the heartbeat's writable count)
    assert await state.count_writable_prospects() == 1
    # Sender._blocked
    sender = make_sender(state, FakeProvider())
    item = await state.get_outbox_item(sams)
    assert await sender._blocked(item, await state.get_prospect(sam), False) is None
    bob_item = {**item, "prospect_id": bob, "to_email": "bob@shop.example"}
    status, error = await sender._blocked(bob_item, await state.get_prospect(bob), False)
    assert status == "cancelled" and COLLEAGUE_REPLIED in error
    # A later stop by the same person (they write again) still keeps them...
    await state.stop_company(ann)
    assert not await state.colleague_who_replied(sam)
    # ...but another colleague's stop doesn't.
    await state.stop_company(bob)
    assert await state.colleague_who_replied(sam)


@pytest.mark.asyncio
async def test_the_referral_is_kept_while_the_rest_of_the_company_is_stopped(state, graded):
    cid = await shop(state)
    await person(state, "ann@shop.example", cid)
    bob = await person(state, "bob@shop.example", cid, first="Bob")
    bobs = await queued(state, bob, "bob@shop.example")
    await handle(state, Brain(), reply("ann@shop.example", ANN_REPLY))
    status = await state.get_outbox_item(bobs)
    assert status["status"] == "cancelled" and COLLEAGUE_REPLIED in status["error"]
    sam = await state.get_prospect_by_email("sam.lee@shop.example")
    assert await state.colleague_who_replied(sam.id) is None


# ── LinkedIn ──


def linkedin_world(tmp_path, brain, autopilot=False):
    import asyncio
    from harvey.agents.linkedin_agent import LinkedInAgent
    from harvey.config import HarveyConfig
    from tests.linkedin_fakes import IN_HOURS, MINIMAL

    st = StateManager(str(tmp_path / "li.db"))
    asyncio.run(st.init_db())
    config = HarveyConfig(**{**MINIMAL, "channels": {
        "linkedin": {"enabled": True, "autopilot": autopilot, "min_delay_seconds": 0,
                     "max_delay_seconds": 0, "break_min_seconds": 0, "break_max_seconds": 0},
        "email": {"provider": "smtp"}}})

    async def no_sleep(_):
        pass
    agent = LinkedInAgent(brain, st, config, driver_factory=None, now=lambda: IN_HOURS,
                          sleep=no_sleep, rng=random.Random(3), env=Env())
    return st, agent


async def linkedin_lead(st, body):
    cid = await shop(st)
    ann = await person(st, "ann@shop.example", cid)
    lead, _ = await st.li_upsert_lead("ann-doe-1", stage="messaged", name="Ann Doe",
                                      first_name="Ann", company="Shop", prospect_id=ann,
                                      company_id=cid)
    await st.li_add_message(lead, direction="in", kind="message", body=body, status="sent")
    return cid, ann, lead


def test_linkedin_an_address_at_the_company_becomes_the_same_email_referral(tmp_path, graded):
    import asyncio
    brain = Brain(reply="Thanks Ann, I'll get in touch with Sam.")
    st, agent = linkedin_world(tmp_path, brain)

    async def go():
        cid, ann, lead = await linkedin_lead(st, "Not me. Sam Lee looks after it: sam.lee@shop.example")
        bob = await person(st, "bob@shop.example", cid, first="Bob")
        bobs = await queued(st, bob, "bob@shop.example")
        await agent.handle_inbound()
        sam = await st.get_prospect_by_email("sam.lee@shop.example")
        assert sam is not None and sam.source == "referral" and sam.email_status == "verified"
        [first] = await outbox_for(st, "sam.lee@shop.example")
        assert first["status"] == "pending_review" and first["error"].startswith(HELD_FOR_REVIEW)
        assert "Ann suggested I get in touch" in first["body"]
        assert (await st.get_outbox_item(bobs))["status"] == "cancelled"
        assert await st.colleague_who_replied(sam.id) is None
        [draft] = await st.li_messages(lead_id=lead, direction="out")
        assert draft["kind"] == "reply"
        assert "get in touch with Sam Lee directly" in brain.prompts["draft_reply"][0]
    asyncio.run(go())


def test_linkedin_a_profile_becomes_a_lead_whose_invite_waits_even_on_autopilot(tmp_path, graded):
    import asyncio
    brain = Brain(who={"name": "Sam Lee", "role": "Owner", "email": None})
    st, agent = linkedin_world(tmp_path, brain, autopilot=True)

    async def go():
        cid, _, ann_lead = await linkedin_lead(
            st, "Not me, talk to Sam: https://www.linkedin.com/in/sam-lee-example/")
        await agent.handle_inbound()
        lead = await st.li_get_lead_by_slug("sam-lee-example")
        assert lead["stage"] == "found" and lead["company_id"] == cid
        assert lead["source"] == "referral" and "Ann Doe" in lead["note"]
        [invite] = await st.li_messages(lead_id=lead["id"])
        assert invite["kind"] == "invite_note" and invite["status"] == "draft"
        assert invite["error"].startswith(HELD_FOR_REVIEW)
        [thanks] = await st.li_messages(lead_id=ann_lead, direction="out")
        assert thanks["status"] == "approved"                  # autopilot took the thank-you
        assert await st.li_approve() == 0                      # "Approve all" leaves the invite
        assert (await st.li_get_message(invite["id"]))["status"] == "draft"
        assert "reach out to Sam Lee on LinkedIn" in brain.prompts["draft_reply"][0]
    asyncio.run(go())


def test_linkedin_someone_outside_the_company_gets_no_reply_draft(tmp_path, graded):
    import asyncio
    brain = Brain(who={"name": "Pat", "role": None, "email": "pat@agency.test"})
    st, agent = linkedin_world(tmp_path, brain)

    async def go():
        _, _, lead = await linkedin_lead(st, "Our agency does that: pat@agency.test")
        await agent.handle_inbound()
        assert await st.li_messages(lead_id=lead, direction="out") == []
        assert await st.get_prospect_by_email("pat@agency.test") is None
        assert "agency.test" in (await st.li_get_lead(lead))["note"]
    asyncio.run(go())


@pytest.mark.asyncio
async def test_brain_unavailable_is_raised_not_swallowed():
    class Down(Brain):
        async def think(self, *a, **k):
            self.paused_reason = "logged out"
            return ""
    with pytest.raises(BrainUnavailable):
        await ref.find_referral(Down(), "Write to sam@shop.example", domains={"shop.example"})
