"""The Writer by website segment: campaigns, FACTS, playbooks and who waits.

Campaigns group by what a company's site runs on (SITE_PLATFORM), email 1
names only the platform and age facts the profile proved, an old site is
pitched as old only with signs of life, and the custom segment is written
only to verified addresses.
"""

import json
from types import SimpleNamespace

import pytest
import pytest_asyncio

import harvey.integrations.email_finder as email_finder
from harvey.agents.writer import SITE_CLAIMS_RULE, Writer
from harvey.models.company import Company
from harvey.models.prospect import COMPANY_INBOX_SOURCE, Prospect
from harvey.signals import seed_signal_catalog
from harvey.state import StateManager

SEQ = [{"step": n, "subject": f"s{n}", "body": f"b{n} {{{{company}}}}", "delay_days": 0 if n == 1 else 3}
       for n in (1, 2, 3)]
COPYRIGHT = "the copyright notice on the home page still says 2013"
VIEWPORT = "the home page has no mobile viewport tag, so phones show the desktop layout shrunk down"


class RecordingBrain:
    def __init__(self, reply):
        self.reply, self.prompts, self.skills = reply, [], []

    def load_skills_for_agent(self, name):
        return ""

    def load_prompt(self, name, **kw):
        return "prompt"

    def load_skill(self, name):
        self.skills.append(name)
        return f"PLAYBOOK FOR {name}" if name.startswith("segment_") else ""

    async def think_json(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.reply


def config(provider):
    return SimpleNamespace(
        channels=SimpleNamespace(email=SimpleNamespace(provider=provider, require_approval=True)),
        product=SimpleNamespace(name="P", description="D", key_benefits=[], pricing=""),
        persona=SimpleNamespace(name="S", company="C", role="R", tone="t"),
        humanizer=SimpleNamespace(enabled=False),
    )


def writer(brain, state, provider="gmail"):
    w = Writer(brain, state, config(provider))
    w.skills = ""
    return w


@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(str(tmp_path / "w.db"))
    await s.init_db()
    await seed_signal_catalog(s)
    return s


@pytest.fixture
def mx(monkeypatch):
    """The domain's MX lookup, without DNS. Set ``mx.host = None`` for no mail server."""
    holder = SimpleNamespace(host="mx.example.net", asked=[])

    async def lookup(domain):
        holder.asked.append(domain)
        return holder.host
    monkeypatch.setattr(email_finder, "get_mx_host", lookup)
    return holder


async def shop(state, n, platform=None, score=None, facts=(), **kw):
    cid = await state.add_company(Company(name=f"Shop {n}", domain=f"shop{n}.example", **kw))
    if platform:
        await state.add_observation("SITE_PLATFORM", company_id=cid, collector="profile",
                                    value_text=platform, confidence=0.9)
    if score is not None:
        await state.add_observation("LEGACY_SITE_SCORE", company_id=cid, collector="profile", value_num=score)
    if facts or platform:
        await state.add_observation("SITE_AGE_FACTS", company_id=cid, collector="profile",
                                    value_num=len(facts), value_text=json.dumps(
                                        [{"code": "X", "sentence": s} for s in facts]))
    return cid


async def person(state, cid, n, status="verified", inbox=False, industry="Garden supplies"):
    fields = dict(company_id=cid, company=f"Shop {n}", industry=industry,
                  email=f"{'info' if inbox else 'pat'}@shop{n}.example", email_status=status)
    if inbox:
        fields.update(title="General inbox", source=COMPANY_INBOX_SOURCE)
    else:
        fields.update(first_name=f"Pat{n}", last_name="Doe", title="Owner")
    return await state.add_prospect(Prospect(**fields))


async def campaign_names(state):
    return {c.name for c in await state.get_campaigns_by_status("draft")}


@pytest.mark.asyncio
async def test_campaigns_are_grouped_by_segment(state, mx):
    await person(state, await shop(state, 1, "shopify"), 1)
    await person(state, await shop(state, 2, "oscommerce", 60), 2)
    await person(state, await shop(state, 3, "woocommerce"), 3)
    await person(state, await shop(state, 4, "zencart"), 4, inbox=True)
    await person(state, await shop(state, 5), 5, industry="Roofing")          # never profiled
    await person(state, await shop(state, 6, "unknown", 55), 6)               # old, unrecognised
    await writer(RecordingBrain(SEQ), state, "instantly").run()
    assert await campaign_names(state) == {
        "Shopify-outreach", "Custom-outreach", "WordPress-outreach", "Custom-inbox-outreach", "Other-outreach"}


@pytest.mark.asyncio
async def test_the_custom_segment_is_written_to_verified_addresses_only(state, mx):
    legacy = await shop(state, 1, "cubecart", 50)
    risky = await person(state, legacy, 1, status="risky")
    modern = await shop(state, 2, "shopify")
    risky_shopify = await person(state, modern, 2, status="risky")
    await writer(RecordingBrain(SEQ), state, "instantly").run()
    assert (await state.get_prospect(risky)).status == "new"            # waits, unwritten
    assert (await state.get_prospect(risky_shopify)).status != "new"
    assert await campaign_names(state) == {"Shopify-outreach"}
    assert await state.count_writable_prospects() == 0                  # the heartbeat agrees


@pytest.mark.asyncio
async def test_facts_carry_the_platform_and_the_proven_age_facts(state, mx):
    cid = await shop(state, 1, "oscommerce", 70, facts=(COPYRIGHT, VIEWPORT))
    pid = await person(state, cid, 1)
    brain = RecordingBrain({"subject": "your shop", "body": "Hi Pat"})
    await writer(brain, state)._write_personal_email(await state.get_prospect(pid))
    [prompt] = brain.prompts
    assert "Website platform (read from their home page): osCommerce" in prompt
    assert f"Seen on their website: {COPYRIGHT}" in prompt
    assert f"Seen on their website: {VIEWPORT}" in prompt
    assert SITE_CLAIMS_RULE.strip() in prompt
    assert "PLAYBOOK FOR segment_custom" in prompt
    assert mx.asked == ["shop1.example"]


@pytest.mark.asyncio
async def test_an_old_site_without_signs_of_life_is_written_as_generic(state, mx):
    mx.host = None              # no mail server: not pitched as an old site
    cid = await shop(state, 1, "oscommerce", 70, facts=(COPYRIGHT,))
    pid = await person(state, cid, 1)
    brain = RecordingBrain({"subject": "your shop", "body": "Hi Pat"})
    await writer(brain, state)._write_personal_email(await state.get_prospect(pid))
    [prompt] = brain.prompts
    assert "- Seen on their website:" not in prompt
    assert "PLAYBOOK FOR segment_custom" not in prompt
    assert "osCommerce" in prompt                       # the platform is still a fact
    assert SITE_CLAIMS_RULE.strip() in prompt           # and nothing else may be claimed


@pytest.mark.asyncio
async def test_a_dissolved_company_is_never_pitched_as_old(state, mx):
    cid = await shop(state, 1, "zencart", 70, facts=(COPYRIGHT,), notes="Dissolved in 2025")
    pid = await person(state, cid, 1)
    brain = RecordingBrain({"subject": "s", "body": "b"})
    await writer(brain, state)._write_personal_email(await state.get_prospect(pid))
    assert "- Seen on their website:" not in brain.prompts[0]


@pytest.mark.asyncio
async def test_an_unprofiled_company_gets_no_site_claims(state, mx):
    pid = await person(state, await shop(state, 1), 1)
    brain = RecordingBrain({"subject": "s", "body": "b"})
    await writer(brain, state)._write_personal_email(await state.get_prospect(pid))
    [prompt] = brain.prompts
    assert "- Website platform" not in prompt and "- Seen on their website:" not in prompt
    assert SITE_CLAIMS_RULE.strip() not in prompt
    assert mx.asked == []


@pytest.mark.asyncio
async def test_the_batch_template_gets_the_segment_playbook_but_no_age_facts(state, mx):
    a = await person(state, await shop(state, 1, "wordpress", 45, facts=(COPYRIGHT,)), 1)
    b = await person(state, await shop(state, 2, "woocommerce", 50, facts=(VIEWPORT,)), 2)
    brain = RecordingBrain(SEQ)
    await writer(brain, state, "instantly")._write_sequence(
        [await state.get_prospect(a), await state.get_prospect(b)])
    [prompt] = brain.prompts
    assert "PLAYBOOK FOR segment_wordpress" in prompt
    assert COPYRIGHT not in prompt and VIEWPORT not in prompt

    # One site in the batch can't be pitched as old: the template is generic.
    c = await person(state, await shop(state, 3, "wordpress", 10), 3)
    brain = RecordingBrain(SEQ)
    await writer(brain, state, "instantly")._write_sequence(
        [await state.get_prospect(a), await state.get_prospect(c)])
    assert "PLAYBOOK FOR segment_wordpress" not in brain.prompts[0]


@pytest.mark.asyncio
async def test_shopify_gets_its_playbook_without_an_old_site_check(state, mx):
    pid = await person(state, await shop(state, 1, "shopify", 0), 1)
    brain = RecordingBrain({"subject": "s", "body": "b"})
    await writer(brain, state)._write_personal_email(await state.get_prospect(pid))
    assert "PLAYBOOK FOR segment_shopify" in brain.prompts[0]
    assert "Website platform (read from their home page): Shopify" in brain.prompts[0]
