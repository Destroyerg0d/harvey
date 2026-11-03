"""The segment mix of first emails, the custom segment's ramp, the Segments
section of the Mailboxes tab, each segment spread across mailboxes, and the
contact buffer. Every business and address is fictional (.test)."""

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

import harvey.segment_mix as sm
from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.state import StateManager
from tests.test_outbox_native import Cfg, FakeProvider, make_sender as single_sender

BODY = "Hi there,\n\nHow do you take orders today?"


def _ago(**delta) -> str:
    return (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(**delta)).isoformat()


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        s = StateManager(os.path.join(tmp, "mix.db"))
        await s.init_db()
        yield s


async def lead(state, n, status="queued", mailbox="", company_id=""):
    return await state.add_prospect(Prospect(
        first_name="Jo", last_name="Example", title="Owner", company=f"Shop {n}",
        email=f"jo@shop{n}.test", email_status="verified", status=status,
        mailbox=mailbox, company_id=company_id))


async def first(state, n, segment, minutes=60, mailbox=None, company_id=""):
    """A due, approved first email to a new lead at shop{n}.test."""
    pid = await lead(state, n, mailbox=mailbox or "", company_id=company_id)
    item = await state.add_outbox_item(
        prospect_id=pid, to_email=f"jo@shop{n}.test", subject="taking orders", body=BODY,
        send_at=_ago(minutes=minutes), status="approved", kind="sequence", step=1,
        campaign_id=f"c{n}", approved_by="you", provider="fake", mailbox=mailbox)
    if segment:
        await state.update_outbox_item(item, segment=segment)
    return item


def weights(monkeypatch, value):
    monkeypatch.setattr(Cfg.channels.email, "segment_weights", value, raising=False)


def sent_to(provider) -> list[str]:
    return [s["to"] for s in provider.sent]


# ── Shares and the ramp's levels ──

def test_weights_are_scaled_to_add_up_to_one():
    assert sm.clean_weights({"custom": 40, "wordpress": 35, "shopify": 25}) == pytest.approx(
        {"custom": 0.40, "wordpress": 0.35, "shopify": 0.25})
    assert sm.clean_weights({"Custom": 2, "shopify": 0, "nonsense": 5, "other": "x"}) == {"custom": 1.0}
    assert sm.clean_weights({}) == {} and sm.clean_weights(None) == {}


def test_no_weights_in_the_config_means_no_mix():
    from tests.test_mailboxes import make_config
    assert sm.weights_of(make_config()) == {}
    assert sm.weights_of(Cfg) == {}


def test_the_config_rejects_a_segment_it_doesnt_know():
    from pydantic import ValidationError
    from tests.test_mailboxes import make_config
    cfg = make_config(segment_weights={"custom": 0.4, "wordpress": 0.35, "shopify": 0.25})
    assert cfg.channels.email.segment_weights == {"custom": 0.4, "wordpress": 0.35, "shopify": 0.25}
    with pytest.raises(ValidationError):
        make_config(segment_weights={"magento": 1})
    with pytest.raises(ValidationError):
        make_config(segment_weights={"custom": -1})


def test_custom_ramps_from_15_to_25_to_its_target():
    w = sm.clean_weights({"custom": 0.40, "wordpress": 0.35, "shopify": 0.25})
    assert sm.ramp_levels(0.40) == [0.15, 0.25, 0.40]
    assert sm.ramp_levels(0.20) == [0.15, 0.20]
    assert sm.ramp_levels(0.10) == [0.10]
    level0 = sm.effective_shares(w, 0)
    assert level0["custom"] == pytest.approx(0.15)
    # What custom doesn't use yet goes to the others, in their proportions.
    assert level0["wordpress"] == pytest.approx(0.35 * 0.85 / 0.60)
    assert level0["shopify"] == pytest.approx(0.25 * 0.85 / 0.60)
    assert sum(level0.values()) == pytest.approx(1.0)
    assert sm.effective_shares(w, 2) == pytest.approx(w)
    assert sm.effective_shares(w, 9) == pytest.approx(w)        # clamped to the top


# ── Ordering ──

def rows(*segments, kind="sequence", step=1, prefix="r"):
    return [{"id": f"{prefix}{i}", "kind": kind, "step": step, "segment": s}
            for i, s in enumerate(segments)]


def mix_of(shares, counts=None, ceilings=None, queue_rows=()):
    mix = sm.SegmentMix(shares, counts, ceilings)
    mix.segments.update({r["id"]: r["segment"] for r in queue_rows if r["segment"]})
    return mix


def test_follow_ups_and_replies_keep_their_place_ahead_of_first_emails():
    firsts = rows("shopify", "wordpress")
    follow = rows("shopify", step=2, prefix="f")
    reply = rows("", kind="reply", step=0, prefix="x")
    queue = firsts + follow + reply
    mix = mix_of({"wordpress": 0.5, "shopify": 0.5}, queue_rows=queue)
    [out] = mix.order([queue])
    assert [r["id"] for r in out[:2]] == ["f0", "x0"]
    assert sorted(r["id"] for r in out) == sorted(r["id"] for r in queue)   # only moved


def test_first_emails_go_to_the_segment_furthest_below_its_share():
    queue = rows("shopify", "shopify", "shopify", "shopify", "wordpress", "wordpress")
    mix = mix_of({"wordpress": 0.5, "shopify": 0.5}, queue_rows=queue)
    [out] = mix.order([queue])
    assert [r["segment"] for r in out[:4]] == ["shopify", "wordpress", "shopify", "wordpress"]
    # Today's sends count: wordpress is already behind, so it goes first.
    mix = mix_of({"wordpress": 0.5, "shopify": 0.5}, {"shopify": 3}, queue_rows=queue)
    assert [r["segment"] for r in mix.order([queue])[0][:3]] == ["wordpress", "wordpress", "shopify"]


def test_a_segment_with_nothing_due_leaves_its_share_to_the_others():
    queue = rows("shopify", "shopify", "shopify", "wordpress", "other", "unknown")
    mix = mix_of({"custom": 0.4, "wordpress": 0.35, "shopify": 0.25}, queue_rows=queue)
    out = [r["segment"] for r in mix.order([queue])[0]]
    assert out[:4] == ["wordpress", "shopify", "shopify", "shopify"]
    # Segments without a weight only after every weighted one, in queue order.
    assert out[4:] == ["other", "unknown"]


def test_a_ramping_segment_waits_at_its_ceiling():
    queue = rows("custom", "custom", "custom", "shopify", "shopify", "shopify")
    mix = mix_of({"custom": 0.15, "shopify": 0.85}, ceilings={"custom": 0.15}, queue_rows=queue)
    out = [r["segment"] for r in mix.order([queue])[0]]
    # One custom email may start the day; more only as the others make room.
    assert out == ["shopify", "shopify", "shopify", "custom", "custom", "custom"]
    assert mix.allows(queue[0])
    mix.record(queue[0])                                  # one custom sent
    assert not mix.allows(queue[1])
    other = {"id": "s", "kind": "sequence", "step": 1}
    for _ in range(11):
        mix.record(other)                                 # 12 first emails today
    assert not mix.allows(queue[1])                       # a second would be 2 of 13: over 15%
    mix.record(other)
    assert mix.allows(queue[1])                           # 2 of 14 is under 15%
    assert mix.allows(rows("", kind="reply", step=0)[0])  # anything else always may


def test_mailboxes_share_one_count_and_their_own_limits():
    a = rows("shopify", "shopify", "shopify", "wordpress", prefix="a")
    b = rows("wordpress", "wordpress", prefix="b")
    mix = mix_of({"wordpress": 0.5, "shopify": 0.5}, queue_rows=a + b)
    out_a, out_b = mix.order([a, b], limits=[3, 1])
    assert [r["segment"] for r in out_a[:3]] == ["shopify", "shopify", "wordpress"]
    assert [r["id"] for r in out_b] == ["b0", "b1"]


# ── The drain ──

@pytest.mark.asyncio
async def test_without_weights_the_drain_goes_in_queue_order(state):
    sender = single_sender(state, FakeProvider())
    for n, segment in enumerate(["shopify", "shopify", "wordpress"]):
        await first(state, n, segment, minutes=60 - n)
    await sender._drain_due()
    assert sent_to(sender.provider) == ["jo@shop0.test", "jo@shop1.test", "jo@shop2.test"]


@pytest.mark.asyncio
async def test_the_drain_sends_by_the_mix_inside_the_daily_cap(state, monkeypatch):
    weights(monkeypatch, {"wordpress": 0.5, "shopify": 0.5})
    monkeypatch.setattr(Cfg.channels.email, "max_daily_sends", 3)
    sender = single_sender(state, FakeProvider())
    for n, segment in enumerate(["shopify", "shopify", "shopify", "wordpress"]):
        await first(state, n, segment, minutes=60 - n)
    await sender._drain_due()
    assert sent_to(sender.provider) == ["jo@shop0.test", "jo@shop3.test", "jo@shop1.test"]
    assert (await state.count_outbox_sent_today()) == 3            # the cap still holds


@pytest.mark.asyncio
async def test_a_due_follow_up_goes_before_any_first_email(state, monkeypatch):
    weights(monkeypatch, {"wordpress": 0.5, "shopify": 0.5})
    monkeypatch.setattr(Cfg.channels.email, "max_daily_sends", 2)
    sender = single_sender(state, FakeProvider())
    for n in range(3):
        await first(state, n, "shopify", minutes=120 - n)
    # Someone emailed three days ago, their step 2 due a minute ago.
    pid = await lead(state, 9, status="contacted")
    one = await state.add_outbox_item(
        prospect_id=pid, to_email="jo@shop9.test", subject="taking orders", body=BODY,
        send_at=_ago(days=3), status="approved", kind="sequence", step=1, campaign_id="c9",
        approved_by="you", provider="fake")
    await state.update_outbox_item(one, status="sent", sent_at=_ago(days=3))
    await state.add_outbox_item(
        prospect_id=pid, to_email="jo@shop9.test", subject="re: taking orders", body=BODY,
        send_at=_ago(minutes=1), status="approved", kind="sequence", step=2, campaign_id="c9",
        approved_by="you", provider="fake")
    await sender._drain_due()
    assert sent_to(sender.provider) == ["jo@shop9.test", "jo@shop0.test"]


@pytest.mark.asyncio
async def test_a_row_without_a_stamp_takes_its_companys_profile(state, monkeypatch):
    from harvey.signals import seed_signal_catalog
    await seed_signal_catalog(state)
    weights(monkeypatch, {"wordpress": 0.9, "shopify": 0.1})
    monkeypatch.setattr(Cfg.channels.email, "max_daily_sends", 1)
    cid = await state.add_company(Company(name="Shop 5", domain="shop5.test"))
    await state.add_observations([{"signal_code": "SITE_PLATFORM", "company_id": cid,
                                   "collector": "profile", "value_text": "woocommerce"}])
    sender = single_sender(state, FakeProvider())
    await first(state, 0, "shopify", minutes=90)
    await first(state, 5, "", minutes=10, company_id=cid)
    found = await sm.queued_segments(state.db_path, await state.get_outbox(status="approved"))
    assert sorted(found.values()) == ["shopify", "wordpress"]
    await sender._drain_due()
    assert sent_to(sender.provider) == ["jo@shop5.test"]


@pytest.mark.asyncio
async def test_custom_waits_at_its_ramp_ceiling_but_not_once_ramped(state, monkeypatch):
    weights(monkeypatch, {"custom": 0.4, "shopify": 0.6})
    sender = single_sender(state, FakeProvider())
    for n in range(3):
        await first(state, n, "custom", minutes=100 - n)
    for n in range(3, 6):
        await first(state, n, "shopify", minutes=100 - n)
    await sender._drain_due()
    segments = {s["to"]: s for s in sender.provider.sent}
    assert len(segments) == 4                                   # three shopify, one custom
    held = await state.get_outbox(status="approved")
    assert [r["segment"] for r in held] == ["custom", "custom"]
    ramp = json.loads(await state.get_setting(sm.RAMP_KEY))
    assert ramp["level"] == 0 and ramp["since"]

    # At its target there is no ceiling: the rest go too.
    await state.set_setting(sm.RAMP_KEY, json.dumps({"level": 2, "since": _ago(days=1)}))
    await single_sender(state, sender.provider)._drain_due()
    assert await state.get_outbox(status="approved") == []


# ── The ramp ──

async def custom_sent(state, n, bounced=0, days_ago=1):
    """n custom emails sent ``days_ago``, the first ``bounced`` of them bounced."""
    for i in range(n):
        pid = await lead(state, f"r{days_ago}-{i}", status="contacted")
        item = await state.add_outbox_item(
            prospect_id=pid, to_email=f"jo@r{days_ago}-{i}.test", subject="s", body=BODY,
            send_at=_ago(days=days_ago), status="approved", kind="sequence", step=1,
            campaign_id="cr", approved_by="you", provider="fake")
        await state.update_outbox_item(item, status="sent", sent_at=_ago(days=days_ago),
                                       segment="custom")
        if i < bounced:
            await state.log_action(action_type="bounce", agent="handler",
                                   details={"prospect": "x", "outbox_id": item})


async def ramp_at(state, level, days):
    await state.set_setting(sm.RAMP_KEY, json.dumps({"level": level, "since": _ago(days=days)}))


W = sm.clean_weights({"custom": 0.40, "wordpress": 0.35, "shopify": 0.25})


@pytest.mark.asyncio
async def test_the_ramp_starts_at_its_first_level(state):
    ramp = await sm.update_ramp(state, W)
    assert ramp["level"] == 0 and ramp["history"] == []
    assert json.loads(await state.get_setting(sm.RAMP_KEY))["since"] == ramp["since"]


@pytest.mark.asyncio
async def test_a_clean_week_with_enough_sends_steps_up(state):
    await ramp_at(state, 0, days=8)
    await custom_sent(state, 40, bounced=0, days_ago=2)
    ramp = await sm.update_ramp(state, W)
    assert ramp["level"] == 1
    [change] = ramp["history"]
    assert (change["from"], change["to"], change["sent"], change["bounced"]) == (0.15, 0.25, 40, 0)
    # The next step needs a clean week of its own.
    assert (await sm.update_ramp(state, W))["level"] == 1
    stored = json.loads(await state.get_setting(sm.RAMP_KEY))
    assert stored["level"] == 1 and len(stored["history"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("sent, bounced, days, why", [
    (39, 0, 8, "under 40 sends"),
    (40, 0, 5, "under a week at this level"),
    (50, 1, 8, "2.0% is not under 2%"),
])
async def test_the_ramp_holds_without_its_minimums(state, sent, bounced, days, why):
    await ramp_at(state, 0, days=days)
    await custom_sent(state, sent, bounced=bounced, days_ago=1)
    assert (await sm.update_ramp(state, W))["level"] == 0, why


@pytest.mark.asyncio
async def test_old_sends_outside_the_window_dont_count(state):
    await ramp_at(state, 0, days=30)
    await custom_sent(state, 40, bounced=0, days_ago=10)
    assert (await sm.update_ramp(state, W))["level"] == 0


@pytest.mark.asyncio
async def test_bounces_over_3_percent_step_down(state):
    await ramp_at(state, 2, days=2)
    await custom_sent(state, 20, bounced=1, days_ago=1)          # 5%
    ramp = await sm.update_ramp(state, W)
    assert ramp["level"] == 1 and ramp["history"][-1]["to"] == 0.25
    assert "over 3%" in ramp["history"][-1]["why"]
    # The same bounces don't step it down again: the clock restarted.
    assert (await sm.update_ramp(state, W))["level"] == 1


@pytest.mark.asyncio
async def test_too_few_sends_to_judge_a_step_down(state):
    await ramp_at(state, 1, days=2)
    await custom_sent(state, 5, bounced=1, days_ago=1)           # 20%, but 5 sends
    assert (await sm.update_ramp(state, W))["level"] == 1


@pytest.mark.asyncio
async def test_no_custom_weight_no_ramp(state):
    ramp = await sm.update_ramp(state, sm.clean_weights({"shopify": 1}))
    assert ramp["level"] == 0 and await state.get_setting(sm.RAMP_KEY) == ""


# ── The Mailboxes tab ──

@pytest.mark.asyncio
async def test_bounces_count_against_the_segment_of_the_email_they_name(state):
    from harvey.mailbox_health import collect
    from tests.test_mailboxes import make_config
    await custom_sent(state, 10, bounced=2, days_ago=1)
    for n in range(4):
        item = await first(state, 20 + n, "shopify")
        await state.update_outbox_item(item, status="sent", sent_at=_ago(minutes=5))
    await state.log_action(action_type="bounce", agent="handler",
                           details={"prospect": "y", "total_sent": 3})   # names no email
    cfg = make_config(segment_weights={"custom": 0.4, "wordpress": 0.35, "shopify": 0.25})
    report = await collect(state.db_path, cfg)
    seg = report["segments"]
    by = {r["segment"]: r for r in seg["rows"]}
    assert (by["custom"]["sent_total"], by["custom"]["bounces_total"]) == (10, 2)
    assert by["custom"]["bounce_rate"] == 0.2
    assert (by["shopify"]["sent_total"], by["shopify"]["bounces_total"]) == (4, 0)
    assert by["shopify"]["first_today"] == 4 and by["shopify"]["actual_today"] == 1.0
    assert by["wordpress"]["sent_total"] == 0 and by["wordpress"]["target"] == pytest.approx(0.35)
    assert by["custom"]["target"] == pytest.approx(0.40)
    assert by["custom"]["effective"] == pytest.approx(0.15)      # the ramp's first level
    assert seg["unattributed_bounces"] == 1 and seg["mix_active"]
    assert seg["ramp"]["share"] == pytest.approx(0.15) and seg["ramp"]["ramping"]


@pytest.mark.asyncio
async def test_without_weights_the_tab_still_shows_each_segment(state):
    from harvey.mailbox_health import collect
    from tests.test_mailboxes import make_config
    await custom_sent(state, 3, bounced=1, days_ago=1)
    seg = (await collect(state.db_path, make_config()))["segments"]
    assert not seg["mix_active"] and seg["ramp"] is None
    [row] = seg["rows"]
    assert row["segment"] == "custom" and row["target"] is None and row["bounces_total"] == 1


# ── Each segment spread across the mailboxes ──

@pytest.mark.asyncio
async def test_each_mailbox_gets_a_share_of_every_segment(state, tmp_path, monkeypatch):
    import harvey.integrations.gmail as gmail
    from tests.test_multi_mailbox_send import BOXES, config, connect, fakes, make_writer
    monkeypatch.setattr(gmail, "TOKEN_FILE", tmp_path / "gmail_token.json")
    monkeypatch.setattr(gmail, "TOKENS_DIR", tmp_path / "gmail_tokens")
    cfg = config(BOXES[:2])
    connect(cfg, BOXES[0]["email"], BOXES[1]["email"])
    shops = [await state.get_prospect(await lead(state, f"s{i}", status="new")) for i in range(4)]
    olds = [await state.get_prospect(await lead(state, f"o{i}", status="new")) for i in range(4)]
    out = await make_writer(state, cfg, fakes())._assign_mailboxes(
        {"Shopify-outreach": shops, "Custom-outreach": olds})
    for group in out.values():
        boxes = [(await state.get_prospect(p.id)).mailbox for p in group]
        assert sorted(boxes) == sorted([BOXES[0]["email"]] * 2 + [BOXES[1]["email"]] * 2), boxes


# ── The contact buffer ──

def test_the_buffer_is_two_days_of_first_emails():
    from harvey.main import CONTACT_BUFFER, contact_buffer
    from tests.test_mailboxes import make_config
    six = [{"email": f"box{i}@sender-one.test", "name": f"Box {i}", "daily_limit": 30}
           for i in range(6)]
    assert contact_buffer(make_config(six, persona_email="box0@sender-one.test")) == 120
    assert contact_buffer(make_config(max_daily_sends=50)) == CONTACT_BUFFER   # never under 60
    assert contact_buffer(make_config(max_daily_sends=300)) == 200


def test_keep_scouting_reads_the_buffer_it_is_given():
    from harvey.main import keep_scouting
    summary = {"companies_needing_prospects": 3, "writable_prospects": 80,
               "prospects": {"new": 80}}
    assert keep_scouting(summary, 120)
    assert not keep_scouting(summary, 60)
    assert not keep_scouting(summary, 0)


class VerifierEnv:
    bounceless_api_key = "test-key"
    emailverify_api_key = ""


@pytest.mark.asyncio
async def test_the_buffer_stops_growing_when_the_verifiers_are_nearly_used(state, monkeypatch):
    from harvey.integrations import email_finder as ef
    from harvey.main import VERIFIER_RESERVE, scouting_buffer, verifier_budget_low
    from tests.test_mailboxes import make_config
    for name in list(os.environ):
        if name.endswith(("_MONTHLY_CHECKS", "_DAILY_CHECKS")):
            monkeypatch.delenv(name, raising=False)
    cfg = make_config(max_daily_sends=300)
    assert not await verifier_budget_low(VerifierEnv(), state)
    assert await scouting_buffer(cfg, VerifierEnv(), state) == 200
    used = ef.VERIFIER_ALLOWANCES["bounceless"][1] - VERIFIER_RESERVE + 1
    await state.set_setting(ef._verifier_usage_key("bounceless"), str(used))
    assert await verifier_budget_low(VerifierEnv(), state)
    assert await scouting_buffer(cfg, VerifierEnv(), state) == 0
    # With no counted verifier set up there is nothing to go on.
    assert not await verifier_budget_low(object(), state)
