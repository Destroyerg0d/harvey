"""The Mailboxes tab: sending health and results per mailbox and per sending
domain (harvey/mailbox_health.py, GET /api/mailboxes and its app.js view).

Fictional mailboxes on sender-one.test and sender-two.test; no network: the
DNS checks are replaced in every test that would reach them."""

import asyncio
import json
import re
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import harvey.config as config_mod
import harvey.dashboard as dash
import harvey.integrations.gmail as gmail
import harvey.mailbox_health as mh
import harvey.mailboxes as mb
import harvey.state as state_mod
from harvey import deliverability as d
from harvey.state import StateManager
from tests.test_mailboxes import PRIMARY, SECOND, THIRD, make_config
from tests.test_multi_mailbox_send import BOXES, connect

ORIGIN = "http://127.0.0.1:5555"
NOW = datetime(2026, 11, 3, 15, 0, 0)


def ago(**delta) -> str:
    return (NOW - timedelta(**delta)).isoformat()


# ── Fixtures and rows ──

@pytest.fixture
def tokens(tmp_path, monkeypatch):
    monkeypatch.setattr(gmail, "TOKEN_FILE", tmp_path / "gmail_token.json")
    monkeypatch.setattr(gmail, "TOKENS_DIR", tmp_path / "gmail_tokens")
    return tmp_path


@pytest.fixture
def multi(monkeypatch, tokens):
    cfg = make_config(BOXES)
    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: cfg)
    return cfg


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "data" / "harvey.db"
    path.parent.mkdir(exist_ok=True)
    monkeypatch.setattr(state_mod, "DB_PATH", path)
    asyncio.run(StateManager(str(path)).init_db())
    return path


@pytest.fixture(autouse=True)
def fresh_checks(monkeypatch):
    """A DNS cache of its own per test, whose lookups are recorded and never
    leave the machine."""
    calls = []

    async def fake_run(domain, selector=""):
        calls.append((domain, selector))
        return all_ok(domain)

    clock = [1000.0]
    checks = mh.DomainChecks(run=fake_run, clock=lambda: clock[0])
    checks.calls, checks.clock_value = calls, clock
    monkeypatch.setattr(mh, "CHECKS", checks)
    return checks


def check(name, status=d.OK, detail="fine", unknown=False) -> dict:
    return {"name": name, "status": status, "detail": detail, "unknown": unknown}


def all_ok(domain="sender-one.test") -> dict:
    return {
        "mx": check("MX", detail=f"mx.{domain}"), "spf": check("SPF", detail="v=spf1 ~all"),
        "dkim": check("DKIM", detail="selector 'google' found"),
        "dmarc": check("DMARC", detail="v=DMARC1; p=quarantine"),
        "age": check("Domain age", detail="400 days (registered 2025-09-29)"),
        "blocklists": [check("Blocklist dbl.spamhaus.org", detail="not listed"),
                       check("Blocklist multi.surbl.org", detail="not listed")],
        "checked_at": "2026-11-03T15:00:00Z",
    }


def sql(path, query, params=()):
    with sqlite3.connect(path) as con:
        con.execute(query, params)


def count(path, table) -> int:
    with sqlite3.connect(path) as con:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def outbox(path, oid, mailbox, *, when=None, pid="", kind="sequence", status="sent", step=1):
    sql(path, "INSERT INTO outbox (id, campaign_id, prospect_id, step, kind, to_email, subject, "
              "body, status, send_at, sent_at, mailbox) VALUES (?, ?, ?, ?, ?, ?, 'hello', 'Hi', "
              "?, ?, ?, ?)",
        (oid, "camp-" + oid, pid, step, kind, f"{pid or oid}@client.example.test", status,
         when or ago(hours=1), when if status == "sent" else None, mailbox))


def prospect(path, pid, status, email_status="verified", email=True, mailbox=""):
    sql(path, "INSERT INTO prospects (id, first_name, email, email_status, status, mailbox) "
              "VALUES (?, 'Ann', ?, ?, ?, ?)",
        (pid, f"{pid}@outreach-{pid}.example.test" if email else "", email_status, status, mailbox))


def conversation(path, cid, pid, intent, stage, mailbox=""):
    sql(path, "INSERT INTO conversations (id, prospect_id, intent, stage, status, mailbox) "
              "VALUES (?, ?, ?, ?, 'open', ?)", (cid, pid, intent, stage, mailbox))


def bounce(path, aid, when, mailbox=None, raw=None):
    details = raw if raw is not None else json.dumps(
        {"prospect": "x@client.example.test", **({"mailbox": mailbox} if mailbox is not None else {})})
    sql(path, "INSERT INTO actions (id, action_type, agent, details_json, created_at) "
              "VALUES (?, 'bounce', 'handler', ?, ?)", (aid, details, when.replace("T", " ")[:19]))


def setting(path, key, value):
    asyncio.run(StateManager(str(path)).set_setting(key, value))


@pytest.fixture
def seeded(db, multi):
    """Three mailboxes on two domains, with sends, bounces, replies and a queue."""
    # PRIMARY (stored as ""): three cold emails and a reply, all today.
    outbox(db, "o1", "", when=ago(minutes=30), pid="p1")
    outbox(db, "o2", "", when=ago(hours=2), pid="p2")
    outbox(db, "o3", "", when=ago(hours=3), pid="p3")
    outbox(db, "o4", PRIMARY, when=ago(minutes=20), pid="p1", kind="reply", step=2)
    # SECOND: two 2 days ago, one 9 days ago.
    outbox(db, "o5", SECOND, when=ago(days=2), pid="p4")
    outbox(db, "o6", SECOND, when=ago(days=2, hours=1), pid="p5")
    outbox(db, "o7", SECOND.upper(), when=ago(days=9), pid="p6")
    # THIRD: one, 20 days ago.
    outbox(db, "o8", THIRD, when=ago(days=20), pid="p7")
    # Waiting: one to approve, one approved, one going out now.
    outbox(db, "q1", "", pid="p8", status="pending_review")
    outbox(db, "q2", SECOND, pid="p9", status="approved")
    outbox(db, "q3", THIRD, pid="p10", status="sending")
    # Not sent: never counted.
    outbox(db, "f1", SECOND, pid="p9", status="failed", step=2)

    prospect(db, "p1", "replied")
    prospect(db, "p2", "opted_out")
    prospect(db, "p3", "contacted")
    prospect(db, "p4", "lost")
    prospect(db, "p5", "contacted", email_status="invalid")     # bounced
    prospect(db, "p6", "replied")
    prospect(db, "p7", "meeting")
    for pid in ("p8", "p9", "p10"):
        prospect(db, pid, "queued")
    prospect(db, "p11", "new", email_status="guess")
    prospect(db, "p12", "new", email_status="", email=False)
    conversation(db, "c1", "p1", "interested", "engaged")
    conversation(db, "c2", "p6", "question", "engaged", mailbox=SECOND)
    sql(db, "INSERT INTO companies (id, name, domain) VALUES ('co1', 'Outreach One', 'outreach-one.example.test')")
    sql(db, "INSERT INTO companies (id, name, domain) VALUES ('co2', 'Outreach Two', 'outreach-two.example.test')")

    bounce(db, "b1", ago(days=1), mailbox=SECOND)
    bounce(db, "b2", ago(days=30))                       # one-mailbox days: the primary's
    bounce(db, "b3", ago(days=40), raw="not json")       # unreadable: the primary's

    setting(db, mb.paused_key(SECOND), "bounce rate 1/3 exceeded 5%")
    setting(db, mb.bounce_key(SECOND), "1")
    setting(db, mb.read_ok_key(PRIMARY), ago(minutes=5))
    connect(multi, PRIMARY, SECOND)                       # THIRD isn't signed in
    return db


def run_collect(db, cfg, **kw):
    return asyncio.run(mh.collect(db, cfg, now=NOW, **kw))


def by_email(report) -> dict:
    return {m["email"]: m for m in report["mailboxes"]}


# ── Numbers per mailbox ──

def test_each_mailbox_counts_its_own_sends_bounces_replies_and_queue(seeded, multi):
    report = run_collect(seeded, multi)
    boxes = by_email(report)
    assert list(boxes) == [PRIMARY, SECOND, THIRD]

    p = boxes[PRIMARY]
    assert (p["sent_today"], p["sent_24h"], p["sent_7d"], p["sent_total"], p["cold_today"]) == (4, 4, 4, 4, 3)
    assert (p["contacted"], p["replied"], p["opted_out"], p["positive"]) == (3, 2, 1, 1)
    assert (p["bounces_total"], p["bounces_7d"], p["bounces_since_resume"]) == (2, 0, 0)
    assert (p["to_approve"], p["approved"], p["queued"]) == (1, 0, 1)
    assert p["status"] == "sending" and p["last_inbox_read"] == ago(minutes=5)
    assert p["last_sent"] == ago(minutes=20)
    assert (p["bounce_rate"], p["reply_rate"]) == (0.5, round(2 / 3, 4))

    s = boxes[SECOND]
    assert (s["sent_today"], s["sent_7d"], s["sent_total"]) == (0, 2, 3)
    assert (s["contacted"], s["replied"], s["opted_out"], s["positive"]) == (3, 2, 0, 0)
    assert (s["bounces_total"], s["bounces_7d"], s["bounces_since_resume"]) == (1, 1, 1)
    assert (s["to_approve"], s["approved"]) == (0, 1)
    assert s["daily_limit"] == 30 and s["name"] == "Sam Sample"

    t = boxes[THIRD]
    assert (t["sent_total"], t["contacted"], t["replied"], t["positive"]) == (1, 1, 1, 1)
    assert t["approved"] == 1                                # 'sending' is on its way


def test_status_says_paused_not_connected_or_failing_and_why(seeded, multi):
    boxes = by_email(run_collect(seeded, multi))
    assert boxes[SECOND]["status"] == "paused"
    assert boxes[SECOND]["pause_scope"] == "mailbox"
    assert boxes[SECOND]["reason"] == "bounce rate 1/3 exceeded 5%"
    assert boxes[THIRD]["status"] == "not_connected"
    assert f"harvey gmail auth --mailbox {THIRD}" in boxes[THIRD]["reason"]

    setting(seeded, mb.down_since_key(PRIMARY), "2026-11-03T09:00:00")
    assert by_email(run_collect(seeded, multi))[PRIMARY]["status"] == "failing"

    setting(seeded, mb.GLOBAL_PAUSE_KEY, "paused from dashboard")
    boxes = by_email(run_collect(seeded, multi))
    assert boxes[PRIMARY]["status"] == "paused" and boxes[PRIMARY]["pause_scope"] == "all"
    assert boxes[PRIMARY]["reason"] == "All sending is paused: paused from dashboard"
    assert boxes[THIRD]["status"] == "not_connected" and boxes[THIRD]["paused"]


def test_summary_and_domains_add_up(seeded, multi):
    report = run_collect(seeded, multi)
    s = report["summary"]
    assert (s["sent_today"], s["capacity"], s["sent_7d"], s["sent_total"]) == (4, 90, 6, 8)
    assert (s["bounces_7d"], s["bounces_total"]) == (1, 3)
    assert s["bounce_rate_7d"] == round(1 / 6, 4) and s["bounce_rate"] == round(3 / 8, 4)
    assert (s["contacted"], s["replied"], s["positive"], s["opted_out"]) == (7, 5, 2, 1)
    assert s["reply_rate"] == round(5 / 7, 4)
    assert (s["mailboxes"], s["sending"], s["paused"], s["not_connected"]) == (3, 1, 1, 1)
    assert (s["queued"], s["to_approve"], s["approved"]) == (3, 1, 2)

    one, two = report["domains"]
    assert one["domain"] == "sender-one.test" and one["mailboxes"] == [PRIMARY, SECOND]
    assert (one["sent_total"], one["sent_7d"], one["bounces_total"], one["paused"]) == (7, 6, 3, 1)
    assert (one["contacted"], one["replied"], one["capacity"]) == (6, 4, 60)
    assert two["domain"] == "sender-two.test" and two["mailboxes"] == [THIRD]


def test_daily_series_are_per_mailbox_and_in_the_viewers_day(seeded, multi):
    daily = run_collect(seeded, multi)["daily"]
    assert daily["days"][0] == "2026-10-21" and daily["days"][-1] == "2026-11-03"
    series = {s["email"]: s for s in daily["series"]}
    assert series[PRIMARY]["sent"][-1] == 4
    assert series[SECOND]["sent"][daily["days"].index("2026-11-01")] == 2
    assert series[SECOND]["sent"][daily["days"].index("2026-10-25")] == 1
    assert sum(series[THIRD]["sent"]) == 0                       # 20 days ago
    assert daily["bounces"][daily["days"].index("2026-11-02")] == 1
    assert series[SECOND]["bounces"][daily["days"].index("2026-11-02")] == 1
    assert daily["sent"][-1] == 4 and sum(daily["sent"]) == 7

    # Ten hours east of UTC it is already 4 November, 01:00: only what went
    # out after 14:00 UTC is "today" there.
    east = run_collect(seeded, multi, tz_minutes=600)
    assert east["daily"]["days"][-1] == "2026-11-04"
    assert by_email(east)[PRIMARY]["sent_today"] == 2


def test_one_mailbox_reads_the_global_switch_and_counter(db, tokens, monkeypatch):
    cfg = make_config()
    outbox(db, "o1", "", when=ago(hours=1), pid="p1")
    prospect(db, "p1", "contacted")
    bounce(db, "b1", ago(hours=2))
    setting(db, "bounce_count", "4")
    setting(db, mb.paused_key(PRIMARY), "a stale per-mailbox pause")   # ignored with one
    report = run_collect(db, cfg)
    [box] = report["mailboxes"]
    assert box["email"] == PRIMARY and not report["multi"]
    assert (box["bounces_total"], box["bounces_since_resume"]) == (1, 4)
    assert box["status"] == "not_connected" and "'harvey gmail auth'" in box["reason"]
    connect(cfg, PRIMARY)
    assert run_collect(db, cfg)["mailboxes"][0]["status"] == "sending"
    setting(db, "sending_paused", "bounce rate 4/20 exceeded 5%")
    [box] = run_collect(db, cfg)["mailboxes"]
    assert (box["status"], box["pause_scope"]) == ("paused", "all")


def test_mail_from_a_mailbox_no_longer_configured_keeps_a_row(db, multi):
    outbox(db, "o1", "gone@sender-three.test", when=ago(days=1), pid="p1")
    prospect(db, "p1", "contacted")
    report = run_collect(db, multi)
    old = by_email(report)["gone@sender-three.test"]
    assert old["configured"] is False and old["status"] == "not_configured"
    assert report["summary"]["sent_total"] == 1 and report["summary"]["mailboxes"] == 3
    assert [x["domain"] for x in report["domains"]] == ["sender-one.test", "sender-two.test"]


def test_instantly_isnt_tracked_per_mailbox(db, tokens):
    cfg = make_config(provider="instantly")
    report = run_collect(db, cfg)
    assert report["tracks_sends"] is False
    assert report["mailboxes"][0]["status"] == "untracked"


def test_a_warmup_ramp_shows_todays_cold_cap(db, tokens):
    start = (datetime.now().date() - timedelta(days=16)).isoformat()
    cfg = make_config(BOXES, warmup={"start_date": start, "min_days": 14,
                                     "start_sends": 5, "daily_increase": 3})
    assert by_email(run_collect(db, cfg))[SECOND]["cold_cap_today"] == 11
    assert by_email(run_collect(db, make_config(BOXES)))[SECOND]["cold_cap_today"] is None


def test_an_empty_install_has_zeros_not_errors(tmp_path, tokens):
    report = run_collect(tmp_path / "nothing-here.db", make_config(BOXES))
    assert report["summary"]["sent_total"] == 0 and len(report["mailboxes"]) == 3
    assert report["funnel"]["companies"] == 0 and report["summary"]["reply_rate"] is None


# ── The results funnel ──

def test_the_funnel_counts_the_way_today_does(seeded, multi):
    f = run_collect(seeded, multi)["funnel"]
    assert (f["companies"], f["contacts"]) == (2, 12)
    # Ten addresses: nine verified and one that bounced after it was emailed.
    assert f["sendable"] == 10
    assert (f["contacted"], f["replied"], f["positive"], f["ready_to_meet"]) == (7, 5, 2, 1)
    assert (f["not_interested"], f["opted_out"]) == (1, 1)
    assert f["reply_rate"] == round(5 / 7, 4)
    assert f["companies"] >= 0 and f["sendable"] >= f["contacted"] >= f["replied"] >= f["positive"] >= f["ready_to_meet"]


def test_the_status_lists_match_the_rest_of_harvey():
    from harvey.agents.handler import BOUNCE_COUNT_KEY
    assert mh.CONTACTED_STATUSES == dash.SENT_STATUSES
    assert set(dash.REPLY_STATUSES) < set(mh.WROTE_BACK_STATUSES) < set(mh.CONTACTED_STATUSES)
    assert mh.GLOBAL_BOUNCE_KEY == BOUNCE_COUNT_KEY
    assert mh.SENDABLE_EMAIL_STATUSES == state_mod.WRITABLE_EMAIL_STATUSES


# ── The health score ──

def items(result) -> dict:
    return {i["rule"]: i["points"] for i in result["items"]}


def test_a_clean_domain_scores_100():
    result = mh.health_score(all_ok(), sent_7d=200, bounces_7d=2, sent_all=400, bounces_all=4)
    assert (result["score"], result["grade"], result["label"], result["partial"]) == (100, "good", "Healthy", False)
    assert set(items(result)) == {"Bounce rate", "SPF", "DKIM", "DMARC", "MX", "Blocklists",
                                  "Domain age", "Paused mailboxes"}


def test_each_rule_takes_its_points():
    c = all_ok()
    c["dmarc"] = check("DMARC", d.WARN, "p=none (monitoring only)")
    c["spf"] = check("SPF", d.FAIL, "No SPF record.")
    c["dkim"] = check("DKIM", d.WARN, "No key at any common selector.")
    c["mx"] = check("MX", d.FAIL, "No MX record")
    c["age"] = check("Domain age", d.WARN, "45 days (registered 2026-09-19) - still young")
    c["blocklists"][1] = check("Blocklist multi.surbl.org", d.FAIL, "LISTED (127.0.0.2)")
    result = mh.health_score(c, paused=2)
    assert items(result) == {"Bounce rate": 0, "SPF": -15, "DKIM": -5, "DMARC": -5, "MX": -10,
                             "Blocklists": -30, "Domain age": -5, "Paused mailboxes": -20}
    assert result["score"] == 10 and result["grade"] == "risk"
    by_rule = {i["rule"]: i for i in result["items"]}
    assert by_rule["Blocklists"]["result"] == "listed on multi.surbl.org"
    assert by_rule["Domain age"]["result"] == "45 days"
    c["age"] = check("Domain age", d.FAIL, "12 days (registered 2026-10-22)")
    assert mh.health_score(c, paused=3)["score"] == 0               # never below 0


@pytest.mark.parametrize("sent_7d,bounces_7d,sent_all,bounces_all,points,window", [
    (100, 2, 100, 2, 0, "last 7 days"),        # 2.0%: not over 2%
    (100, 3, 100, 3, -10, "last 7 days"),      # 3.0%
    (100, 4, 100, 4, -25, "last 7 days"),      # 4.0%
    (100, 6, 100, 6, -40, "last 7 days"),      # 6.0%
    (100, 0, 500, 100, 0, "last 7 days"),      # a clean week outweighs an old bad patch
    (10, 5, 200, 12, -40, "all time"),         # under 20 this week: all time, 6.0%
    (5, 5, 10, 5, 0, "too few"),               # under 20 ever: not judged
])
def test_the_bounce_rule(sent_7d, bounces_7d, sent_all, bounces_all, points, window):
    result = mh.health_score(all_ok(), sent_7d=sent_7d, bounces_7d=bounces_7d,
                             sent_all=sent_all, bounces_all=bounces_all)
    item = result["items"][0]
    assert item["rule"] == "Bounce rate" and item["points"] == points
    assert window in item["result"]


def test_a_check_that_couldnt_run_takes_nothing_off():
    c = all_ok()
    c["spf"] = check("SPF", d.INFO, "Couldn't look this up (Timeout).", unknown=True)
    c["age"] = check("Domain age", d.INFO, "Registry didn't publish a registration date.")
    c["blocklists"] = [check("Blocklist dbl.spamhaus.org", d.INFO, "list refused the query")]
    result = mh.health_score(c)
    assert result["score"] == 100
    by_rule = {i["rule"]: i for i in result["items"]}
    assert by_rule["SPF"]["result"].startswith("couldn't check")
    assert by_rule["Blocklists"]["result"] == "couldn't check any list"


def test_without_dns_the_score_is_partial():
    result = mh.health_score(None, paused=1)
    assert result["partial"] is True and result["score"] == 90
    assert list(items(result)) == ["Bounce rate", "Paused mailboxes"]


def test_the_formula_on_the_page_matches_the_rules():
    text = " ".join(mh.SCORE_FORMULA)
    for limit, points in mh.BOUNCE_TIERS:
        assert f"over {round(limit * 100)}%: -{points}" in text
    assert "-15" in text and "-5" in text and f"-{mh.BLOCKLIST_POINTS}" in text
    assert f"-{mh.MX_POINTS}" in text and f"-{mh.PAUSED_POINTS}" in text
    assert "not an inbox-placement score" in mh.SCORE_NOTE


# ── DNS checks: off the event loop, cached, deduplicated ──

def test_lookups_are_cached_for_an_hour_and_rechecked_at_most_once_a_minute(fresh_checks):
    checks, calls, clock = fresh_checks, fresh_checks.calls, fresh_checks.clock_value

    async def get(**kw):
        return await checks.get("sender-one.test", "", **kw)

    assert checks.cached("sender-one.test") is None
    asyncio.run(get())
    asyncio.run(get())
    assert len(calls) == 1 and checks.cached("sender-one.test") is not None
    clock[0] += 30
    asyncio.run(get(refresh=True))                  # too soon: the last answer
    assert len(calls) == 1
    clock[0] += 31
    asyncio.run(get(refresh=True))
    assert len(calls) == 2
    clock[0] += mh.DNS_TTL_SECONDS + 1
    assert checks.cached("sender-one.test") is None
    asyncio.run(get())
    assert len(calls) == 3


def test_two_requests_at_once_share_one_lookup():
    started = []

    async def slow(domain, selector=""):
        started.append(domain)
        await asyncio.sleep(0.05)
        return all_ok(domain)

    checks = mh.DomainChecks(run=slow)

    async def both():
        return await asyncio.gather(checks.get("sender-one.test"), checks.get("sender-one.test"))

    first, second = asyncio.run(both())
    assert started == ["sender-one.test"] and first is second


def test_the_real_checks_run_in_threads_and_never_raise(monkeypatch):
    seen = {}

    def ok(name):
        def run(domain, *args):
            seen[name] = (domain, *args)
            return d.Check(name, d.OK, "fine")
        return run

    for name in ("check_mx", "check_spf", "check_dmarc", "check_domain_age", "check_dkim"):
        monkeypatch.setattr(d, name, ok(name))

    def broken(domain):
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(d, "check_domain_blocklists", broken)
    result = asyncio.run(mh.run_domain_checks("sender-one.test", "s2024"))
    assert seen["check_dkim"] == ("sender-one.test", "s2024")
    assert result["spf"]["status"] == d.OK
    [lists] = result["blocklists"]
    assert lists["unknown"] is True and "RuntimeError" in lists["detail"]

    seen.clear()
    bad = asyncio.run(mh.run_domain_checks("not a domain"))
    assert not seen and bad["spf"]["unknown"] is True


def test_the_dkim_selector_comes_from_the_warmup_config(tokens):
    assert mh.dkim_selector(make_config()) == ""
    assert mh.dkim_selector(make_config(warmup={"dkim_selector": " google "})) == "google"


# ── The endpoint ──

@pytest.fixture
def client(tmp_path, monkeypatch, db):
    monkeypatch.setattr(dash, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(dash, "DB_PATH", db)
    monkeypatch.setattr(dash, "PID_FILE", tmp_path / "data" / "harvey.pid")
    monkeypatch.setattr(dash, "LOG_FILE", tmp_path / "data" / "harvey.log")
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")
    with TestClient(dash.app, base_url=ORIGIN,
                    headers={"Authorization": f"Bearer {dash.token_store.get()}"}) as c:
        yield c


def test_the_page_never_waits_on_dns_then_fills_it_in(client, seeded, fresh_checks):
    first = client.get("/api/mailboxes").json()
    assert fresh_checks.calls == []
    assert all(x["checking"] and x["checks"] is None and x["health"]["partial"] for x in first["domains"])

    data = client.get("/api/mailboxes?dns=wait").json()
    assert sorted(fresh_checks.calls) == [("sender-one.test", ""), ("sender-two.test", "")]
    one, two = data["domains"]
    assert not one["checking"] and one["checks"]["spf"]["status"] == d.OK
    # Seven sent is too few to judge bounces; SECOND is paused.
    assert (one["health"]["score"], two["health"]["score"]) == (90, 100)
    assert one["checked_at"] == "2026-11-03T15:00:00Z"

    again = client.get("/api/mailboxes").json()                      # cached now
    assert not again["domains"][0]["checking"] and len(fresh_checks.calls) == 2
    fresh_checks.clock_value[0] += 120
    client.get("/api/mailboxes?dns=wait&refresh=true")
    assert len(fresh_checks.calls) == 4


def test_the_endpoint_only_reads(client, seeded):
    before = {t: count(seeded, t) for t in ("settings", "actions", "outbox", "prospects", "conversations")}
    data = client.get("/api/mailboxes?dns=wait&tz=330").json()
    assert "error" not in data and data["summary"]["sent_total"] == 8
    assert {t: count(seeded, t) for t in before} == before


def test_a_failed_lookup_shows_on_the_card_not_as_an_error(client, seeded, monkeypatch):
    async def down(domain, selector=""):
        raise OSError("no network")

    monkeypatch.setattr(mh, "CHECKS", mh.DomainChecks(run=down))
    data = client.get("/api/mailboxes?dns=wait").json()
    assert all(x["check_failed"] and not x["checking"] for x in data["domains"])
    assert data["summary"]["sent_total"] == 8


def test_an_unreadable_config_is_an_error_not_a_blank_page(client, monkeypatch):
    def broken(*a, **k):
        raise config_mod.ConfigError("harvey.yaml: bad indentation")

    monkeypatch.setattr(config_mod, "load_config", broken)
    r = client.get("/api/mailboxes")
    assert r.status_code == 500 and "bad indentation" in r.json()["error"]


def test_the_tab_is_on_the_page():
    web = Path(dash.__file__).parent / "web"
    html = (web / "index.html").read_text(encoding="utf-8")
    js = (web / "app.js").read_text(encoding="utf-8")
    assert "showTab('mailboxes', this)" in html and 'id="mailboxes"' in html
    assert html.index("showTab('conversations'") < html.index("showTab('mailboxes'") < html.index("showTab('signals'")
    assert "case 'mailboxes': loadMailboxes(); break;" in js


# ── The view (app.js under node) ──

HELPERS = ("escHtml", "jsq", "parseTs", "relTime", "formatDate", "icon", "emptyState",
           "mbxPct", "mbxNum", "mbxStatusBadge", "mbxCheckBadge", "mbxResumeLink")


def render(func: str, *args) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    source = (Path(dash.__file__).parent / "web" / "app.js").read_text(encoding="utf-8")
    parts = ["const _mbxOpen = new Set();"]
    for name in (*HELPERS, func):
        match = re.search(rf"^function {name}\(.*?^}}\n", source, re.S | re.M)
        assert match, name
        parts.append(match.group(0))
    call = ", ".join(json.dumps(a) for a in args)
    script = "\n".join(parts) + f"\nprocess.stdout.write({func}({call}));"
    return subprocess.run([node, "-e", script], capture_output=True, text=True, encoding="utf-8",
                          check=True, timeout=30).stdout


def test_the_table_shows_why_and_points_to_the_outbox(client, seeded):
    setting(seeded, mb.paused_key(SECOND), "<img src=x onerror=alert(1)>")
    data = client.get("/api/mailboxes").json()
    html = render("mbxTableHtml", data)
    assert "Resume it on the Outbox tab" in html and "goTab('outbox')" in html
    assert "harvey sending resume --mailbox " + SECOND in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html and "<img src=x" not in html
    assert f"harvey gmail auth --mailbox {THIRD}" in html
    assert "Not connected" in html and "Paused" in html and "Sending" in html
    assert "1 since resume" in html


def test_the_domain_card_shows_the_score_and_its_working(client, seeded):
    first = client.get("/api/mailboxes").json()
    pending = render("mbxDomainCard", first["domains"][1], first)
    assert "Checking DNS" in pending and "pending DNS" in pending

    data = client.get("/api/mailboxes?dns=wait").json()
    html = render("mbxDomainCard", data["domains"][0], data)
    assert ">90<" in html and "Healthy" in html
    assert "Score: 100 &minus; 10 = 90" in html
    assert "Paused mailboxes" in html and "How the score is worked out" in html
    assert "not an inbox-placement score" in html
    assert "SPF" in html and "Pass" in html and "dbl.spamhaus.org" in html


def test_the_chart_has_a_row_per_mailbox_and_one_for_bounces(seeded, multi):
    daily = run_collect(seeded, multi)["daily"]                     # NOW's 14 days
    html = render("mbxChartHtml", daily)
    for name in ("All mailboxes", PRIMARY, SECOND, THIRD, "Bounces"):
        assert f'title="{name}"' in html
    assert html.count('class="mbx-bars"') == 5
    assert "4 emails sent, 0 bounces" in html


def test_the_results_panel(client, seeded):
    funnel = client.get("/api/mailboxes").json()["funnel"]
    html = render("mbxFunnelHtml", funnel)
    for label in ("Companies found", "Contacts with a sendable email", "Contacted", "Replied",
                  "Interested", "Ready to meet", "Opted out", "Not interested"):
        assert label in html
    assert "71%" in html                       # 5 of 7 contacted replied
