"""The site review a yes gets (owner, 2026-11-14): measured facts, two or three
fixes, a call link, and it waits for approval until reviews autopilot is on.
Every site, name and address here is fictional; nothing is measured for real."""

import asyncio
from types import SimpleNamespace as NS

import pytest

from harvey import site_review as sr
from harvey.site_review import (
    HELD_REASON, REVIEW_ANGLE, REVIEW_AUTOPILOT, REVIEW_AUTOPILOT_KEY, Measured, facts,
    offered_review, review_problem,
)
from harvey.state import HELD_FOR_REVIEW
from tests.test_auto_replies import Brain, person
from tests.test_outbox_native import (  # noqa: F401  (state is a fixture)
    Cfg, Env, FakeProvider, InboundMessage, make_sender, state,
)

BOOK = "https://book.studio.example/call/"
OFFER = "Fixing that is most of what we do. Want me to write up the three changes I'd make first?"


def slow_shop() -> Measured:
    return Measured(url="https://shop.example/", ok=True, measured_on="2026-11-14", lcp_s=6.4,
                    lcp_what="image", cls=0.02, fcp_s=1.9, bytes_total=4 * 1024 * 1024, requests=140,
                    image_bytes=3 * 1024 * 1024, heaviest_images=[("hero.jpg", 880), ("logo.png", 40)],
                    head_blocking=6, images=20, no_alt=7, description=False, shopify=True, theme="Dawn")


REVIEW = """Hi Ann,

Here are the three changes I'd make first, from a test of your home page on a phone.

1. The hero image, hero.jpg, is 880 KB and the largest element showed after 6.4 seconds. I'd resize and compress it.
2. 6 scripts in the page head hold up the first paint. I'd defer them.
3. 7 of the 20 images have no alt text. I'd add short descriptions.

Want to walk through them on a 15-minute call? Pick a time here: https://book.studio.example/call/"""


class ReviewCfg(Cfg):
    class product(Cfg.product):
        class offer:
            booking_url = BOOK

    class site_review:
        enabled = True
        pagespeed = False
        timeout_seconds = 30


def config():
    return ReviewCfg()


# ── Facts and the checks on the text ──

def test_facts_state_what_was_measured_with_its_numbers():
    lines = facts(slow_shop())
    text = "\n".join(lines)
    assert "6.4 seconds" in text and "2.5 seconds" in text
    assert "4.0 MB in 140 requests" in text and "hero.jpg at 880 KB" in text
    assert "logo.png" not in text                        # small images aren't a problem
    assert "6 scripts" in text and "7 of the 20 images" in text and "no meta description" in text
    assert lines[-1] == "The site runs on Shopify, theme Dawn."


def test_a_healthy_site_has_only_the_plain_measurements():
    fine = Measured(url="https://fine.example/", ok=True, measured_on="2026-11-14", lcp_s=1.8,
                    cls=0.01, fcp_s=1.2, bytes_total=900 * 1024, requests=30, images=5,
                    title="Fine Shop: handmade candles")
    assert len(facts(fine)) == 1 and facts(fine)[0].startswith("Measured on 2026-11-14")


def test_a_good_review_passes():
    assert review_problem(REVIEW, facts(slow_shop()), config(), "shop.example", "email") == ""


@pytest.mark.parametrize("change, why", [
    (lambda t: t.replace("6.4 seconds", "9.1 seconds"), "numbers the measurements don't have"),
    (lambda t: t + "\nSee https://elsewhere.example/guide", "neither their site nor the booking page"),
    (lambda t: t.replace(BOOK, "our site"), "booking link"),
    (lambda t: t.replace("1. ", "").replace("2. ", "").replace("3. ", ""), "number the fixes"),
    (lambda t: t.replace("Want to walk", "It's free. Want to walk"), "'free'"),
    (lambda t: "", "empty"),
])
def test_a_review_that_invents_or_links_elsewhere_is_refused(change, why):
    assert why in review_problem(change(REVIEW), facts(slow_shop()), config(), "shop.example", "email")


def test_a_link_to_their_own_site_is_fine():
    text = REVIEW.replace("from a test of your home page", "from a test of https://shop.example/")
    assert review_problem(text, facts(slow_shop()), config(), "shop.example", "email") == ""


def test_what_counts_as_offering_and_promising_the_write_up():
    assert offered_review([OFFER])
    assert offered_review(["I found a few issues on your site. Want me to send the list?"])
    assert not offered_review(["Thanks for connecting. How do you handle product photos today?"])
    assert sr.PROMISED_RE.search("I'll write it up and send it within two working days.")
    assert not sr.PROMISED_RE.search("Happy to help. When suits you?")


def test_dashes_markdown_and_emojis_are_tidied():
    assert sr.tidy("**Fix** the hero — it is heavy 🚀") == "Fix the hero, it is heavy"


def test_site_url_and_their_site():
    assert sr.site_url("shop.example") == "https://shop.example/"
    assert sr.site_url("http://shop.example/collections/all") == "http://shop.example/"
    assert sr.domain_of_email("ann@gmail.com") == ""
    assert sr.domain_of_email("ann@shop.example") == "shop.example"


# ── Email: a yes to the offer ──

def handler_with(state, inbound, brain):
    from harvey.agents.handler import Handler
    provider = FakeProvider()
    provider.inbound = list(inbound)
    h = Handler(brain=brain, state=state, config=config(), env=Env())
    h.provider = provider
    return h


async def offered(state, email="ann@shop.example"):
    pid = await person(state, email)
    item = await state.add_outbox_item(prospect_id=pid, to_email=email, subject="quick question",
                                       body=OFFER, send_at="2026-11-10T10:00:00", status="approved",
                                       campaign_id="c1", step=2)
    await state.update_outbox_item(item, status="sent", sent_at="2026-11-10T10:00:00",
                                   message_id="<s2@mail.example>")
    return pid


def yes(sender="ann@shop.example", body="Yes please, send them over."):
    return InboundMessage(provider_id="y1", from_email=sender, subject="Re: quick question", body=body,
                          message_id="<y1@shop.example>", thread_ref="t-y1",
                          headers={"from": f"Ann Sample <{sender}>"}, in_reply_to="<s2@mail.example>")


@pytest.fixture
def measured(monkeypatch):
    calls = []

    async def fake_measure(url, config=None, env=None):
        calls.append(url)
        return slow_shop()
    monkeypatch.setattr(sr, "measure", fake_measure)
    return calls


@pytest.mark.asyncio
async def test_a_yes_gets_the_review_itself_held_for_you(state, measured):
    pid = await offered(state)
    h = handler_with(state, [yes()], Brain("interested", reply=REVIEW))
    await h._run_native()
    assert measured == ["https://shop.example/"]
    [item] = await state.get_outbox(status="pending_review")
    assert item["angle"] == REVIEW_ANGLE and item["body"] == REVIEW
    assert item["error"] == (HELD_FOR_REVIEW + HELD_REASON)[:300]
    assert item["in_reply_to"] == "<y1@shop.example>"
    [convo] = await state.get_conversations_by_status("open")
    assert convo.stage == "presenting"
    assert "generate_response" not in h.brain.calls     # no promise of a review: the review


@pytest.mark.asyncio
async def test_with_reviews_autopilot_on_it_goes_and_turning_it_off_holds_it_again(state, measured):
    pid = await offered(state)
    await state.set_setting(REVIEW_AUTOPILOT_KEY, "on")
    await handler_with(state, [yes()], Brain("interested", reply=REVIEW))._run_native()
    [item] = await state.get_outbox(status="approved")
    assert item["approved_by"] == REVIEW_AUTOPILOT
    sender = make_sender(state, FakeProvider())
    sender.config = config()
    prospect = await state.get_prospect(pid)
    assert await sender._blocked(item, prospect, allow_risky=False) is None
    await state.delete_setting(REVIEW_AUTOPILOT_KEY)
    held = await sender._blocked(item, prospect, allow_risky=False)
    assert held[0] == "pending_review" and HELD_REASON in held[1]


@pytest.mark.asyncio
async def test_a_site_that_cant_be_measured_goes_to_you(state, monkeypatch):
    async def down(url, config=None, env=None):
        return Measured(url=url, why="the home page answered 503")
    monkeypatch.setattr(sr, "measure", down)
    await offered(state)
    await handler_with(state, [yes()], Brain("interested", reply=REVIEW))._run_native()
    assert await state.get_outbox(status="pending_review") == []
    [convo] = await state.get_conversations_by_status("needs_human")
    assert convo.prospect_id


@pytest.mark.asyncio
async def test_only_one_review_each_and_none_without_the_offer(state, measured):
    await offered(state)
    await handler_with(state, [yes()], Brain("interested", reply=REVIEW))._run_native()
    [review] = await state.get_outbox(status="pending_review")
    await state.update_outbox_item(review["id"], status="sent", sent_at="2026-11-14T10:00:00")
    second = yes(body="Thanks, also could you look at the checkout?")
    second.provider_id, second.message_id = "y2", "<y2@shop.example>"
    await handler_with(state, [second], Brain("question", reply="Sure. When suits you?"))._run_native()
    assert len(measured) == 1
    # Someone we never offered it to gets the ordinary answer.
    pid = await person(state, "bo@other.example")
    other = yes(sender="bo@other.example", body="Yes, interested.")
    other.provider_id, other.message_id = "y3", "<y3@other.example>"
    await handler_with(state, [other], Brain("interested", reply="Great. When suits you?"))._run_native()
    assert len(measured) == 1


# ── LinkedIn ──

def test_linkedin_a_yes_gets_a_held_review(tmp_path, measured):
    from tests.test_referrals import linkedin_world
    st, agent = linkedin_world(tmp_path, Brain("interested", reply=REVIEW))
    agent.config.product.offer.booking_url = BOOK

    async def go():
        lead, _ = await st.li_upsert_lead("ann-doe-1", stage="messaged", name="Ann Doe", first_name="Ann")
        company = await st.add_company(__import__("harvey.models.company", fromlist=["Company"]).Company(
            name="Shop", domain="shop.example"))
        await st.li_update_lead(lead, company_id=company)
        await st.li_add_message(lead, direction="out", kind="message", body=OFFER, status="sent")
        await st.li_add_message(lead, direction="in", kind="message", body="Yes please", status="sent")
        await agent.handle_inbound()
        return await st.li_messages(lead_id=lead, direction="out", statuses=("draft",))
    [draft] = asyncio.run(go())
    assert draft["kind"] == "reply" and "6.4 seconds" in draft["body"]
    assert draft["error"].endswith(HELD_REASON[:40]) or HELD_REASON[:40] in draft["error"]
    assert measured == ["https://shop.example/"]


# ── The ones already promised ──

@pytest.mark.asyncio
async def test_promised_reviews_are_found_and_written_held(state, measured):
    lead, _ = await state.li_upsert_lead("bo-lee-1", stage="warm", name="Bo Lee", first_name="Bo",
                                         company="Lee Goods")
    await state.li_add_message(lead, direction="in", kind="message", body="Yes please", status="sent")
    await state.li_add_message(lead, direction="out", kind="reply", status="sent",
                               body="Good to hear. I'll write up the three changes I'd make first and get "
                                    "them to you within two working days.")
    other, _ = await state.li_upsert_lead("cy-ng-1", stage="warm", name="Cy Ng", first_name="Cy")
    await state.li_add_message(other, direction="out", kind="reply", status="sent",
                               body="Thanks Cy, how do you handle product photos today?")
    [p] = await sr.promised(state)
    assert p["channel"] == "linkedin" and p["who"] == "Bo Lee" and p["site"] == ""
    [r] = await sr.write_promised(state, Brain("interested", reply=REVIEW), config(), Env())
    assert not r["done"] and r["why"] == sr.NO_SITE                 # no site, nothing guessed
    with pytest.raises(ValueError):
        await sr.write_promised(state, Brain("interested", reply=REVIEW), config(), Env(),
                                only="nobody", site="shop.example")   # must name one person
    [r] = await sr.write_promised(state, Brain("interested", reply=REVIEW), config(), Env(),
                                  only="bo lee", site="shop.example")
    assert r["done"] and measured == ["https://shop.example/"]
    [draft] = await state.li_messages(lead_id=lead, direction="out", statuses=("draft",))
    assert HELD_REASON[:30] in draft["error"]
    assert await sr.promised(state) == []                            # one each


# ── Dashboard ──

def test_the_dashboard_switch_needs_the_word(client):  # noqa: F811
    r = client.get("/api/reviews")
    assert r.status_code == 200 and r.json()["autopilot"] is False
    assert client.post("/api/reviews/autopilot", json={"on": True}).status_code == 400
    assert client.post("/api/reviews/autopilot", json={"on": True, "confirm": "reviews"}).json()["autopilot"]
    assert client.get("/api/reviews").json()["autopilot"] is True
    assert client.post("/api/reviews/autopilot", json={"on": False}).json()["autopilot"] is False


from tests.test_dashboard_signals import client  # noqa: E402,F401  (fixture)
