"""What the Sender and Handler do differently for a follow-up to someone who gave
us their address in our own chat: no compliance check, first in line, outside the
segment mix and samples, a fresh chat ignoring the send window, and everything
else (footer, caps, pauses, gate, stop-on-reply) unchanged. Everyone here is
invented (.example domains only) and every amount is made up."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import aiosqlite
import httpx
import pytest

import harvey.agents.sender as sender_mod
from harvey.agents.handler import Handler
from harvey.agents.learner import gather
from harvey.config import HarveyConfig, InboundConfig
from harvey.integrations.precleared import PreclearedClient
from harvey.models.conversation import Conversation, Message
from harvey.models.prospect import Prospect
from harvey.segment_mix import SegmentMix, first_emails_since, is_first_email
from harvey.segment_stamp import (
    INBOUND_ANGLE, INBOUND_AUTOPILOT, INBOUND_ESTIMATE_ANGLE, INBOUND_FRESH_ANGLE,
    is_fresh_inbound_row,
)
from harvey.state import HELD_FOR_REVIEW
from tests.test_outbox_native import Cfg, Env, FakeProvider, make_sender, state  # noqa: F401
from tests.test_send_window import (
    BASE, WED_0200, _patch_heartbeat_real_sender, pin_now, seed_at, window_sender,
)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture(autouse=True)
def fresh_cfg(monkeypatch):
    """Cfg is a shared class that other modules flip and never put back."""
    monkeypatch.setattr(Cfg.channels.email, "require_approval", False)
    monkeypatch.setattr(Cfg.channels.email, "max_daily_sends", 50)


async def seed(state, email="ann@acme-shop.example", status="queued"):
    return await state.add_prospect(Prospect(
        first_name="Ann", last_name="Lee", title="Website enquiry", company="Acme Shop", email=email,
        email_status="verified", email_verified=True, status=status, source="inbound:website_chat"))


async def inbound_row(state, pid, to="ann@acme-shop.example", angle=INBOUND_ANGLE,
                      approved_by=INBOUND_AUTOPILOT, status="approved", body="A short plain note. Question?",
                      send_at=None):
    item = await state.add_outbox_item(
        prospect_id=pid, to_email=to, subject="your new shop", body=body,
        send_at=send_at or (_now() - timedelta(minutes=1)).isoformat(), status=status, step=1,
        kind="sequence", provider="fake", angle=angle, approved_by=approved_by)
    return item


def inbound_on(monkeypatch, sender, **settings):
    monkeypatch.setattr(sender.config.channels.email, "inbound", InboundConfig(**settings), raising=False)


# ── Precleared ──

@pytest.mark.asyncio
@pytest.mark.parametrize("angle", [INBOUND_ANGLE, INBOUND_FRESH_ANGLE])
async def test_inbound_skips_precleared(state, monkeypatch, angle):
    calls = []

    def answer(request):
        calls.append(request)
        return httpx.Response(503)
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)
    inbound_on(monkeypatch, sender, autopilot=True)
    sender.precleared = PreclearedClient(sender_country="GB", transport=httpx.MockTransport(answer))
    await inbound_row(state, await seed(state), angle=angle)
    await sender._drain_due()
    assert calls == [] and [m["to"] for m in provider.sent] == ["ann@acme-shop.example"]


@pytest.mark.asyncio
async def test_an_approved_estimate_skips_precleared_too(state):
    calls = []

    def answer(request):
        calls.append(request)
        return httpx.Response(503)
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)
    sender.precleared = PreclearedClient(sender_country="GB", transport=httpx.MockTransport(answer))
    await inbound_row(state, await seed(state), angle=INBOUND_ESTIMATE_ANGLE, approved_by="you")
    await sender._drain_due()
    assert calls == [] and len(provider.sent) == 1


@pytest.mark.asyncio
async def test_cold_email_is_still_checked(state):
    calls = []

    def answer(request):
        calls.append(request)
        return httpx.Response(503)
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)
    sender.precleared = PreclearedClient(sender_country="GB", transport=httpx.MockTransport(answer))
    pid = await seed(state, "cold@other.example")
    await state.add_outbox_item(
        prospect_id=pid, to_email="cold@other.example", subject="hello there", body="A plain note.",
        send_at=(_now() - timedelta(minutes=1)).isoformat(), status="approved", campaign_id="c1", step=1,
        approved_by="autopilot")
    await sender._drain_due()
    assert calls and provider.sent == []


@pytest.mark.asyncio
async def test_a_held_inbound_row_isnt_counted_as_waiting_on_the_check(state):
    sender = make_sender(state, FakeProvider(), require_approval=False)
    sender.precleared = PreclearedClient(sender_country="GB",
                                         transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    await inbound_row(state, await seed(state))
    waiting = await state.count_due_cold_outbox(
        _now().isoformat(), has_cached_verdict=sender._has_cached_verdict)
    assert waiting == 0


# ── Drain order ──

def test_inbound_rows_arent_first_emails():
    for angle in (INBOUND_ANGLE, INBOUND_FRESH_ANGLE, INBOUND_ESTIMATE_ANGLE):
        assert not is_first_email({"kind": "sequence", "step": 1, "angle": angle})
    assert is_first_email({"kind": "sequence", "step": 1, "angle": "tools"})
    assert is_first_email({"kind": "sequence", "step": 1})


@pytest.mark.asyncio
async def test_they_go_ahead_of_a_cold_backlog_with_no_segment_mix(state, monkeypatch):
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)
    inbound_on(monkeypatch, sender, autopilot=True)
    earlier = (_now() - timedelta(days=3)).isoformat()
    for n in range(40):      # more than a page, all older
        email = f"cold{n}@backlog{n}.example"
        pid = await seed(state, email)
        await state.add_outbox_item(
            prospect_id=pid, to_email=email, subject="hello there", body="A plain note.",
            send_at=earlier, status="approved", campaign_id="c1", step=1, approved_by="autopilot")
    await inbound_row(state, await seed(state))
    monkeypatch.setattr(sender_mod, "MAX_SENDS_PER_CYCLE", 1)
    await sender._drain_due()
    assert [m["to"] for m in provider.sent] == ["ann@acme-shop.example"]


@pytest.mark.asyncio
async def test_replies_still_go_before_them(state, monkeypatch):
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)
    inbound_on(monkeypatch, sender, autopilot=True)
    await inbound_row(state, await seed(state), send_at=(_now() - timedelta(hours=1)).isoformat())
    answered = await seed(state, "writes@back.example", status="replied")
    await state.add_outbox_item(
        prospect_id=answered, to_email="writes@back.example", subject="Re: hello", body="Sure.",
        send_at=(_now() - timedelta(minutes=1)).isoformat(), status="approved", kind="reply",
        approved_by="autopilot")
    monkeypatch.setattr(sender_mod, "MAX_SENDS_PER_CYCLE", 1)
    await sender._drain_due()
    assert [m["to"] for m in provider.sent] == ["writes@back.example"]


@pytest.mark.asyncio
async def test_the_mix_puts_them_first_and_doesnt_count_them(state):
    now = _now().isoformat()
    pid = await seed(state)
    item = await inbound_row(state, pid)
    await state.update_outbox_item(item, status="sent", sent_at=now, segment="unknown")
    assert await first_emails_since(state.db_path, (_now() - timedelta(hours=1)).isoformat()) == {}

    cold = {"id": "c", "kind": "sequence", "step": 1, "angle": "tools"}
    warm = {"id": "w", "kind": "sequence", "step": 1, "angle": INBOUND_ANGLE}
    mix = SegmentMix({"shopify": 1.0})
    mix.segments = {"c": "shopify"}
    assert mix.order([[cold, warm]]) == [[warm, cold]]
    assert mix.allows(warm)
    mix.record(warm)
    assert mix.counts == {}


@pytest.mark.asyncio
async def test_samples_ignore_them(state, monkeypatch):
    sender = make_sender(state, FakeProvider(), require_approval=False)
    monkeypatch.setattr(sender.config.channels.email, "review_segments", ["unknown"], raising=False)
    pid = await seed(state)
    row = await state.get_outbox_item(await inbound_row(state, pid))
    assert await sender._segment_sample(row, await state.get_prospect(pid)) is None
    cold = {**row, "angle": "tools"}
    assert await sender._segment_sample(cold, await state.get_prospect(pid)) is not None


@pytest.mark.asyncio
async def test_a_sent_row_gets_no_subject_variant(state):
    pid = await seed(state)
    sender = make_sender(state, FakeProvider(), require_approval=False)
    row = await state.get_outbox_item(await inbound_row(state, pid))
    stamp = await sender._outcome_stamp(row, await state.get_prospect(pid))
    assert "variant" not in stamp


# ── The fresh window ──

@pytest.mark.asyncio
async def test_a_fresh_chat_ignores_the_recipients_window(state, monkeypatch):
    provider = FakeProvider()
    sender = window_sender(state, provider, monkeypatch)
    inbound_on(monkeypatch, sender, autopilot=True)
    pid = await seed_at(state, "New York, NY", "jane@ny.example")   # 22:00 there
    await inbound_row(state, pid, to="jane@ny.example", angle=INBOUND_FRESH_ANGLE)
    pin_now(monkeypatch, WED_0200)
    await sender._drain_due()
    assert [m["to"] for m in provider.sent] == ["jane@ny.example"]


@pytest.mark.asyncio
@pytest.mark.parametrize("angle,by", [(INBOUND_ANGLE, INBOUND_AUTOPILOT), (INBOUND_ESTIMATE_ANGLE, "you")])
async def test_older_ones_and_estimates_keep_the_window(state, monkeypatch, angle, by):
    provider = FakeProvider()
    sender = window_sender(state, provider, monkeypatch)
    inbound_on(monkeypatch, sender, autopilot=True)
    pid = await seed_at(state, "New York, NY", "jane@ny.example")
    item = await inbound_row(state, pid, to="jane@ny.example", angle=angle, approved_by=by)
    pin_now(monkeypatch, WED_0200)
    await sender._drain_due()
    assert provider.sent == []
    row = await state.get_outbox_item(item)
    assert row["status"] == "approved" and datetime.fromisoformat(row["send_at"]) > WED_0200


def test_a_fresh_row_left_waiting_past_a_day_keeps_the_window_too():
    at = datetime(2026, 10, 20, 12, 0)
    row = {"angle": INBOUND_FRESH_ANGLE, "created_at": "2026-10-20 12:00:00"}
    assert is_fresh_inbound_row(row, at + timedelta(hours=23))
    assert not is_fresh_inbound_row(row, at + timedelta(hours=25))
    assert not is_fresh_inbound_row({**row, "angle": INBOUND_ANGLE}, at)
    assert not is_fresh_inbound_row({**row, "angle": INBOUND_ESTIMATE_ANGLE}, at)


@pytest.mark.asyncio
async def test_quiet_hours_with_windows_wake_for_a_fresh_chat(state, monkeypatch):
    sender = window_sender(state, FakeProvider(), monkeypatch)
    pid = await seed_at(state, "New York, NY", "jane@ny.example")
    await inbound_row(state, pid, to="jane@ny.example", angle=INBOUND_FRESH_ANGLE)
    pin_now(monkeypatch, WED_0200)
    assert await sender.cold_due_in_window()


@pytest.mark.asyncio
async def test_quiet_hours_with_no_windows_send_only_a_fresh_chat(state, monkeypatch):
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)
    inbound_on(monkeypatch, sender, autopilot=True)
    sender.cold_only = sender.inbound_only = True
    cold = await seed(state, "cold@other.example")
    await state.add_outbox_item(
        prospect_id=cold, to_email="cold@other.example", subject="hello there", body="A plain note.",
        send_at=(_now() - timedelta(minutes=1)).isoformat(), status="approved", campaign_id="c1", step=1,
        approved_by="autopilot")
    assert not await sender.cold_due_in_window()

    older = await seed(state, "older@other.example")
    await inbound_row(state, older, to="older@other.example", angle=INBOUND_ANGLE)
    assert not await sender.cold_due_in_window()

    await inbound_row(state, await seed(state), angle=INBOUND_FRESH_ANGLE)
    assert await sender.cold_due_in_window()
    await sender._drain_due()
    assert [m["to"] for m in provider.sent] == ["ann@acme-shop.example"]


@pytest.mark.asyncio
async def test_with_it_off_nothing_changes_in_quiet_hours(state):
    sender = make_sender(state, FakeProvider(), require_approval=False)
    await inbound_row(state, await seed(state), angle=INBOUND_FRESH_ANGLE)
    assert not await sender.cold_due_in_window()    # no window, not inbound_only


def _no_window_cfg(**inbound):
    return HarveyConfig(**BASE, channels={"email": {
        "provider": "gmail", "require_approval": False, "inbound": inbound}})


@pytest.mark.asyncio
async def test_the_heartbeat_in_quiet_hours_sends_only_a_fresh_chat(state, monkeypatch):
    """No send windows at all: everything else waits for the operator's hours."""
    cold = await seed(state, "cold@other.example")
    held = await state.add_outbox_item(
        prospect_id=cold, to_email="cold@other.example", subject="hello there", body="A plain note.",
        send_at=(_now() - timedelta(minutes=1)).isoformat(), status="approved", campaign_id="c1", step=1,
        approved_by="autopilot")
    await inbound_row(state, await seed(state), angle=INBOUND_FRESH_ANGLE)
    provider, calls, stop = FakeProvider(), [], asyncio.Event()
    main_mod = _patch_heartbeat_real_sender(
        monkeypatch, _no_window_cfg(enabled=True, autopilot=True), state, calls, stop, provider)
    await asyncio.wait_for(main_mod.heartbeat(stop), timeout=30)
    assert [m["to"] for m in provider.sent] == ["ann@acme-shop.example"]
    assert calls == ["replies"]      # replies read first, as any send; nothing else ran
    assert (await state.get_outbox_item(held))["status"] == "approved"


@pytest.mark.asyncio
async def test_the_heartbeat_in_quiet_hours_doesnt_wake_for_anything_else(state, monkeypatch):
    await inbound_row(state, await seed(state), angle=INBOUND_ANGLE)     # an older chat
    provider, calls, stop = FakeProvider(), [], asyncio.Event()
    main_mod = _patch_heartbeat_real_sender(
        monkeypatch, _no_window_cfg(enabled=True, autopilot=True), state, calls, stop, provider)
    await asyncio.wait_for(main_mod.heartbeat(stop), timeout=30)
    assert calls == [] and provider.sent == []


@pytest.mark.asyncio
async def test_the_heartbeat_with_it_off_holds_a_fresh_chat_through_quiet_hours(state, monkeypatch):
    await inbound_row(state, await seed(state), angle=INBOUND_FRESH_ANGLE)
    provider, calls, stop = FakeProvider(), [], asyncio.Event()
    main_mod = _patch_heartbeat_real_sender(
        monkeypatch, _no_window_cfg(enabled=False), state, calls, stop, provider)
    await asyncio.wait_for(main_mod.heartbeat(stop), timeout=30)
    assert calls == [] and provider.sent == []


def test_quiet_hours_wake_only_when_it_is_on():
    from harvey.main import inbound_wakes_quiet_hours, sends_through_quiet_hours

    def cfg(enabled=True, provider="gmail", channel=True):
        email = SimpleNamespace(enabled=channel, provider=provider, send_window=None,
                                inbound=InboundConfig(enabled=enabled))
        return SimpleNamespace(channels=SimpleNamespace(email=email))
    assert inbound_wakes_quiet_hours(cfg())
    assert not inbound_wakes_quiet_hours(cfg(enabled=False))
    assert not inbound_wakes_quiet_hours(cfg(provider="instantly"))
    assert not inbound_wakes_quiet_hours(cfg(channel=False))
    assert not sends_through_quiet_hours(cfg())      # the older rule doesn't change


# ── Everything else still applies ──

@pytest.mark.asyncio
async def test_the_footer_goes_on(state, monkeypatch):
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)
    inbound_on(monkeypatch, sender, autopilot=True)
    monkeypatch.setattr(sender_mod, "compliance_footer_lines",
                        lambda config: ["1 Example Street", 'Reply "no thanks" and I won\'t email again.'])
    await inbound_row(state, await seed(state))
    await sender._drain_due()
    assert provider.sent[0]["body"].endswith('Reply "no thanks" and I won\'t email again.')
    assert "1 Example Street" in provider.sent[0]["body"]


@pytest.mark.asyncio
async def test_the_daily_cap_still_holds_them(state, monkeypatch):
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)
    inbound_on(monkeypatch, sender, autopilot=True)
    monkeypatch.setattr(sender.config.channels.email, "max_daily_sends", 0)
    await inbound_row(state, await seed(state))
    await sender._drain_due()
    assert provider.sent == []


@pytest.mark.asyncio
async def test_a_pause_still_holds_them(state, monkeypatch):
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)
    inbound_on(monkeypatch, sender, autopilot=True)
    await state.set_setting(sender_mod.KILL_SWITCH_KEY, "test pause")
    await inbound_row(state, await seed(state))
    await sender._run_native()
    assert provider.sent == []


@pytest.mark.asyncio
async def test_the_gate_still_blocks_them(state, monkeypatch):
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)
    inbound_on(monkeypatch, sender, autopilot=True)
    item = await inbound_row(state, await seed(state), body="Act now, this is a special offer!")
    await sender._drain_due()
    assert provider.sent == []
    assert (await state.get_outbox_item(item))["status"] == "failed"


@pytest.mark.asyncio
async def test_opted_out_and_stop_on_reply_still_stop_them(state, monkeypatch):
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)
    inbound_on(monkeypatch, sender, autopilot=True)
    pid = await seed(state)
    item = await inbound_row(state, pid)
    await state.update_prospect_status(pid, "opted_out")
    await sender._drain_due()
    assert provider.sent == [] and (await state.get_outbox_item(item))["status"] == "cancelled"


@pytest.mark.asyncio
async def test_a_planted_link_still_sends_it_back_to_you(state, monkeypatch):
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)
    inbound_on(monkeypatch, sender, autopilot=True)
    item = await inbound_row(state, await seed(state), body="See https://planted.example/x please.")
    await sender._drain_due()
    row = await state.get_outbox_item(item)
    assert provider.sent == [] and row["status"] == "pending_review"
    assert row["error"].startswith(HELD_FOR_REVIEW)


# ── Its autopilot is its own switch ──

@pytest.mark.asyncio
async def test_inbound_autopilot_goes_while_require_approval_is_on(state, monkeypatch):
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=True)
    inbound_on(monkeypatch, sender, autopilot=True)
    monkeypatch.setattr(sender_mod, "sync_sending_mode", _no_sync)
    await inbound_row(state, await seed(state))
    await sender._drain_due()
    assert len(provider.sent) == 1


@pytest.mark.asyncio
async def test_turning_inbound_autopilot_off_sends_what_it_approved_back_to_you(state, monkeypatch):
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=True)
    inbound_on(monkeypatch, sender, autopilot=False)
    monkeypatch.setattr(sender_mod, "sync_sending_mode", _no_sync)
    item = await inbound_row(state, await seed(state))
    await sender._drain_due()
    row = await state.get_outbox_item(item)
    assert provider.sent == [] and row["status"] == "pending_review"
    assert row["error"].startswith(HELD_FOR_REVIEW) and "inbound.autopilot" in row["error"]


async def _no_sync(config, state):
    return None


# ── A written estimate ──

ESTIMATE_BODY = ("Hi Ann,\n\nFollowing your chat on our website.\n\nPlans:\n- Growth: $7,300 (recommended)\n\n"
                 "Harvey")


@pytest.mark.asyncio
@pytest.mark.parametrize("by", ["", "autopilot", INBOUND_AUTOPILOT, "earlier"])
async def test_an_estimate_nobody_approved_never_goes_however_it_got_marked(state, monkeypatch, by):
    """Three locks in a row: this is the last, if something else ever marks one approved."""
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)
    inbound_on(monkeypatch, sender, autopilot=True)
    item = await inbound_row(state, await seed(state), angle=INBOUND_ESTIMATE_ANGLE, approved_by=by,
                             body=ESTIMATE_BODY)
    await sender._drain_due()
    row = await state.get_outbox_item(item)
    assert provider.sent == [] and row["status"] == "pending_review"
    assert row["error"].startswith(HELD_FOR_REVIEW) and "check the price" in row["error"]


@pytest.mark.asyncio
async def test_an_estimate_a_person_approved_goes(state, monkeypatch):
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)
    item = await inbound_row(state, await seed(state), angle=INBOUND_ESTIMATE_ANGLE, approved_by="",
                             status="pending_review", body=ESTIMATE_BODY)
    assert await state.approve_outbox(item) == 1
    await sender._drain_due()
    assert len(provider.sent) == 1 and (await state.get_outbox_item(item))["status"] == "sent"


@pytest.mark.asyncio
async def test_the_gate_lets_a_full_length_estimate_through_with_its_footer(state, monkeypatch):
    """The gate's usual ceiling is below an estimate's limit: approved, it must not fail at send."""
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)
    monkeypatch.setattr(sender_mod, "compliance_footer_lines",
                        lambda config: ["1 Example Street, Example Town", 'Reply "no thanks" and I won\'t email again.'])
    long_body = "$900 " + "word " * 298
    assert 220 < len(long_body.split()) < 300
    item = await inbound_row(state, await seed(state), angle=INBOUND_ESTIMATE_ANGLE, approved_by="you",
                             body=long_body)
    await sender._drain_due()
    assert (await state.get_outbox_item(item))["status"] == "sent" and len(provider.sent) == 1


@pytest.mark.asyncio
async def test_the_same_length_as_an_ordinary_email_still_fails_the_gate(state, monkeypatch):
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)
    inbound_on(monkeypatch, sender, autopilot=True)
    item = await inbound_row(state, await seed(state), body="word " * 250)
    await sender._drain_due()
    assert provider.sent == [] and "body too long" in (await state.get_outbox_item(item))["error"]


# ── The Handler reads the notes ──

@pytest.mark.asyncio
async def test_reply_drafting_sees_personalization_notes_fenced(state):
    prompts = []

    class Brain:
        paused_reason = ""

        def load_skills_for_agent(self, *_):
            return ""

        def load_prompt(self, *_a, **_k):
            return ""

        def load_skill(self, *_):
            return ""

        async def think(self, prompt, session_id=None, agent="", task="", **_):
            prompts.append(prompt)
            return "Happy to help."
    handler = Handler(brain=Brain(), state=state, config=Cfg(), env=Env())
    handler.skills = ""
    pid = await state.add_prospect(Prospect(
        first_name="Ann", email="ann@acme-shop.example", status="contacted",
        personalization_notes="The live chat on our website, 2026-10-01: wants a new online shop"))
    prospect = await state.get_prospect(pid)
    convo = Conversation(id="", prospect_id=pid, channel="email", intent="question",
                         thread=[Message(sender="prospect", content="How long would it take?")])
    await handler._generate_response("question", "How long would it take?", prospect, convo)
    assert "wants a new online shop" in prompts[0]
    assert '<untrusted source="notes on them">' in prompts[0]


@pytest.mark.asyncio
async def test_no_notes_adds_no_section(state):
    prompts = []

    class Brain:
        paused_reason = ""

        def load_prompt(self, *_a, **_k):
            return ""

        async def think(self, prompt, session_id=None, agent="", task="", **_):
            prompts.append(prompt)
            return "Happy to help."
    handler = Handler(brain=Brain(), state=state, config=Cfg(), env=Env())
    handler.skills = ""
    pid = await state.add_prospect(Prospect(first_name="Ann", email="ann@acme-shop.example", status="contacted"))
    convo = Conversation(id="", prospect_id=pid, channel="email", intent="question",
                         thread=[Message(sender="prospect", content="Hello?")])
    await handler._generate_response("question", "Hello?", await state.get_prospect(pid), convo)
    assert "What we knew about them" not in prompts[0]


# ── The learner reads cold email only ──

@pytest.mark.asyncio
async def test_the_learner_leaves_them_out_of_its_comparisons(state):
    now = _now()
    pid = await seed(state)
    item = await inbound_row(state, pid)
    await state.update_outbox_item(item, status="sent", sent_at=(now - timedelta(hours=1)).isoformat(),
                                   segment="unknown")
    data = await gather(state, now=now)
    assert data["rows"] == []
    assert not any("inbound" in key for key in data["cells"])
    # The outcome table itself still counts them, under their own angle.
    from harvey.outcomes import build_outcomes
    rows = await build_outcomes(state, days=30, now=now)
    assert [r["angle"] for r in rows] == [INBOUND_ANGLE]
    async with aiosqlite.connect(state.db_path) as db:
        async with db.execute("SELECT COUNT(*) FROM outbox WHERE status = 'sent'") as cursor:
            assert (await cursor.fetchone())[0] == 1
