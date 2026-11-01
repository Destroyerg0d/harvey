"""Several mailboxes, sending side: who gets which mailbox, the sign-off
tag, and the drain taking mailboxes in turn under their own limits. Without
mailboxes configured nothing here may change what Harvey does."""

import json
import os
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

import harvey.integrations.gmail as gmail
import harvey.mailboxes as mb
from harvey.integrations.mail_provider import MailProvider, SendResult
from harvey.models.prospect import Prospect
from harvey.state import HELD_FOR_REVIEW, MAYBE_SENT_PREFIX, StateManager
from tests.test_mailboxes import PRIMARY, SECOND, THIRD, make_config

# PRIMARY signs as Alex, SECOND as Sam, THIRD as Robin.
BOXES = [
    {"email": PRIMARY, "name": "Alex Example"},
    {"email": SECOND, "name": "Sam Sample"},
    {"email": THIRD, "name": "Robin"},
]
SIGNED = "Hi there,\n\nHow do you book jobs today?\n\n{{sender_first_name}}"


def _ago(**delta) -> str:
    return (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(**delta)).isoformat()


@pytest.fixture
def tokens(tmp_path, monkeypatch):
    """Token files under tmp_path, none signed in until connect()."""
    monkeypatch.setattr(gmail, "TOKEN_FILE", tmp_path / "gmail_token.json")
    monkeypatch.setattr(gmail, "TOKENS_DIR", tmp_path / "gmail_tokens")
    return tmp_path


@pytest_asyncio.fixture
async def state(tmp_path):
    sm = StateManager(str(tmp_path / "harvey.db"))
    await sm.init_db()
    return sm


def config(mailboxes=None, **email):
    email.setdefault("require_approval", False)
    return make_config(BOXES if mailboxes is None else mailboxes, **email)


def connect(cfg, *addresses):
    for address in addresses:
        path = mb.mailbox_for(cfg, address).token_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"refresh_token": "test-refresh"}), encoding="utf-8")


class BoxProvider(MailProvider):
    """One mailbox's fake Gmail: records sends, can refuse or raise."""
    name = "gmail"

    def __init__(self, address):
        self.address = address
        self.sent = []
        self.attempts = 0
        self.inbound = []
        self.result = None        # a SendResult to answer every send with
        self.error = None         # an exception to raise on send
        self.read_error = None    # an exception to raise on read
        self.read_delay = 0.0
        self.unavailable_reason = ""
        self.confirmed = 0
        self.widened = []

    def is_configured(self):
        return True

    async def send_email(self, to_email, subject, body, thread_ref="", in_reply_to=""):
        self.attempts += 1
        if self.error is not None:
            raise self.error
        if self.result is not None:
            return self.result
        self.sent.append({"to": to_email, "subject": subject, "body": body,
                          "thread_ref": thread_ref, "in_reply_to": in_reply_to})
        return SendResult(ok=True, message_id=f"<{self.address}-{len(self.sent)}@x.test>",
                          thread_ref=f"{self.address}-thread", status=200)

    async def get_replies(self, limit=50):
        if self.read_delay:
            import asyncio
            await asyncio.sleep(self.read_delay)
        if self.read_error is not None:
            raise self.read_error
        return list(self.inbound)

    def confirm_read(self):
        self.confirmed += 1

    def widen_next_read(self, days):
        self.widened.append(days)

    async def test_connection(self):
        return True, "fake"


class Env:
    instantly_api_key = ""
    gmail_client_id = "id"
    gmail_client_secret = "secret"


def fakes(*addresses):
    return {a: BoxProvider(a) for a in (addresses or (PRIMARY, SECOND, THIRD))}


def make_sender(state, cfg, providers):
    from harvey.agents.sender import Sender
    sender = Sender(brain=None, state=state, config=cfg, env=Env())
    sender.mailbox_providers.factory = lambda m: providers.setdefault(m.email, BoxProvider(m.email))
    sender.send_pacing = False
    return sender


async def person(state, email, mailbox="", status="queued", first="Jo"):
    return await state.add_prospect(Prospect(
        first_name=first, last_name="Example", title="Owner", company="Example Co",
        email=email, email_status="verified", status=status, mailbox=mailbox))


async def row(state, pid, to, *, mailbox=None, kind="sequence", step=1, campaign="c1",
              body=SIGNED, status="approved", approved_by="autopilot", due=_ago,
              thread_ref="", in_reply_to=""):
    return await state.add_outbox_item(
        prospect_id=pid, to_email=to, subject="booking jobs", body=body,
        send_at=due(minutes=5), status=status, kind=kind, step=step, campaign_id=campaign,
        mailbox=mailbox, approved_by=approved_by, thread_ref=thread_ref,
        in_reply_to=in_reply_to, provider="gmail")


async def sent(state, pid, to, mailbox, campaign, hours=1, kind="sequence"):
    item = await row(state, pid, to, mailbox=mailbox, campaign=campaign, kind=kind)
    await state.update_outbox_item(item, status="sent", sent_at=_ago(hours=hours),
                                   message_id=f"<{campaign}@x.test>")
    return item


# ── Who gets which mailbox ──

class WriterBrain:
    """Canned drafts; keeps every prompt and load_prompt's arguments."""

    def __init__(self, personal_body=SIGNED):
        self.personal_body = personal_body
        self.prompts = []
        self.prompt_args = []
        self.paused_reason = ""
        self.last_reply_empty = False

    def load_skills_for_agent(self, *_):
        return "SKILLS-MARKER"

    def load_prompt(self, name, **kw):
        self.prompt_args.append(kw)
        return f"Identity: {kw.get('persona_name')}"

    def load_skill(self, *_):
        return ""

    async def think_json(self, prompt, session_id=None, agent="", task=""):
        self.prompts.append((task, prompt))
        if task == "write_sequence":
            return [{"step": 1, "subject": "booking jobs", "body": SIGNED, "delay_days": 0},
                    {"step": 2, "subject": "one more", "body": "Still curious?\n\n{{sender_first_name}}",
                     "delay_days": 3}]
        return {"subject": "booking jobs", "body": self.personal_body}


def make_writer(state, cfg, providers, brain=None):
    from harvey.agents.writer import Writer
    writer = Writer(brain or WriterBrain(), state, cfg, Env())
    writer.mailbox_providers.factory = lambda m: providers.setdefault(m.email, BoxProvider(m.email))
    return writer


@pytest.mark.asyncio
async def test_the_personal_first_email_goes_out_from_the_prospects_mailbox(state, tokens):
    cfg = config()
    connect(cfg, PRIMARY, SECOND, THIRD)
    providers = fakes()
    pid = await person(state, "jo@roofco.test", mailbox=SECOND, status="new")
    await make_writer(state, cfg, providers).run()

    [first] = [r for r in await state.get_outbox(limit=50) if r["step"] == 1]
    assert first["mailbox"] == SECOND
    assert first["body"].endswith("{{sender_first_name}}")      # filled in when it sends

    await make_sender(state, cfg, providers)._run_native()
    [out] = providers[SECOND].sent
    assert out["to"] == "jo@roofco.test"
    assert "\n\nSam\n" in out["body"] and "{{" not in out["body"]
    assert providers[PRIMARY].sent == providers[THIRD].sent == []
    stored = await state.get_outbox_item(first["id"])
    assert stored["status"] == "sent" and "\n\nSam\n" in stored["body"]
    assert (await state.get_prospect(pid)).mailbox == SECOND


@pytest.mark.asyncio
async def test_two_new_colleagues_in_one_run_get_one_mailbox(state, tokens):
    cfg = config()
    connect(cfg, PRIMARY, SECOND, THIRD)
    a = await person(state, "jo@roofco.test", status="new")
    b = await person(state, "lee@roofco.test", status="new", first="Lee")
    other = await person(state, "kim@plumbco.test", status="new", first="Kim")
    await make_writer(state, cfg, fakes()).run()
    boxes = {pid: (await state.get_prospect(pid)).mailbox for pid in (a, b, other)}
    assert boxes[a] == boxes[b] != ""
    # The second company is chosen after the first is saved: another mailbox.
    assert boxes[other] not in ("", boxes[a])


@pytest.mark.asyncio
async def test_a_company_already_written_to_keeps_its_mailbox(state, tokens):
    cfg = config()
    connect(cfg, PRIMARY, SECOND, THIRD)
    # The primary is the busiest, so only history can send anyone there.
    for i in range(3):
        busy = await person(state, f"busy{i}@elsewhere.test")
        await row(state, busy, f"busy{i}@elsewhere.test", mailbox=PRIMARY, campaign=f"b{i}")
    # Emailed before there were mailboxes: "" is the primary.
    old = await person(state, "old@roofco.test", status="contacted")
    await sent(state, old, "old@roofco.test", "", "c-old")
    newcomer = await person(state, "new@roofco.test", status="new")
    # A colleague who is merely new, with "" and nothing sent, is no history:
    # it doesn't send its company to the primary.
    idle = await person(state, "idle@plumbco.test", status="new", first="Ida")
    fresh = await person(state, "fresh@plumbco.test", status="new", first="Fay")

    # Only the prospects being written for this cycle are given a mailbox.
    w = make_writer(state, cfg, fakes())
    batches = {"b": [await state.get_prospect(newcomer), await state.get_prospect(fresh)]}
    out = await w._assign_mailboxes(batches)
    assert [p.id for p in out["b"]] == [newcomer, fresh]
    assert (await state.get_prospect(newcomer)).mailbox == PRIMARY
    assert (await state.get_prospect(fresh)).mailbox not in ("", PRIMARY)
    assert (await state.get_prospect(old)).mailbox == ""        # history isn't rewritten
    assert (await state.get_prospect(idle)).mailbox == ""       # not written for this cycle


@pytest.mark.asyncio
async def test_the_least_busy_mailbox_is_chosen(state, tokens):
    cfg = config()
    a = await person(state, "a@one.test")
    await row(state, a, "a@one.test", mailbox=PRIMARY, campaign="x1")       # waiting: PRIMARY 2
    await row(state, a, "a@one.test", mailbox=PRIMARY, campaign="x2")
    b = await person(state, "b@two.test", status="contacted")
    await sent(state, b, "b@two.test", SECOND, "x3", hours=24 * 3)          # sent this week: SECOND 1
    await sent(state, b, "b@two.test", THIRD, "x4", hours=24 * 8)           # last week: THIRD 0
    boxes = mb.configured_mailboxes(cfg)
    assert (await mb.least_loaded(state, cfg, boxes)).email == THIRD
    assert (await mb.least_loaded(state, cfg, boxes[:2])).email == SECOND
    # A prospect given to a mailbox with nothing written yet counts too.
    await person(state, "c@three.test", mailbox=THIRD, status="new")
    await person(state, "d@four.test", mailbox=THIRD, status="new")
    assert (await mb.least_loaded(state, cfg, boxes)).email == SECOND


@pytest.mark.asyncio
async def test_with_no_mailbox_able_to_take_them_nothing_is_written(state, tokens):
    cfg = config()
    connect(cfg, SECOND)
    await state.set_setting(mb.paused_key(SECOND), "paused manually")
    brain = WriterBrain()
    pid = await person(state, "jo@roofco.test", status="new")
    await make_writer(state, cfg, fakes(), brain).run()
    p = await state.get_prospect(pid)
    assert (p.status, p.mailbox) == ("new", "")
    assert brain.prompts == [] and await state.get_outbox(limit=10) == []


@pytest.mark.asyncio
async def test_a_mailbox_down_or_wrong_account_takes_no_new_prospects(state, tokens):
    cfg = config()
    connect(cfg, PRIMARY, SECOND, THIRD)
    providers = fakes()
    providers[PRIMARY].unavailable_reason = "signed in as someone else"
    await state.set_setting(mb.down_since_key(SECOND), _ago(hours=1))
    pid = await person(state, "jo@roofco.test", status="new")
    await make_writer(state, cfg, providers).run()
    assert (await state.get_prospect(pid)).mailbox == THIRD


# ── The prompts ──

@pytest.mark.asyncio
async def test_multi_mailbox_prompts_sign_off_with_the_tag(state, tokens):
    cfg = config()
    connect(cfg, PRIMARY, SECOND, THIRD)
    brain = WriterBrain()
    await person(state, "jo@roofco.test", status="new")
    await make_writer(state, cfg, fakes(), brain).run()
    tasks = {task for task, _ in brain.prompts}
    assert tasks == {"write_sequence", "personal_email"}
    for _, prompt in brain.prompts:
        assert "only {{sender_first_name}}" in prompt
        assert "Never write a sender's name yourself" in prompt
        assert "Alex Example" not in prompt and "Sam Sample" not in prompt
    for kw in brain.prompt_args:
        assert kw["persona_name"].startswith("a member of the Example Co team")


@pytest.mark.asyncio
async def test_one_mailbox_prompts_are_unchanged(state, tokens):
    cfg = make_config()                       # no mailboxes
    brain = WriterBrain(personal_body="Hi Jo,\n\nHow do you book jobs today?")
    await person(state, "jo@roofco.test", status="new")
    await make_writer(state, cfg, {}, brain).run()
    assert brain.prompts
    assert all("sender_first_name" not in prompt for _, prompt in brain.prompts)
    assert all(kw["persona_name"] == "Alex Example" for kw in brain.prompt_args)
    assert (await state.get_prospects_by_status("queued"))[0].mailbox == ""


def test_operator_rules_never_name_the_sender_with_several_mailboxes():
    from harvey.agents.writer import operator_rules_block
    assert "sender_first_name" not in operator_rules_block("Sign off as Alex.")
    multi = operator_rules_block("Sign off as Alex.", multi=True)
    assert "write the line {{sender_first_name}} instead" in multi
    assert multi.endswith("Sign off as Alex.")


@pytest.mark.asyncio
async def test_an_inbox_batch_may_sign_off_with_the_tag():
    from tests.test_company_inbox import inbox, sequence_writer
    steps = [{"step": 1, "subject": "quick one",
              "body": "Hi there, who answers at {{company}}?\n\n{{sender_first_name}}",
              "delay_days": 0}]
    out = await sequence_writer(steps)._write_sequence([inbox(industry="Roofing")])
    assert len(out) == 1 and out[0].body.endswith("{{sender_first_name}}")


# ── The drain ──

@pytest.mark.asyncio
async def test_a_capped_mailboxs_backlog_does_not_block_another(state, tokens):
    cfg = config()
    connect(cfg, PRIMARY, SECOND)
    providers = fakes()
    # The primary used its 30 for the day, and 30 more are due before Sam's one.
    for i in range(30):
        pid = await person(state, f"p{i}@many.test", mailbox=PRIMARY)
        await sent(state, pid, f"p{i}@many.test", PRIMARY, f"done{i}", hours=2)
        await row(state, pid, f"p{i}@many.test", mailbox=PRIMARY, campaign=f"due{i}",
                  step=1, due=lambda **_: _ago(hours=3))
    sam = await person(state, "jo@roofco.test", mailbox=SECOND)
    await row(state, sam, "jo@roofco.test", mailbox=SECOND)
    await make_sender(state, cfg, providers)._drain_due()
    assert [s["to"] for s in providers[SECOND].sent] == ["jo@roofco.test"]
    assert providers[PRIMARY].attempts == 0


@pytest.mark.asyncio
async def test_mailboxes_take_turns_within_their_caps(state, tokens):
    cfg = config()
    connect(cfg, PRIMARY, SECOND, THIRD)
    providers = fakes()
    for address in (PRIMARY, SECOND, THIRD):
        for i in range(5):
            to = f"{address.split('@')[0]}{i}@leads.test"
            await row(state, await person(state, to, mailbox=address), to, mailbox=address,
                      campaign=f"{address}{i}")
    await make_sender(state, cfg, providers)._drain_due()
    assert [len(providers[a].sent) for a in (PRIMARY, SECOND, THIRD)] == [3, 3, 3]


@pytest.mark.asyncio
async def test_an_unconnected_or_paused_mailboxs_email_waits(state, tokens):
    cfg = config()
    connect(cfg, PRIMARY, THIRD)                 # SECOND isn't signed in
    providers = fakes()
    await state.set_setting(mb.paused_key(THIRD), "bounce rate too high")
    ids = {}
    for address in (PRIMARY, SECOND, THIRD):
        to = f"lead@{address.split('@')[0]}.test"
        ids[address] = await row(state, await person(state, to, mailbox=address), to,
                                 mailbox=address)
    await make_sender(state, cfg, providers)._drain_due()
    assert len(providers[PRIMARY].sent) == 1
    assert providers[SECOND].attempts == providers[THIRD].attempts == 0
    for address in (SECOND, THIRD):
        assert (await state.get_outbox_item(ids[address]))["status"] == "approved"
    # Not signed in counts towards being down; a pause doesn't.
    assert await state.get_setting(mb.down_since_key(SECOND))
    assert not await state.get_setting(mb.down_since_key(THIRD))


@pytest.mark.asyncio
async def test_each_mailbox_has_its_own_daily_limit(state, tokens):
    cfg = config([BOXES[0], {**BOXES[1], "daily_limit": 2}])
    connect(cfg, PRIMARY, SECOND)
    providers = fakes(PRIMARY, SECOND)
    sam = await person(state, "jo@roofco.test", mailbox=SECOND, status="contacted")
    await sent(state, sam, "jo@roofco.test", SECOND, "today", hours=3)
    await sent(state, sam, "jo@roofco.test", SECOND, "yesterday", hours=25)   # outside 24h
    for i in range(3):
        to = f"n{i}@leads.test"
        await row(state, await person(state, to, mailbox=SECOND), to, mailbox=SECOND,
                  campaign=f"n{i}")
    await make_sender(state, cfg, providers)._drain_due()
    assert len(providers[SECOND].sent) == 1


@pytest.mark.asyncio
async def test_replies_go_out_past_a_spent_limit_and_the_cold_cap(state, tokens):
    cfg = config([BOXES[0], {**BOXES[1], "daily_limit": 1}])
    connect(cfg, PRIMARY, SECOND)
    providers = fakes(PRIMARY, SECOND)
    jo = await person(state, "jo@roofco.test", mailbox=SECOND, status="replied")
    await sent(state, jo, "jo@roofco.test", SECOND, "today", hours=1)
    reply = await row(state, jo, "jo@roofco.test", mailbox=SECOND, kind="reply", step=0,
                      campaign="", body="Tuesday works.", thread_ref="t-9",
                      in_reply_to="<r@roofco.test>")
    cold = await row(state, await person(state, "kim@plumbco.test", mailbox=SECOND),
                     "kim@plumbco.test", mailbox=SECOND, campaign="c2")
    await make_sender(state, cfg, providers)._drain_due()
    assert [s["to"] for s in providers[SECOND].sent] == ["jo@roofco.test"]
    assert (await state.get_outbox_item(reply))["status"] == "sent"
    assert (await state.get_outbox_item(cold))["status"] == "approved"


@pytest.mark.asyncio
async def test_while_warming_only_replies_go_from_a_mailbox(state, tokens):
    today = datetime.now().date().isoformat()
    cfg = config(warmup={"start_date": today})        # 14 warming days: cold cap 0
    connect(cfg, PRIMARY)
    providers = fakes()
    jo = await person(state, "jo@roofco.test", mailbox=PRIMARY, status="replied")
    await row(state, jo, "jo@roofco.test", mailbox=PRIMARY, kind="reply", step=0,
              campaign="", body="Tuesday works.")
    await row(state, await person(state, "kim@plumbco.test", mailbox=PRIMARY),
              "kim@plumbco.test", mailbox=PRIMARY, campaign="c2")
    await make_sender(state, cfg, providers)._drain_due()
    assert [s["to"] for s in providers[PRIMARY].sent] == ["jo@roofco.test"]


@pytest.mark.asyncio
async def test_one_mailbox_a_spent_daily_cap_still_lets_replies_out(state, monkeypatch):
    from tests.test_outbox_native import Cfg, FakeProvider, make_sender as single_sender
    monkeypatch.setattr(Cfg.channels.email, "max_daily_sends", 1)
    sender = single_sender(state, FakeProvider())
    jo = await person(state, "jo@roofco.test", status="replied")
    await sent(state, jo, "jo@roofco.test", "", "today", hours=0)
    reply = await row(state, jo, "jo@roofco.test", kind="reply", step=0, campaign="",
                      body="Tuesday works.", approved_by="you")
    cold = await row(state, await person(state, "kim@plumbco.test"), "kim@plumbco.test",
                     campaign="c2", approved_by="you", body="Hi there,\n\nHow do you book jobs?")
    await sender._drain_due()
    assert [s["to"] for s in sender.provider.sent] == ["jo@roofco.test"]
    assert (await state.get_outbox_item(reply))["status"] == "sent"
    assert (await state.get_outbox_item(cold))["status"] == "approved"


@pytest.mark.parametrize("answer, waits", [
    (SendResult(ok=False, error="not signed in", attempted=False), True),
    (SendResult(ok=False, error="gmail send failed", status=401), True),
    (SendResult(ok=False, error="gmail send failed", status=403), True),
    (SendResult(ok=False, error="gmail send failed", status=429), True),
    (SendResult(ok=False, error="gmail send failed", status=400), False),
])
@pytest.mark.asyncio
async def test_a_refused_mailbox_puts_its_email_back(state, tokens, answer, waits):
    cfg = config()
    connect(cfg, PRIMARY, SECOND)
    providers = fakes(PRIMARY, SECOND, THIRD)
    providers[SECOND].result = answer
    first = await row(state, await person(state, "a@one.test", mailbox=SECOND), "a@one.test",
                      mailbox=SECOND, campaign="a")
    second = await row(state, await person(state, "b@two.test", mailbox=SECOND), "b@two.test",
                       mailbox=SECOND, campaign="b")
    other = await row(state, await person(state, "c@three.test", mailbox=PRIMARY),
                      "c@three.test", mailbox=PRIMARY, campaign="c")
    await make_sender(state, cfg, providers)._drain_due()
    item = await state.get_outbox_item(first)
    if waits:
        assert (item["status"], item["error"]) == ("approved", "")
        # The mailbox rests for the cycle: its next email isn't tried.
        assert providers[SECOND].attempts == 1
        assert (await state.get_outbox_item(second))["status"] == "approved"
        assert await state.get_setting(mb.down_since_key(SECOND))
    else:
        assert item["status"] == "failed"
        assert providers[SECOND].attempts == 2
        assert not await state.get_setting(mb.down_since_key(SECOND))
    assert (await state.get_outbox_item(other))["status"] == "sent"


@pytest.mark.parametrize("error", [RuntimeError("Gmail send outcome is unknown"),
                                   ConnectionError("reset")])
@pytest.mark.asyncio
async def test_a_send_that_may_have_gone_out_is_never_retried(state, tokens, error):
    cfg = config()
    connect(cfg, SECOND)
    providers = fakes()
    providers[SECOND].error = error
    item = await row(state, await person(state, "a@one.test", mailbox=SECOND), "a@one.test",
                     mailbox=SECOND)
    await make_sender(state, cfg, providers)._drain_due()
    stored = await state.get_outbox_item(item)
    assert stored["status"] == "failed" and stored["error"].startswith(MAYBE_SENT_PREFIX)


@pytest.mark.asyncio
async def test_a_good_send_clears_the_down_mark(state, tokens):
    cfg = config()
    connect(cfg, SECOND)
    await state.set_setting(mb.down_since_key(SECOND), _ago(hours=2))
    await row(state, await person(state, "a@one.test", mailbox=SECOND), "a@one.test",
              mailbox=SECOND)
    await make_sender(state, cfg, fakes())._drain_due()
    assert not await state.get_setting(mb.down_since_key(SECOND))


@pytest.mark.asyncio
async def test_a_day_down_moves_only_the_untouched_prospects(state, tokens):
    cfg = config()
    connect(cfg, PRIMARY, THIRD)                   # SECOND is signed out
    providers = fakes()
    await state.set_setting(mb.down_since_key(SECOND), _ago(hours=25))
    untouched = await person(state, "jo@roofco.test", mailbox=SECOND)
    queued = await row(state, untouched, "jo@roofco.test", mailbox=SECOND)
    emailed = await person(state, "lee@plumbco.test", mailbox=SECOND, status="contacted")
    await sent(state, emailed, "lee@plumbco.test", SECOND, "c-sent")
    follow_up = await row(state, emailed, "lee@plumbco.test", mailbox=SECOND, step=2,
                          campaign="c-sent")
    colleague = await person(state, "kim@plumbco.test", mailbox=SECOND)
    their_row = await row(state, colleague, "kim@plumbco.test", mailbox=SECOND, campaign="c3")

    await make_sender(state, cfg, providers)._run_native()

    moved = (await state.get_prospect(untouched)).mailbox
    assert moved == PRIMARY                        # the least busy (a tie goes in config order)
    assert (await state.get_outbox_item(queued))["status"] == "sent"
    # The moved email signs as its new sender.
    [out] = providers[PRIMARY].sent
    assert out["to"] == "jo@roofco.test" and "\n\nAlex\n" in out["body"]
    for pid in (emailed, colleague):
        assert (await state.get_prospect(pid)).mailbox == SECOND
    for item in (follow_up, their_row):
        stored = await state.get_outbox_item(item)
        assert (stored["mailbox"], stored["status"]) == (SECOND, "approved")


@pytest.mark.asyncio
async def test_a_short_outage_moves_nobody(state, tokens):
    cfg = config()
    connect(cfg, PRIMARY)
    await state.set_setting(mb.down_since_key(SECOND), _ago(hours=2))
    pid = await person(state, "jo@roofco.test", mailbox=SECOND)
    await make_sender(state, cfg, fakes())._run_native()
    assert (await state.get_prospect(pid)).mailbox == SECOND


# ── The sign-off ──

@pytest.mark.asyncio
async def test_the_tag_is_filled_in_from_the_mailbox_at_send_time(state, tokens):
    cfg = config()
    connect(cfg, SECOND, THIRD)
    providers = fakes()
    pid = await person(state, "jo@roofco.test", mailbox=SECOND)
    item = await row(state, pid, "jo@roofco.test", mailbox=SECOND)
    # Moved before it went: the same email now signs as Robin.
    await state.move_prospects_to_mailbox([pid], THIRD)
    await make_sender(state, cfg, providers)._drain_due()
    assert providers[SECOND].sent == []
    [out] = providers[THIRD].sent
    assert "\n\nRobin\n" in out["body"]
    assert "\n\nRobin\n" in (await state.get_outbox_item(item))["body"]


def test_staging_leaves_the_tag_for_send_time():
    from tests.test_outreach_fixes import _render
    assert _render("Hi {{first_name}},\n\n{{sender_first_name}}", "Acme") == \
        "Hi Dana,\n\n{{sender_first_name}}"


@pytest.mark.asyncio
async def test_another_unrendered_tag_is_still_caught(state, tokens):
    cfg = config()
    connect(cfg, SECOND)
    item = await row(state, await person(state, "jo@roofco.test", mailbox=SECOND),
                     "jo@roofco.test", mailbox=SECOND,
                     body="Hi {{nickname}},\n\nHow do you book jobs?\n\n{{sender_first_name}}")
    await make_sender(state, cfg, fakes())._drain_due()
    stored = await state.get_outbox_item(item)
    assert stored["status"] == "failed" and "unrendered merge tags" in stored["error"]


@pytest.mark.asyncio
async def test_a_mis_signed_email_is_held_until_you_approve_it(state, tokens):
    cfg = config()
    connect(cfg, SECOND)
    providers = fakes()
    pid = await person(state, "jo@roofco.test", mailbox=SECOND)
    item = await row(state, pid, "jo@roofco.test", mailbox=SECOND,
                     body="Hi there,\n\nHow do you book jobs today?\n\nThanks,\nAlex")
    sender = make_sender(state, cfg, providers)
    await sender._drain_due()
    stored = await state.get_outbox_item(item)
    assert stored["status"] == "pending_review"
    assert stored["error"].startswith(HELD_FOR_REVIEW) and "Alex" in stored["error"]
    assert await state.approve_outbox() == 0          # Approve all leaves it
    assert await state.approve_outbox(item) == 1      # one by one, it's yours
    await sender._drain_due()
    assert (await state.get_outbox_item(item))["status"] == "sent"
    assert providers[SECOND].sent[0]["body"].startswith("Hi there,")


@pytest.mark.parametrize("body, held", [
    ("Hi there,\n\nHow do you book jobs today?\n\nSam", False),
    ("Hi there,\n\nHow do you book jobs today?\n\nBest wishes, Sam", False),
    ("Hi there,\n\nHow do you book jobs today?\n\nThanks,\nSam Sample", False),
    ("Hi there,\n\nHow do you book jobs today?\n\nThanks,\nAlex Example", True),
    ("Hi Robin,\n\nHow do you book jobs today?\n\nSam", False),      # the greeting is theirs
    ("Hi there,\n\nHow do you book jobs today?", True),             # no sign-off
    ("Hi there,\n\nRobin here. How do you book jobs today?\n\nSam", True),
    ("Hi there,\n\nI'm Alex, how do you book jobs today?\n\nSam", True),
])
@pytest.mark.asyncio
async def test_what_counts_as_a_wrong_sign_off(state, tokens, body, held):
    cfg = config()
    sender = make_sender(state, cfg, fakes())
    item = {"kind": "sequence", "body": body}
    assert bool(sender._sign_off_problems(item, mb.mailbox_for(cfg, SECOND))) is held


@pytest.mark.asyncio
async def test_a_reply_only_must_not_sign_as_someone_else(state, tokens):
    cfg = config()
    sender = make_sender(state, cfg, fakes())
    sam = mb.mailbox_for(cfg, SECOND)
    assert sender._sign_off_problems({"kind": "reply", "body": "Tuesday works."}, sam) == []
    assert sender._sign_off_problems({"kind": "reply", "body": "Tuesday works.\n\nRobin"}, sam)


@pytest.mark.asyncio
async def test_drafts_waiting_for_review_are_marked_held_when_mis_signed(state, tokens):
    cfg = config(require_approval=True)
    connect(cfg, SECOND)
    pid = await person(state, "jo@roofco.test", mailbox=SECOND)
    good = await row(state, pid, "jo@roofco.test", mailbox=SECOND, status="pending_review",
                     approved_by="", campaign="g")
    bad = await row(state, pid, "jo@roofco.test", mailbox=SECOND, status="pending_review",
                    approved_by="", campaign="b", body="Hi there,\n\nHow do you book jobs?\n\nAlex")
    await make_sender(state, cfg, fakes())._run_native()
    assert (await state.get_outbox_item(good))["error"] == ""
    assert (await state.get_outbox_item(bad))["error"].startswith(HELD_FOR_REVIEW)
    assert await state.approve_outbox() == 1          # the good one only


@pytest.mark.asyncio
async def test_a_cold_email_from_another_mailbox_than_the_prospects_waits(state, tokens):
    cfg = config()
    connect(cfg, PRIMARY, SECOND)
    pid = await person(state, "jo@roofco.test", mailbox=SECOND)
    item = await row(state, pid, "jo@roofco.test", mailbox=PRIMARY)
    await make_sender(state, cfg, fakes())._drain_due()
    stored = await state.get_outbox_item(item)
    assert stored["status"] == "pending_review" and stored["error"].startswith(HELD_FOR_REVIEW)


# ── Config-wide bits ──

def test_every_mailbox_domain_counts_as_yours():
    from harvey.gate import allowed_link_domains, autopilot_review
    cfg = config()
    assert {"sender-one.test", "sender-two.test"} <= allowed_link_domains(cfg)
    assert autopilot_review("hi", "See sender-two.test/work for examples.", cfg) == []
    assert allowed_link_domains(make_config()) >= {"sender-one.test"}


def test_a_new_sign_in_rebuilds_the_mailboxs_provider(tokens):
    cfg = config()
    built = []
    pool = mb.MailboxProviders(cfg, Env(), factory=lambda m: built.append(m.email) or object())
    sam = mb.mailbox_for(cfg, SECOND)
    first = pool.get(sam)
    assert pool.get(sam) is first
    connect(cfg, SECOND)                             # signed in: the file appears
    second = pool.get(sam)
    assert second is not first and pool.get(sam) is second
    stat = sam.token_path.stat()
    os.utime(sam.token_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    assert pool.get(sam) is not second               # signed in again
    assert built == [SECOND, SECOND, SECOND]


def test_the_pool_builds_each_mailboxs_own_gmail_provider(tokens):
    cfg = config()
    provider = mb.MailboxProviders(cfg, Env()).get(mb.mailbox_for(cfg, SECOND))
    assert isinstance(provider, gmail.GmailProvider) and provider.mailbox.email == SECOND
