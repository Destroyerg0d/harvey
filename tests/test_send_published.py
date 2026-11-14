"""Addresses a company prints on its own website go out when no verifier can
judge them, while channels.email.send_published is on (owner, 2026-11-14: the
verifier credits ran out and cold email stopped). Guessed patterns never do.
All domains here are fictional."""

from types import SimpleNamespace as NS

import pytest

from harvey.gate import PUBLISHED, pre_send_check
from harvey.integrations import email_finder as ef
from harvey.models.prospect import Prospect


@pytest.fixture
def no_verdict(monkeypatch):
    async def mx(domain):
        return "smtp.google.com"

    async def check(email, mx_type, env, state=None):
        return None, False         # every verifier out of credit

    monkeypatch.setattr(ef, "get_mx_host", mx)
    monkeypatch.setattr(ef, "_verify_candidate", check)


@pytest.mark.asyncio
async def test_a_published_inbox_no_verifier_could_judge(no_verdict):
    assert await ef.verify_published_address("info@shop.example") == "guess"
    assert await ef.verify_published_address("info@shop.example", unverified=PUBLISHED) == PUBLISHED


@pytest.mark.asyncio
async def test_a_verdict_still_wins_over_published(monkeypatch):
    async def mx(domain):
        return "smtp.google.com"

    async def invalid(email, mx_type, env, state=None):
        return "invalid", False
    monkeypatch.setattr(ef, "get_mx_host", mx)
    monkeypatch.setattr(ef, "_verify_candidate", invalid)
    assert await ef.verify_published_address("info@shop.example", unverified=PUBLISHED) == "invalid"


@pytest.mark.asyncio
async def test_a_persons_own_published_address_but_never_a_pattern(no_verdict):
    own = await ef.find_email("Ann", "Lee", "shop.example", known_emails=[("ann@shop.example", "Ann", "Lee")],
                              published=PUBLISHED)
    assert (own.email, own.status) == ("ann@shop.example", PUBLISHED)
    pattern = await ef.find_email("Bo", "Ng", "shop.example", published=PUBLISHED)
    assert pattern.status == "guess"


def test_the_gate_sends_published_only_when_allowed():
    p = Prospect(first_name="Ann", last_name="Lee", email="info@shop.example", email_status=PUBLISHED)
    held = pre_send_check("info@shop.example", "Hello", "A short note.", prospect=p, allow_risky=True)
    assert any("not deliverable" in r for r in held.reasons)
    ok = pre_send_check("info@shop.example", "Hello", "A short note.", prospect=p, allow_risky=True,
                        allow_published=True)
    assert not any("not deliverable" in r for r in ok.reasons)


def test_scout_grades_published_only_with_the_switch():
    from harvey.agents.scout import Scout
    scout = Scout.__new__(Scout)
    scout.config = NS(channels=NS(email=NS(send_published=True)))
    assert scout._published_grade() == PUBLISHED
    scout.config = NS(channels=NS(email=NS(send_published=False)))
    assert scout._published_grade() == "guess"
