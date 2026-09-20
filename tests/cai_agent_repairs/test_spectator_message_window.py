import asyncio
from types import SimpleNamespace

import pytest

from cai.api import spectator_routes


def test_extract_window_keeps_latest_user_assistant_messages_once():
    history = []
    for index in range(5):
        history.extend(
            [
                {"role": "user", "content": f"user-{index}"},
                {"role": "assistant", "content": f"assistant-{index}"},
            ]
        )
    history.extend(
        [
            {"role": "system", "content": "hidden system prompt"},
            {"role": "tool", "content": "large tool output"},
        ]
    )
    session = SimpleNamespace(history=history)

    window = spectator_routes._extract_window(session, 6, "user-4")

    assert window == [
        ("user", "user-2"),
        ("assistant", "assistant-2"),
        ("user", "user-3"),
        ("assistant", "assistant-3"),
        ("user", "user-4"),
        ("assistant", "assistant-4"),
    ]


def test_extract_window_falls_back_to_current_user_message():
    session = SimpleNamespace(history=[])

    assert spectator_routes._extract_window(session, 6, "new request") == [
        ("user", "new request")
    ]


@pytest.mark.asyncio
async def test_new_observation_cancels_stale_session_task():
    started = asyncio.Event()
    cancelled = asyncio.Event()
    completed_windows = []

    class Service:
        async def observe_round(self, session_id, window, session_bound, round_count):
            if round_count == 1:
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            completed_windows.append(window)

    service = Service()
    spectator_routes._schedule_observation(service, "session", [("user", "old")], False, 1)
    await started.wait()
    spectator_routes._schedule_observation(service, "session", [("user", "new")], False, 2)

    await asyncio.wait_for(cancelled.wait(), timeout=1)
    await asyncio.sleep(0)

    assert completed_windows == [[("user", "new")]]
