"""Profiling runs in the background, one batch at a time, so the send phase
never waits for a batch of slow sites."""

import asyncio

import pytest

import harvey.main as main_mod


@pytest.mark.asyncio
async def test_a_batch_profiles_with_the_configured_size(monkeypatch):
    calls = []

    async def profile(state, limit=100, config=None):
        calls.append(limit)
        return limit, 0, "run"
    monkeypatch.setattr(main_mod, "run_profile_stage", profile)
    await main_mod._profile_in_background(None, None)
    assert calls == [main_mod.PROFILE_BATCH]


@pytest.mark.asyncio
async def test_a_slow_batch_is_stopped_and_a_failing_one_never_raises(monkeypatch, caplog):
    async def slow(state, limit=100, config=None):
        await asyncio.sleep(10)
    monkeypatch.setattr(main_mod, "run_profile_stage", slow)
    monkeypatch.setattr(main_mod, "CYCLE_TIMEOUT_SECONDS", 0.01)
    await main_mod._profile_in_background(None, None)
    assert "was stopped" in caplog.text

    async def broken(state, limit=100, config=None):
        raise RuntimeError("site exploded")
    monkeypatch.setattr(main_mod, "run_profile_stage", broken)
    await main_mod._profile_in_background(None, None)
    assert "Profiling failed: site exploded" in caplog.text


@pytest.mark.asyncio
async def test_sending_does_not_wait_for_a_batch(monkeypatch):
    """The batch is a task of its own: whatever awaits the cycle's agents
    finishes while the batch is still reading sites."""
    started, release = asyncio.Event(), asyncio.Event()

    async def profile(state, limit=100, config=None):
        started.set()
        await release.wait()
    monkeypatch.setattr(main_mod, "run_profile_stage", profile)
    batch = asyncio.create_task(main_mod._profile_in_background(None, None))
    await asyncio.wait_for(started.wait(), 1)
    assert await asyncio.wait_for(main_mod._run_agents([]), 1) == {}
    assert not batch.done()
    release.set()
    await asyncio.wait_for(batch, 1)
