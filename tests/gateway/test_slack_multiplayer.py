"""Behavior tests for gateway.slack_multiplayer task-card settlement tracking."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from gateway import slack_multiplayer


class _FakeCardState:
    def __init__(self, tasks: list[dict[str, str]]) -> None:
        self.tasks = {t["id"]: t for t in tasks}
        self.task_order = [t["id"] for t in tasks]

    def visible_tasks(self) -> list[dict[str, str]]:
        return [self.tasks[i] for i in self.task_order[-8:]]


def test_task_card_publish_does_not_mark_settled_tasks_published_on_error(monkeypatch):
    """If publishing raises, scrolled-out finished tasks must remain pending for the next publish."""
    monkeypatch.setattr(slack_multiplayer, "enabled", lambda: True)
    monkeypatch.setattr(slack_multiplayer, "_CARD_MIN_INTERVAL", 0.0)

    seen_frames: list[list[str]] = []
    attempts = 0

    @slack_multiplayer._make_task_card_publish
    async def publish(_self, st):
        nonlocal attempts
        attempts += 1
        seen_frames.append([t["id"] for t in st.visible_tasks()])
        if attempts == 1:
            raise RuntimeError("transient slack error")
        return "ok"

    runner = SimpleNamespace(
        _task_card_drain=lambda _st: False,
        _ctx=SimpleNamespace(_run_still_current=lambda: True),
        _agent_interrupted=lambda: False,
    )
    tasks = [{"id": f"t{i}", "status": "complete"} for i in range(10)]
    st = _FakeCardState(tasks)

    with pytest.raises(RuntimeError, match="transient slack error"):
        asyncio.run(publish(runner, st))
    assert seen_frames == [[f"t{i}" for i in range(10)]]

    # Next publish must still include scrolled-out t0 and t1 because the first publish failed.
    assert asyncio.run(publish(runner, st)) == "ok"
    assert seen_frames[1] == [f"t{i}" for i in range(10)]

    # Once published cleanly, scrolled-out t0 and t1 are settled and no longer resent.
    assert asyncio.run(publish(runner, st)) == "ok"
    assert seen_frames[2] == [f"t{i}" for i in range(2, 10)]
