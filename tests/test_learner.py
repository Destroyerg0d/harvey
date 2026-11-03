"""The learner: data minimums before anything is proposed, one Brain call,
every answer checked (kind, text, evidence recomputed), the daily timer, the
run lock, and `harvey lessons`.

The Brain is a stand-in; every person and business is invented."""

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import aiosqlite
import pytest
import pytest_asyncio

from harvey import lessons
from harvey.agents import learner as L
from harvey.models.conversation import Conversation, Message
from harvey.models.prospect import Prospect
from harvey.state import StateManager

NOW = datetime(2026, 11, 4, 12, 0, 0)


@pytest.fixture(autouse=True)
def _fresh_registry():
    saved = dict(lessons._registry)
    lessons._registry.update(db_path="", texts=(), read_at=0.0)
    yield
    lessons._registry.clear()
    lessons._registry.update(saved)


@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(str(tmp_path / "learner.db"))
    await s.init_db()
    return s


class LearnBrain:
    def __init__(self, reply=None, paused="", within_budget=True, preflight=""):
        self.reply, self.prompts = reply, []
        self.paused_reason = paused
        self.last_reply_empty = False
        self._budget, self._preflight = within_budget, preflight

    def load_prompt(self, name, **kw):
        assert name == "learner"
        return f"LEARNER PROMPT for {kw['persona_company']}"

    async def preflight(self):
        return self._preflight

    async def is_within_budget(self, max_calls, max_percent=None):
        return self._budget

    async def think_json(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.reply


CONFIG = SimpleNamespace(usage=SimpleNamespace(max_daily_claude_percent=80),
                         persona=SimpleNamespace(company="Example Studio"),
                         product=SimpleNamespace(name="Rebuilds"))


def row(segment="shopify", angle="tools", step=1, sent=0, human_replies=0, positive=0, **kw):
    return {"channel": "email", "segment": segment, "angle": angle, "step": step,
            "mailbox": "primary", "country": "unknown", "sent": sent, "human_replies": human_replies,
            "positive": positive, "not_interested": 0, "opt_outs": 0, "bounces": 0, **kw}


def make_data(rows=(), variants=(), li_cells=(), edits=()):
    li = {"funnel": {}, "cells": list(li_cells)}
    cells = {**L.email_cells(list(rows)), **L.subject_cells(list(variants)), **L.linkedin_cells(li)}
    return {"rows": list(rows), "variants": list(variants), "linkedin": li, "cells": cells,
            "groups": L.comparable_groups(cells), "segments": L.big_segments(cells),
            "edits": list(edits), "feedback": [], "replies": {"human": 3, "automatic": 9},
            "segment_weights": None}


def learner_for(state, brain, data, now=NOW):
    async def fake_gather(_state, now=None):
        return data
    return L.Learner(brain, state, CONFIG, now=lambda: now, gather_fn=fake_gather)


ENOUGH = [row("shopify", "tools", sent=60, human_replies=2), row("wordpress", "tools", sent=40, human_replies=1),
          row("shopify", "description", sent=45, human_replies=1)]


def cand(kind, text, *cells, **evidence):
    return {"kind": kind, "text": text, "evidence": {"cells": list(cells), **evidence}}


# ── The minimums ──


def test_a_comparison_needs_forty_sends_on_each_side():
    groups = L.comparable_groups(L.email_cells([row(angle="tools", sent=40, human_replies=2),
                                                 row(angle="description", sent=39, human_replies=5)]))
    assert not groups["email.angle"]["ok"] and "40 sends" in groups["email.angle"]["why"]
    groups = L.comparable_groups(L.email_cells([row(angle="tools", sent=40, human_replies=2),
                                                 row(angle="description", sent=40, human_replies=1)]))
    assert groups["email.angle"]["ok"]


def test_a_comparison_needs_three_replies_from_people_between_the_sides():
    groups = L.comparable_groups(L.email_cells([row(angle="tools", sent=200, human_replies=1),
                                                 row(angle="description", sent=200, human_replies=1)]))
    assert not groups["email.angle"]["ok"] and "3 replies" in groups["email.angle"]["why"]


@pytest.mark.asyncio
async def test_below_every_minimum_it_records_not_enough_data_and_asks_nothing(state):
    brain = LearnBrain(reply={"lessons": [cand("writing", "Keep it plain.", "email.angle:tools")]})
    data = make_data([row(angle="tools", sent=39, human_replies=3), row(angle="description", sent=60)],
                     edits=[{"kind": "sequence"}] * 2)
    report = await learner_for(state, brain, data).run()
    assert report["status"] == "not_enough_data" and report["note"].startswith("Not enough data yet")
    assert brain.prompts == []
    assert await lessons.list_lessons(state) == []
    stored = json.loads(await state.get_setting(L.LAST_RUN_KEY))
    assert stored["status"] == "not_enough_data" and stored["at"].startswith("2026-11-04")


@pytest.mark.asyncio
async def test_a_lesson_that_cites_a_cell_below_the_minimums_is_left_out(state):
    brain = LearnBrain(reply={"lessons": [
        # description has 45 sends, but this cites the 39-send step cell.
        cand("writing", "Keep the second email shorter than the first one.", "email.step:1", "email.step:2"),
        cand("writing", "Lead with the tool you saw on their site.", "email.angle:tools"),   # one side only
    ]})
    data = make_data([*ENOUGH, row("shopify", "tools", step=2, sent=39, human_replies=3)])
    report = await learner_for(state, brain, data).run()
    assert report["proposed"] == 0 and report["left_out"] == 2
    assert await lessons.list_lessons(state) == []


@pytest.mark.asyncio
async def test_segment_mix_and_targeting_need_a_hundred_sends_in_each_segment(state):
    rows = [row("custom", "tools", sent=120, human_replies=3), row("shopify", "tools", sent=99, human_replies=1),
            row("wordpress", "description", sent=150, human_replies=1)]
    weights = {"custom": 0.5, "shopify": 0.2, "wordpress": 0.3}
    brain = LearnBrain(reply={"lessons": [
        cand("segment_mix", "Send more first emails to old custom sites than to Shopify stores.",
             "email.segment:custom", "email.segment:shopify", weights=weights),
        cand("targeting", "Shopify stores are worth fewer of the first emails for now.", "email.segment:shopify"),
        cand("segment_mix", "Send more first emails to old custom sites than to WordPress sites.",
             "email.segment:custom", "email.segment:wordpress", weights=weights),
    ]})
    report = await learner_for(state, brain, make_data(rows)).run()
    assert report["proposed"] == 1 and report["left_out"] == 2
    [lesson] = await lessons.list_lessons(state)
    assert lesson["kind"] == "segment_mix" and lesson["evidence"]["weights"] == weights
    assert {c["key"] for c in lesson["evidence"]["cells"]} == {"email.segment:custom", "email.segment:wordpress"}


# ── One Brain call, every answer checked ──


@pytest.mark.asyncio
async def test_it_proposes_what_passes_with_evidence_recomputed_from_the_data(state):
    await state.add_prospect(Prospect(first_name="Dana", last_name="Fictional", company="Bramblewood Pottery",
                                      email="dana@bramblewood.example", email_status="verified"))
    brain = LearnBrain(reply={"note": "Tools openers did better.", "lessons": [
        cand("writing", "Open with a tool you saw on their site rather than their own description.",
             "email.angle:tools", "email.angle:description", sent=99999),          # its number is ignored
        cand("writing", "Openers about tools get 3 times the replies.", "email.angle:tools", "email.angle:description"),
        cand("writing", "Write to owners the way Bramblewood Pottery was written to.",
             "email.angle:tools", "email.angle:description"),
        cand("writing", "Say we have rebuilt many shops like theirs.", "email.angle:tools", "email.angle:description"),
        cand("nonsense", "Plain guidance with an unknown kind.", "email.angle:tools", "email.angle:description"),
    ]})
    report = await learner_for(state, brain, make_data(ENOUGH)).run()
    assert len(brain.prompts) == 1
    prompt = brain.prompts[0]
    assert prompt.startswith("LEARNER PROMPT for Example Studio")
    assert "email.angle:tools: sent 100" in prompt and "email.angle:description: sent 45" in prompt
    assert report["status"] == "proposed" and report["proposed"] == 1 and report["left_out"] == 4
    [lesson] = await lessons.list_lessons(state)
    assert lesson["status"] == "proposed" and lesson["kind"] == "writing"
    e = lesson["evidence"]
    assert e["sent"] == 145 and e["human_replies"] == 4 and e["window_days"] == L.WINDOW_DAYS
    assert {c["key"] for c in e["cells"]} == {"email.angle:tools", "email.angle:description"}
    stored = await L.last_run(state)
    assert stored["status"] == "proposed" and stored["model_note"] == "Tools openers did better."


@pytest.mark.asyncio
async def test_at_most_five_answers_are_read(state):
    texts = ["Open with the tool you saw on their site.", "Keep the first email under a few lines.",
             "Ask one question and nothing more.", "Name the page you looked at.",
             "Close with a plain question about their week.", "A sixth lesson that is never read at all."]
    brain = LearnBrain(reply={"lessons": [cand("writing", t, "email.angle:tools", "email.angle:description")
                                          for t in texts]})
    report = await learner_for(state, brain, make_data(ENOUGH)).run()
    assert report["proposed"] == 5
    assert "never read" not in " ".join(l["text"] for l in await lessons.list_lessons(state))


@pytest.mark.asyncio
async def test_a_subject_lesson_is_only_proposed_with_its_honest_sample_size(state):
    variants = [{"campaign_id": "c1", "campaign": "Fictional shops", "a": {"sent": 60, "human_replies": 2},
                 "b": {"sent": 55, "human_replies": 1}}]
    brain = LearnBrain(reply={"lessons": [
        cand("subject", "Subjects that name their shop seem to do a little better.",
             "subject.variant:a", "subject.variant:b")]})
    report = await learner_for(state, brain, make_data(variants=variants)).run()
    assert report["proposed"] == 1
    [lesson] = await lessons.list_lessons(state)
    assert lesson["status"] == "proposed"          # never applied on its own
    assert lesson["evidence"]["note"].startswith("A small sample: Needs about")
    assert lesson["evidence"]["needed_per_variant"] > 1000


@pytest.mark.asyncio
async def test_lessons_from_the_owners_edits_need_three_edits(state):
    lesson = cand("writing", "Cut the second paragraph; the owner always does.", source="owner_edits")
    lesson["evidence"].pop("cells")
    two = [{"kind": "sequence", "step": 1, "old_subject": "a", "new_subject": "b", "old_body": "x",
            "new_body": "y"}] * 2
    report = await learner_for(state, LearnBrain(reply={"lessons": [lesson]}),
                               make_data(ENOUGH, edits=two)).run()
    assert report["proposed"] == 0
    report = await learner_for(state, LearnBrain(reply={"lessons": [lesson]}),
                               make_data(ENOUGH, edits=two * 2), now=NOW + timedelta(days=2)).run()
    assert report["proposed"] == 1
    [stored] = await lessons.list_lessons(state)
    assert stored["evidence"] == {"source": "owner_edits", "edits": 4, "window_days": L.WINDOW_DAYS}


@pytest.mark.asyncio
async def test_linkedin_lessons_rest_on_linkedin_cells(state):
    li = [L._cell("linkedin.message", "first", sent=50, human_replies=3, responses=3, response_label="replies"),
          L._cell("linkedin.message", "follow_up", sent=40, human_replies=1, responses=1, response_label="replies")]
    brain = LearnBrain(reply={"lessons": [
        cand("linkedin", "On LinkedIn, the first message does the work; keep the follow-up very short.",
             "linkedin.message:first", "linkedin.message:follow_up"),
        cand("linkedin", "On LinkedIn, lead with the tool you saw.", "email.angle:tools", "email.angle:description"),
    ]})
    report = await learner_for(state, brain, make_data(ENOUGH, li_cells=li)).run()
    assert report["proposed"] == 1
    [lesson] = await lessons.list_lessons(state)
    assert lesson["kind"] == "linkedin" and lesson["evidence"]["sent"] == 90


@pytest.mark.asyncio
async def test_an_unusable_answer_is_recorded_and_nothing_is_proposed(state):
    brain = LearnBrain(reply=None)
    report = await learner_for(state, brain, make_data(ENOUGH)).run()
    assert report["status"] == "failed" and await lessons.list_lessons(state) == []


# ── When it runs ──


@pytest.mark.asyncio
async def test_it_skips_while_the_brain_is_unavailable_or_over_budget(state):
    for brain in (LearnBrain(paused="Codex login expired"), LearnBrain(preflight="CLI too old"),
                  LearnBrain(within_budget=False)):
        report = await learner_for(state, brain, make_data(ENOUGH)).run()
        assert report["status"] == "skipped" and brain.prompts == []
    assert await state.get_setting(L.LAST_RUN_KEY) == ""     # not recorded: the next cycle tries again
    assert await state.get_setting(L.RUNNING_KEY) == ""      # and the lock is released


@pytest.mark.asyncio
async def test_never_two_runs_at_once(state):
    first = learner_for(state, LearnBrain(reply={"lessons": []}), make_data(ENOUGH))
    assert await first._claim()
    report = await learner_for(state, LearnBrain(reply={"lessons": []}), make_data(ENOUGH)).run()
    assert report["status"] == "busy"
    # A lock left by a crashed run goes stale after half an hour.
    later = learner_for(state, LearnBrain(reply={"lessons": []}), make_data(ENOUGH),
                        now=NOW + timedelta(minutes=31))
    assert (await later.run())["status"] == "nothing_new"


def test_the_daily_timer_starts_one_run_a_day(tmp_path):
    async def go():
        state = StateManager(str(tmp_path / "timer.db"))
        await state.init_db()
        clock = {"now": NOW}
        brain = LearnBrain(reply={"lessons": []})
        runner = L.LearnerRunner(brain, state, CONFIG, learner_factory=lambda: learner_for(
            state, brain, make_data(ENOUGH), now=clock["now"]))
        # Quiet hours: no run, but the leak guard's view of lessons is refreshed.
        assert await runner.maybe_start(start=False) == "not now"
        assert lessons._registry["db_path"] == state.db_path and brain.prompts == []
        assert await runner.maybe_start() == "started"
        assert await runner.maybe_start() == "a run is going"
        await runner.task
        assert len(brain.prompts) == 1
        clock["now"] = NOW + timedelta(hours=23)
        assert await runner.maybe_start() == "not due"
        clock["now"] = NOW + timedelta(hours=24, minutes=1)
        assert await runner.maybe_start() == "started"
        await runner.task
        assert len(brain.prompts) == 2
        # A paused brain doesn't start one, even when due.
        clock["now"] = NOW + timedelta(days=3)
        brain.paused_reason = "usage limit reached"
        assert await runner.maybe_start() == "the brain is paused"
        await runner.shutdown()

    asyncio.run(go())


# ── Reading the data ──


async def _sent(db, i, angle, variant="a", sent_at=None):
    await db.execute(
        "INSERT INTO outbox (id, campaign_id, prospect_id, step, kind, to_email, subject, body, status, "
        "sent_at, segment, angle, variant) VALUES (?, 'c1', ?, 1, 'sequence', ?, 's', 'b', 'sent', ?, "
        "'shopify', ?, ?)",
        (f"o-{angle}-{i}", f"p-{angle}-{i}", f"x{i}@{angle}.example",
         (sent_at or NOW - timedelta(days=5)).isoformat(), angle, variant))


@pytest.mark.asyncio
async def test_gather_reads_the_outcome_table_and_subject_variants(state):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with aiosqlite.connect(state.db_path) as db:
        for i in range(40):
            await _sent(db, i, "tools", "a" if i % 2 else "b", now - timedelta(days=5))
            await _sent(db, i, "description", "a" if i % 2 else "b", now - timedelta(days=5))
        await db.execute("INSERT INTO campaigns (id, name) VALUES ('c1', 'Fictional shops')")
        await db.commit()
    for pid, intent, status in (("p-tools-1", "question", "open"), ("p-tools-2", "interested", "open"),
                                ("p-description-3", "question", "open"), ("p-tools-4", "ooo", "auto")):
        await state.add_conversation(Conversation(
            id=f"c-{pid}", prospect_id=pid, intent=intent, status=status,
            thread=[Message(sender="prospect", content="Tell me more.", timestamp=now - timedelta(days=2))],
            created_at=now - timedelta(days=2), updated_at=now - timedelta(days=2)))
    data = await L.gather(state, now=now)
    assert data["cells"]["email.angle:tools"]["sent"] == 40
    assert data["cells"]["email.angle:tools"]["human_replies"] == 2       # the auto-reply never counts
    assert data["groups"]["email.angle"]["ok"]
    assert data["replies"] == {"human": 3, "automatic": 1}
    [campaign] = data["variants"]
    assert campaign["campaign"] == "Fictional shops"
    assert campaign["a"]["sent"] + campaign["b"]["sent"] == 80
    assert campaign["a"]["human_replies"] + campaign["b"]["human_replies"] == 3
    assert "more sends per variant" in campaign["progress"]["note"]


@pytest.mark.asyncio
async def test_linkedin_outcomes_count_what_came_back_to_each_kind(state):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    accepted, _ = await state.li_upsert_lead("lee-example", name="Lee Example", stage="replied",
                                             accepted_at=now - timedelta(days=9))
    pending, _ = await state.li_upsert_lead("kim-example", name="Kim Example", stage="invited")
    for lead, body in ((accepted, "Fellow shop owner here."), (pending, "")):
        await state.li_add_message(lead, kind="invite_note", body=body, status="sent",
                                   sent_at=now - timedelta(days=10))
    await state.li_add_message(accepted, kind="message", body="First one.", status="sent",
                               sent_at=now - timedelta(days=8))
    await state.li_add_message(accepted, kind="message", body="Follow-up.", status="sent",
                               sent_at=now - timedelta(days=3))
    await state.li_add_message(accepted, direction="in", kind="message", body="Sure, send it.",
                               status="sent", sent_at=now - timedelta(days=2))
    li = await L.linkedin_outcomes(state, now=now)
    cells = {c["key"]: c for c in li["cells"]}
    assert cells["linkedin.invite:note"]["accepted"] == 1 and cells["linkedin.invite:no_note"]["accepted"] == 0
    assert cells["linkedin.message:first"]["human_replies"] == 0
    assert cells["linkedin.message:follow_up"]["human_replies"] == 1     # the reply answered the follow-up
    assert li["funnel"]["invited"] == 2


def test_sends_needed_says_thousands_at_a_low_reply_rate():
    assert L.sends_needed(0.01) > 5000
    assert L.sends_needed(0.2) < L.sends_needed(0.01)
    note = L.variant_progress({"sent": 0}, {"sent": 0})
    assert note["assumed_rate"] and note["more_per_variant"] == note["needed_per_variant"]


# ── harvey lessons ──


def test_the_cli_lists_approves_rejects_retires_and_runs(tmp_path, monkeypatch, capsys):
    import harvey.cli
    import harvey.state
    monkeypatch.setattr(harvey.state, "DB_PATH", tmp_path / "cli.db")

    async def setup():
        state = StateManager()
        await state.init_db()
        a, _ = await lessons.propose(state, "writing", "Open with the tool you saw on their site.", {"sent": 140})
        b, _ = await lessons.propose(state, "writing", "Keep the follow-up to a couple of lines.", {"sent": 90})
        return a, b

    a, b = asyncio.run(setup())

    def cli(*argv):
        monkeypatch.setattr(sys, "argv", ["harvey", "lessons", *argv])
        harvey.cli.main()
        return capsys.readouterr().out

    out = cli()
    assert "2 waiting for you" in out and a in out and "140 sent" in out
    assert f"{a}: active" in cli("--approve", a)
    assert f"{b}: rejected" in cli("--reject", b)
    assert "not changed" in cli("--approve", b)
    assert f"{a}: retired" in cli("--retire", a)

    async def fake_run_once(state):
        return {"status": "not_enough_data", "note": "Not enough data yet: fictional."}
    monkeypatch.setattr(L, "run_once", fake_run_once)
    assert "Not enough data yet" in cli("--run")

    async def statuses():
        return {l["id"]: l["status"] for l in await lessons.list_lessons(StateManager())}
    assert asyncio.run(statuses()) == {a: "retired", b: "rejected"}
