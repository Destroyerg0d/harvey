"""channels.email.review_segments: the first emails of a listed segment wait
for you. A few are held in the Outbox as samples and the rest wait in the
queue; the first one you approve lets that segment go out on its own."""

from datetime import datetime, timezone

import pytest

from harvey.agents.sender import REVIEW_SAMPLES, SEGMENT_REVIEWED_KEY
from harvey.config import EmailChannelConfig
from harvey.models.prospect import Prospect
from harvey.state import HELD_FOR_REVIEW
from tests.test_outbox_native import FakeProvider, make_sender, state  # noqa: F401


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture
def sender(state, monkeypatch):
    s = make_sender(state, FakeProvider(), require_approval=False)
    monkeypatch.setattr(s.config.channels.email, "review_segments", ["custom"], raising=False)
    return s


async def first_email(state, n, step=1, approved_by="autopilot", segment="custom"):
    email = f"owner{n}@oldtools{n}.example"
    pid = await state.add_prospect(Prospect(
        first_name="Pat", last_name="Lee", title="Director", company=f"Old Tools {n}",
        email=email, email_status="verified", email_verified=True, status="new"))
    oid = await state.add_outbox_item(
        prospect_id=pid, to_email=email, subject="your website", body="Hi Pat,\n\nA question.",
        send_at=_now().isoformat(), status="approved", campaign_id="c1", step=step)
    item = {**await state.get_outbox_item(oid), "approved_by": approved_by, "segment": segment}
    return item, await state.get_prospect(pid)


async def hold(sender, state, item, prospect):
    """What the drain does with _blocked's answer."""
    blocked = await sender._blocked(item, prospect, False)
    if blocked:
        await state.update_outbox_item(item["id"], status=blocked[0], error=blocked[1])
    return blocked


@pytest.mark.asyncio
async def test_a_few_samples_wait_for_you_and_the_rest_wait_in_the_queue(sender, state):
    for n in range(REVIEW_SAMPLES):
        status, error = await hold(sender, state, *await first_email(state, n))
        assert status == "pending_review"
        assert error.startswith(HELD_FOR_REVIEW) and "sample of the first Custom emails" in error
    item, prospect = await first_email(state, 99)
    status, error = await hold(sender, state, item, prospect)
    assert status == "approved" and "Custom samples" in error
    later = await state.get_outbox_item(item["id"])
    assert later["status"] == "approved" and later["send_at"] > item["send_at"]


@pytest.mark.asyncio
async def test_other_segments_and_follow_ups_are_not_held(sender, state):
    assert await sender._segment_sample(*await first_email(state, 1, segment="shopify")) is None
    assert await sender._segment_sample(*await first_email(state, 2, step=2)) is None


@pytest.mark.asyncio
async def test_approving_one_sample_lets_the_segment_go_on_its_own(sender, state):
    held, _ = await first_email(state, 1)
    await hold(sender, state, held, await state.get_prospect(held["prospect_id"]))
    # You approve that sample: it goes, and the segment is reviewed.
    mine = {**held, "approved_by": "you", "status": "approved"}
    assert await sender._segment_sample(mine, None) is None
    assert await state.get_setting(SEGMENT_REVIEWED_KEY + "custom")
    assert await sender._segment_sample(*await first_email(state, 2)) is None


@pytest.mark.asyncio
async def test_no_list_means_no_samples(state):
    s = make_sender(state, FakeProvider(), require_approval=False)
    assert not getattr(s.config.channels.email, "review_segments", [])
    assert await s._segment_sample(*await first_email(state, 1)) is None


def test_review_segments_are_checked():
    assert EmailChannelConfig(review_segments="WordPress").review_segments == ["wordpress"]
    assert EmailChannelConfig(review_segments=["custom", "custom"]).review_segments == ["custom"]
    with pytest.raises(ValueError):
        EmailChannelConfig(review_segments=["magento"])
