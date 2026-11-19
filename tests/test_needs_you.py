"""Needs you names everything waiting on the owner (owner, 2026-11-19: "you
promise to give them improvement list ... you are also not telling me so ...
give me in needs you"). Every name, address and number here is fictional."""

import pytest

from harvey import needs_you, owner_updates
from harvey import meeting_alert as ma
from harvey.gate import autopilot_review
from harvey.linkedin.store import HELD_FOR_REVIEW
from harvey.linkedin.web import today_items as linkedin_today
from harvey.site_review import HELD_REASON
from tests.test_outbox_native import state  # noqa: F401  (fixture)

OWNER = "+15555550199"


async def lead(st, slug, name):
    lead_id, _ = await st.li_upsert_lead(slug, stage="replied", name=name, first_name=name.split()[0],
                                         profile_url=f"https://www.linkedin.com/in/{slug}/")
    return lead_id


@pytest.mark.asyncio
async def test_every_held_thing_is_its_own_item_with_site_reviews_first(state):
    ann = await lead(state, "ann-doe-1", "Ann Doe")
    bo = await lead(state, "bo-roe-2", "Bo Roe")
    await state.li_add_message(bo, kind="message", body="Saw shop.example", status="draft",
                               error=HELD_FOR_REVIEW + "links to shop.example, not one of your domains")
    await state.li_add_message(ann, kind="reply", body="Three fixes for your site.", status="draft",
                               error=HELD_FOR_REVIEW + HELD_REASON)
    await state.li_add_message(ann, kind="message", body="Plain draft", status="draft")   # not held
    contact, _ = await state.wa_add_contact(phone="+15555550123", name="Cy Poe", source="website chat",
                                            stage="needs_review")
    out = await needs_you.items(state)
    titles = [it["title"] for it in out]
    assert titles.index("Site review for Ann Doe (LinkedIn) is waiting for you") < \
        titles.index("LinkedIn message to Bo Roe is held for you")
    assert "WhatsApp number to check: Cy Poe" in titles
    assert not any("Plain draft" in it["detail"] for it in out)
    assert all(it["tab"] in ("linkedin", "whatsapp", "outbox", "conversations") for it in out)
    # The count card leaves out what is listed one by one: one plain draft.
    [card] = [it for it in await linkedin_today(state) if it["key"] == "linkedin-drafts"]
    assert card["title"].startswith("1 LinkedIn draft")


@pytest.mark.asyncio
async def test_a_promise_with_no_website_says_so(state, monkeypatch):
    import harvey.site_review as sr

    async def promised(st):
        return [{"channel": "linkedin", "lead_id": "L1", "name": "Dee", "who": "Dee Fox", "site": ""}]
    monkeypatch.setattr(sr, "promised", promised)
    [item] = await needs_you.items(state)
    assert item["title"] == "Promised Dee Fox an improvement list on LinkedIn: not sent yet"
    assert "No website is known" in item["detail"] and item["tone"] == "bad"


@pytest.mark.asyncio
async def test_each_item_reaches_the_owner_once(state):
    ann = await lead(state, "ann-doe-1", "Ann Doe")
    await state.li_add_message(ann, kind="reply", body="Three fixes.", status="draft",
                               error=HELD_FOR_REVIEW + HELD_REASON)
    assert await needs_you.announce(state) == 1
    assert await needs_you.announce(state) == 0          # already said
    lines = owner_updates.lines_for([("needs_you", {"title": "Site review for Ann Doe (LinkedIn) is "
                                                              "waiting for you"})])
    assert lines == ["Needs you: Site review for Ann Doe (LinkedIn) is waiting for you"]
    assert owner_updates.lines_for([("site_review", {"ok": False, "name": "Ann Doe",
                                                     "why": "no website to measure"})]) == \
        ["Couldn't write a site review for Ann Doe (no website to measure)."]


def test_a_message_naming_their_own_website_isn_t_held():
    from types import SimpleNamespace as NS
    config = NS(persona=NS(email="me@studio.example", website=""),
                channels=NS(email=NS(link_domains=[], mailboxes=[])),
                product=NS(offer=NS(booking_link="", meeting_link="", chat_link="")))
    body = "Saw your shop at their-shop.example, the product pages load fast."
    assert autopilot_review("", body, config, root=None) != []
    assert autopilot_review("", body, config, own_sites=("https://their-shop.example/",)) == []
    assert autopilot_review("", "see evil.example", config,
                            own_sites=("https://their-shop.example/",)) != []


def test_the_owner_s_own_alert_isn_t_counted_as_a_send(tmp_path):
    from tests.whatsapp_fakes import Person, World, run
    w = World(tmp_path, [Person(phone=OWNER)], alert_number=OWNER)
    run(ma.queue_to_owner(w.state, w.config, "Harvey update\nSent 2 follow-ups."))
    report = w.go()
    assert report["sent"] == 0 and report["alerts"] == 1
    assert owner_updates.lines_for([("whatsapp_run", {k: report.get(k) for k in ("outcome", "sent", "inbound")})]) == []
