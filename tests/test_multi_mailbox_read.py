"""Several mailboxes, reading side: every inbox read, a reply answered from
the inbox it arrived in, one message in two inboxes handled once, what an
unread inbox holds back, and bounces counted per mailbox. Bounces that match
no email Harvey sent change nothing, with one mailbox too."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

import harvey.agents.handler as handler_mod
import harvey.integrations.gmail as gmail
import harvey.mailboxes as mb
from harvey.integrations.mail_provider import InboundMessage
from tests.test_mailboxes import PRIMARY, SECOND, THIRD
from tests.test_multi_mailbox_send import (  # noqa: F401  (fixtures)
    BoxProvider, Env, config, connect, fakes, make_sender, person, row, sent, state, tokens,
)
from tests.test_outbox_native import StubBrain


def make_handler(state, cfg, providers, intent="question", brain=None):
    from harvey.agents.handler import Handler
    handler = Handler(brain=brain or StubBrain(intent=intent), state=state, config=cfg, env=Env())
    handler.mailbox_providers.factory = lambda m: providers.setdefault(m.email, BoxProvider(m.email))
    return handler


def reply(from_email, provider_id, message_id="", thread_ref="", body="What does it cost?",
          **kw):
    return InboundMessage(provider_id=provider_id, from_email=from_email, subject="Re: booking jobs",
                          body=body, message_id=message_id, thread_ref=thread_ref, **kw)


# ── Replies ──

@pytest.mark.asyncio
async def test_a_reply_is_answered_from_the_inbox_it_arrived_in(state, tokens):
    cfg = config()
    connect(cfg, PRIMARY, SECOND)
    providers = fakes(PRIMARY, SECOND)
    pid = await person(state, "jo@roofco.test", mailbox=PRIMARY, status="contacted")
    await sent(state, pid, "jo@roofco.test", PRIMARY, "c1")
    # They wrote to Sam rather than answering Alex.
    providers[SECOND].inbound = [reply("jo@roofco.test", "g-1", "<r1@roofco.test>", "sam-thread-9")]
    handler = make_handler(state, cfg, providers)
    assert await handler._run_native() is True

    [convo] = await state.get_conversations_by_status("open")
    assert convo.mailbox == SECOND
    [answer] = [r for r in await state.get_outbox(limit=20) if r["kind"] == "reply"]
    assert (answer["mailbox"], answer["thread_ref"], answer["in_reply_to"]) == (
        SECOND, "sam-thread-9", "<r1@roofco.test>")
    assert providers[PRIMARY].confirmed == providers[SECOND].confirmed == 1

    await state.approve_outbox(answer["id"])
    await make_sender(state, cfg, providers)._drain_due()
    [out] = providers[SECOND].sent
    assert (out["thread_ref"], out["in_reply_to"]) == ("sam-thread-9", "<r1@roofco.test>")
    assert providers[PRIMARY].sent == []


@pytest.mark.asyncio
async def test_the_reply_prompt_says_who_is_writing_after_the_skills(state, tokens):
    cfg = config()
    connect(cfg, SECOND)
    providers = fakes(SECOND)

    class Brain(StubBrain):
        prompts = []

        def load_skills_for_agent(self, *_):
            return "SKILLS-MARKER"

        async def think(self, prompt, session_id=None, agent="", task=""):
            self.prompts.append(prompt)
            return await super().think(prompt, session_id, agent, task)

    brain = Brain()
    await person(state, "jo@roofco.test", mailbox=SECOND, status="contacted")
    providers[SECOND].inbound = [reply("jo@roofco.test", "g-1", "<r1@roofco.test>")]
    await make_handler(state, cfg, providers, brain=brain).run()
    [written] = [p for p in brain.prompts if "Write a reply" in p]
    assert "You are Sam, on the Example Co team." in written        # no prompt file in the stub
    who = written.index(f"WHO IS WRITING: Sam at Example Co, from {SECOND}")
    assert written.index("SKILLS-MARKER") < who
    assert "Alex" not in written


@pytest.mark.asyncio
async def test_one_message_in_two_inboxes_is_handled_once(state, tokens):
    cfg = config()
    connect(cfg, PRIMARY, SECOND)
    providers = fakes(PRIMARY, SECOND)
    await person(state, "jo@roofco.test", mailbox=SECOND, status="contacted")
    # Copied to both: each inbox has its own Gmail id for it.
    providers[PRIMARY].inbound = [reply("jo@roofco.test", "a-1", "<both@roofco.test>", "t-a")]
    providers[SECOND].inbound = [reply("jo@roofco.test", "b-1", "<both@roofco.test>", "t-b")]
    handler = make_handler(state, cfg, providers)
    assert await handler._run_native() is True
    [convo] = await state.get_conversations_by_status("open")
    assert convo.mailbox == SECOND and len(convo.thread) == 1       # from their own mailbox
    [answer] = [r for r in await state.get_outbox(limit=20) if r["kind"] == "reply"]
    assert answer["thread_ref"] == "t-b"
    for key in ("a-1", "<both@roofco.test>", f"{SECOND}:b-1"):
        assert await state.is_reply_processed(key)
    assert providers[PRIMARY].confirmed == providers[SECOND].confirmed == 1

    assert await handler._run_native() is True                     # read again: nothing new
    assert len((await state.get_conversations_by_status("open"))[0].thread) == 1


@pytest.mark.asyncio
async def test_the_primary_keeps_its_old_keys(state, tokens):
    cfg = config()
    connect(cfg, PRIMARY)
    providers = fakes(PRIMARY)
    await person(state, "jo@roofco.test", mailbox="", status="contacted")
    # Handled before there were mailboxes: only its Gmail id was kept.
    await state.mark_reply_processed("old-1")
    providers[PRIMARY].inbound = [reply("jo@roofco.test", "old-1", "<old@roofco.test>")]
    assert await make_handler(state, cfg, providers)._run_native() is True
    assert await state.get_conversations_by_status("open") == []


# ── What an unread inbox holds back ──

@pytest.mark.asyncio
async def test_an_unreadable_inbox_holds_cold_email_its_replies_could_stop(state, tokens):
    cfg = config()
    connect(cfg, PRIMARY, SECOND)
    providers = fakes(PRIMARY, SECOND)
    providers[SECOND].read_error = gmail.GmailError("Google rejected the saved Gmail sign-in (401)")
    # Sam emailed Lee; Lee's colleague Kim is queued from Alex's mailbox.
    lee = await person(state, "lee@roofco.test", mailbox=SECOND, status="contacted")
    await sent(state, lee, "lee@roofco.test", SECOND, "c-lee")
    kim = await person(state, "kim@roofco.test", mailbox=PRIMARY)
    kims = await row(state, kim, "kim@roofco.test", mailbox=PRIMARY, campaign="c-kim")
    other = await person(state, "ann@plumbco.test", mailbox=PRIMARY)
    anns = await row(state, other, "ann@plumbco.test", mailbox=PRIMARY, campaign="c-ann")
    reply_row = await row(state, kim, "kim@roofco.test", mailbox=PRIMARY, kind="reply", step=0,
                          campaign="", body="Tuesday works.")

    handler = make_handler(state, cfg, providers)
    assert await handler._run_native() is True                     # only part is held
    assert {lee, kim} <= handler.hold_prospects and other not in handler.hold_prospects
    assert handler.hold_mailboxes == {SECOND}
    assert providers[PRIMARY].confirmed == 1 and providers[SECOND].confirmed == 0
    assert await state.get_setting(mb.read_ok_key(PRIMARY))
    assert not await state.get_setting(mb.read_ok_key(SECOND))

    sender = make_sender(state, cfg, providers)
    sender.hold_prospects, sender.hold_mailboxes = handler.hold_prospects, handler.hold_mailboxes
    await sender._drain_due()
    assert (await state.get_outbox_item(kims))["status"] == "approved"
    assert (await state.get_outbox_item(anns))["status"] == "sent"
    assert (await state.get_outbox_item(reply_row))["status"] == "sent"    # replies still go

    # The next run starts afresh.
    providers[SECOND].read_error = None
    assert await handler._run_native() is True
    assert handler.hold_prospects == set() and handler.hold_mailboxes == set()


@pytest.mark.asyncio
async def test_a_signed_out_mailbox_that_has_sent_holds_its_prospects(state, tokens):
    cfg = config()
    connect(cfg, PRIMARY)                                          # SECOND isn't signed in
    providers = fakes(PRIMARY, SECOND)
    lee = await person(state, "lee@roofco.test", mailbox=SECOND, status="contacted")
    await sent(state, lee, "lee@roofco.test", SECOND, "c-lee")
    handler = make_handler(state, cfg, providers)
    assert await handler._run_native() is True
    assert lee in handler.hold_prospects and handler.hold_mailboxes == {SECOND}


@pytest.mark.asyncio
async def test_a_signed_out_mailbox_that_never_sent_holds_nothing(state, tokens):
    cfg = config()
    connect(cfg, PRIMARY)
    handler = make_handler(state, cfg, fakes(PRIMARY, SECOND, THIRD))
    assert await handler._run_native() is True
    assert handler.hold_prospects == set() and handler.hold_mailboxes == set()


@pytest.mark.asyncio
async def test_a_read_that_runs_too_long_holds_all_sending(state, tokens, monkeypatch):
    monkeypatch.setattr(handler_mod, "MAILBOX_READ_TIMEOUT_SECONDS", 0.05)
    cfg = config()
    connect(cfg, PRIMARY, SECOND)
    providers = fakes(PRIMARY, SECOND)
    providers[SECOND].read_delay = 5
    await person(state, "jo@roofco.test", mailbox=PRIMARY, status="contacted")
    providers[PRIMARY].inbound = [reply("jo@roofco.test", "g-1", "<r1@roofco.test>")]
    handler = make_handler(state, cfg, providers)
    assert await handler._run_native() is False
    assert SECOND in handler.last_read_problem
    # The inbox that was read is still handled (its stop-on-reply has run).
    assert (await state.get_prospect_by_email("jo@roofco.test")).status == "replied"
    assert providers[PRIMARY].confirmed == 1 and providers[SECOND].confirmed == 0


@pytest.mark.asyncio
async def test_an_unexpected_read_error_holds_all_sending(state, tokens):
    cfg = config()
    connect(cfg, PRIMARY, SECOND)
    providers = fakes(PRIMARY, SECOND)
    providers[SECOND].read_error = ValueError("something odd")
    assert await make_handler(state, cfg, providers)._run_native() is False


@pytest.mark.asyncio
async def test_a_reply_that_cant_be_handled_holds_all_and_isnt_confirmed(state, tokens, monkeypatch):
    cfg = config()
    connect(cfg, PRIMARY, SECOND)
    providers = fakes(PRIMARY, SECOND)
    await person(state, "jo@roofco.test", mailbox=SECOND, status="contacted")
    providers[SECOND].inbound = [reply("jo@roofco.test", "g-1", "<r1@roofco.test>")]
    handler = make_handler(state, cfg, providers)

    async def broken(*a, **k):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(handler, "_process_reply", broken)
    assert await handler._run_native() is False
    assert providers[PRIMARY].confirmed == 1 and providers[SECOND].confirmed == 0


@pytest.mark.asyncio
async def test_the_brain_going_down_leaves_the_reply_for_later(state, tokens):
    from harvey.brain import BrainUnavailable
    cfg = config()
    connect(cfg, SECOND)
    providers = fakes(SECOND)
    await person(state, "jo@roofco.test", mailbox=SECOND, status="contacted")
    providers[SECOND].inbound = [reply("jo@roofco.test", "g-1", "<r1@roofco.test>")]
    handler = make_handler(state, cfg, providers)
    handler._process_reply = AsyncMock(side_effect=BrainUnavailable("usage limit"))
    assert await handler._run_native() is False
    assert not await state.is_reply_processed(f"{SECOND}:g-1")
    assert providers[SECOND].confirmed == 0


@pytest.mark.asyncio
async def test_an_inbox_not_read_for_a_week_reads_further_back(state, tokens):
    cfg = config()
    connect(cfg, PRIMARY, SECOND, THIRD)
    providers = fakes()
    ten_days = (datetime.now(timezone.utc).replace(tzinfo=None)
                - timedelta(days=10) + timedelta(hours=1)).isoformat()
    await state.set_setting(mb.read_ok_key(PRIMARY), ten_days)
    lee = await person(state, "lee@roofco.test", mailbox=SECOND, status="contacted")
    await sent(state, lee, "lee@roofco.test", SECOND, "c-lee")     # sent, never read
    await state.set_setting(mb.read_ok_key(THIRD), datetime.now(timezone.utc)
                            .replace(tzinfo=None).isoformat())
    handler = make_handler(state, cfg, providers)
    assert await handler._run_native() is True
    assert providers[PRIMARY].widened == [11]
    assert providers[SECOND].widened == [10 ** 6]                  # as far back as reads go
    assert providers[THIRD].widened == []
    # Read completely now: the next one is the usual read.
    assert await handler._run_native() is True
    assert providers[PRIMARY].widened == [11]


@pytest.mark.asyncio
async def test_a_widened_gmail_read_lists_further_back_once():
    p = gmail.GmailProvider(gmail_config(), Env())
    p._token = AsyncMock(return_value="synthetic-token")
    queries = []

    async def answer(method, path, **kw):
        if path == "/profile":
            return {"historyId": "5", "emailAddress": PRIMARY}
        if path == "/messages":
            queries.append(kw["params"]["q"])
            return {}
        return None
    p._request = answer
    p._history_id, p._last_full_read = "4", 10 ** 12               # mid-way: would read history
    p.widen_next_read(45)
    await p.get_replies()
    assert queries == ["in:inbox -from:me newer_than:30d"]
    p.confirm_read()
    assert p.full_read_days == gmail.FULL_READ_DAYS
    p.widen_next_read(2)
    assert p.full_read_days == gmail.FULL_READ_DAYS               # never less than the week


def gmail_config():
    from types import SimpleNamespace
    return SimpleNamespace(persona=SimpleNamespace(name="Alex Example", email=PRIMARY))


@pytest.mark.asyncio
async def test_a_read_clears_the_down_mark(state, tokens):
    cfg = config()
    connect(cfg, SECOND)
    await state.set_setting(mb.down_since_key(SECOND), "2026-01-01T00:00:00")
    assert await make_handler(state, cfg, fakes(SECOND))._run_native() is True
    assert not await state.get_setting(mb.down_since_key(SECOND))


# ── Bounces ──

def bounce(provider_id, body="This message couldn't be delivered.", in_reply_to="", date="",
           mailbox=""):
    return InboundMessage(provider_id=provider_id, from_email="mailer-daemon@googlemail.com",
                          subject="Delivery Status Notification (Failure)", body=body,
                          in_reply_to=in_reply_to, is_bounce=True, date=date, mailbox=mailbox)


async def ten_sent(state, mailbox, to="lee@roofco.test"):
    pid = await person(state, to, mailbox=mailbox, status="contacted", first="Lee")
    for i in range(10):
        await sent(state, pid, to, mailbox, f"c{mailbox}{i}")
    return pid


@pytest.mark.asyncio
async def test_a_bounce_matching_no_sent_email_changes_nothing_with_one_mailbox(state):
    from tests.test_outbox_native import FakeProvider, make_handler as single_handler
    pid = await ten_sent(state, "")
    provider = FakeProvider()
    provider.inbound = [bounce("b1", body="Address not found: someone@else.test",
                               in_reply_to="<not-ours@x.test>")]
    assert await single_handler(state, provider)._run_native() is True
    assert await state.get_setting("bounce_count") == ""
    assert await state.get_setting("sending_paused") == ""
    assert (await state.get_prospect(pid)).email_status == "verified"


@pytest.mark.asyncio
async def test_a_bounce_naming_a_sent_address_counts_with_one_mailbox(state):
    from tests.test_outbox_native import FakeProvider, make_handler as single_handler
    pid = await ten_sent(state, "")
    provider = FakeProvider()
    provider.inbound = [bounce("b1", body="Address not found: lee@roofco.test")]
    await single_handler(state, provider)._run_native()
    assert (await state.get_prospect(pid)).email_status == "invalid"
    assert await state.get_setting("bounce_count") == "1"
    assert await state.get_setting("sending_paused") != ""        # 1/10 > 5%: global, as before


@pytest.mark.asyncio
async def test_a_bounce_from_before_the_email_went_is_not_about_it(state):
    from tests.test_outbox_native import FakeProvider, make_handler as single_handler
    pid = await ten_sent(state, "")
    provider = FakeProvider()
    provider.inbound = [bounce("b1", body="Address not found: lee@roofco.test",
                               date="Mon, 02 Jan 2023 10:00:00 +0000")]
    await single_handler(state, provider)._run_native()
    assert (await state.get_prospect(pid)).email_status == "verified"
    assert await state.get_setting("bounce_count") == ""


@pytest.mark.asyncio
async def test_an_unmatched_bounce_changes_nothing_with_several_mailboxes(state, tokens):
    cfg = config()
    connect(cfg, SECOND)
    providers = fakes(SECOND)
    await ten_sent(state, SECOND)
    providers[SECOND].inbound = [bounce("b1", body="Address not found: someone@else.test")]
    assert await make_handler(state, cfg, providers)._run_native() is True
    assert await state.get_setting(mb.bounce_key(SECOND)) == ""
    assert await state.get_setting(mb.paused_key(SECOND)) == ""


@pytest.mark.asyncio
async def test_bounces_pause_only_the_mailbox_that_sent(state, tokens):
    cfg = config()
    connect(cfg, PRIMARY, SECOND)
    providers = fakes(PRIMARY, SECOND)
    pid = await ten_sent(state, SECOND)
    await ten_sent(state, PRIMARY, to="kim@plumbco.test")
    providers[SECOND].inbound = [bounce("b1", body="Address not found: lee@roofco.test")]
    assert await make_handler(state, cfg, providers)._run_native() is True
    assert (await state.get_prospect(pid)).email_status == "invalid"
    assert await state.get_setting(mb.bounce_key(SECOND)) == "1"
    assert "bounce rate 1/10" in await state.get_setting(mb.paused_key(SECOND))
    assert await state.get_setting("sending_paused") == ""
    assert await state.get_setting("bounce_count") == ""
    assert await state.get_setting(mb.paused_key(PRIMARY)) == ""
    # Only the bounced mailbox's rows count: the address isn't Alex's.
    providers[PRIMARY].inbound = [bounce("b2", body="Address not found: lee@roofco.test")]
    await make_handler(state, cfg, providers)._run_native()
    assert await state.get_setting(mb.bounce_key(PRIMARY)) == ""


@pytest.mark.asyncio
async def test_a_bounce_by_message_id_counts_against_that_emails_mailbox(state, tokens):
    cfg = config()
    connect(cfg, PRIMARY)
    providers = fakes(PRIMARY)
    await ten_sent(state, SECOND)
    # Arrived in Alex's inbox, but answers the email Sam sent.
    providers[PRIMARY].inbound = [bounce("b1", in_reply_to=f"<c{SECOND}3@x.test>")]
    await make_handler(state, cfg, providers)._run_native()
    assert await state.get_setting(mb.bounce_key(SECOND)) == "1"
    assert await state.get_setting(mb.bounce_key(PRIMARY)) == ""


# ── The heartbeat hands the holds to the sender ──

@pytest.mark.asyncio
async def test_the_holds_reach_the_sender_and_reset_each_cycle(state, tokens, monkeypatch):
    import harvey.main as main_mod
    from types import SimpleNamespace
    cfg = config()
    seen = []

    class Agent:
        is_native = True

        def __init__(self, *a, **k):
            # What the Handler's reading held back this cycle.
            self.hold_prospects = {"p-held"}
            self.hold_mailboxes = {SECOND}

        async def run(self):
            return True

    class Sender(Agent):
        def __init__(self, *a, **k):
            self.mailbox_providers = object()
            self.hold_prospects, self.hold_mailboxes = {"stale"}, {"stale"}
            self.cold_only = False

        async def run(self):
            seen.append((set(self.hold_prospects), set(self.hold_mailboxes)))
            return True

    class Brain:
        provider_label, model, paused_reason = "Claude", "", ""

        def __init__(self, *a, **k):
            pass

        async def preflight(self):
            return ""

        async def is_within_budget(self, *a, **k):
            return True

    async def no_sleep(seconds, event):
        event.set()
        return True

    import harvey.agents.analyst
    import harvey.agents.handler
    import harvey.agents.scout
    import harvey.agents.sender
    import harvey.agents.writer
    monkeypatch.setattr(main_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(main_mod, "load_env", lambda: SimpleNamespace())
    monkeypatch.setattr(main_mod, "StateManager", lambda: state)
    monkeypatch.setattr(main_mod, "Brain", Brain)
    monkeypatch.setattr(main_mod, "in_quiet_hours", lambda c: False)
    monkeypatch.setattr(main_mod, "_interruptible_sleep", no_sleep)
    monkeypatch.setattr(harvey.agents.scout, "Scout", Agent)
    monkeypatch.setattr(harvey.agents.writer, "Writer", Agent)
    monkeypatch.setattr(harvey.agents.sender, "Sender", Sender)
    monkeypatch.setattr(harvey.agents.handler, "Handler", Agent)
    monkeypatch.setattr(harvey.agents.analyst, "Analyst", Agent)
    await main_mod.heartbeat(asyncio.Event())
    # The sender ran with what this cycle's reading held back, not the stale sets.
    assert seen == [({"p-held"}, {SECOND})]
    assert await state.get_setting(mb.IN_USE_KEY) == mb.in_use_value(cfg)
