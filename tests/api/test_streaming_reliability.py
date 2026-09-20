import asyncio
import json
import types

import pytest
from fastapi.testclient import TestClient

from cai.api.app import create_cai_api_app
from cai.api.sessions import SessionManager, SessionState
from cai.api.streaming import sse_stream_via_hooks
from cai.sdk.agents.result import RunResult


def _agent_factory(agent_name: str, model_name: str, agent_id: str):
    return types.SimpleNamespace(
        name=f"{agent_name}-{agent_id}",
        model=types.SimpleNamespace(model=model_name),
        handoffs=[],
    )


def _decode_event(chunk: bytes) -> tuple[str | None, dict | None]:
    text = chunk.decode("utf-8")
    if text.startswith(":"):
        return None, None
    lines = text.splitlines()
    event = next(line[6:].strip() for line in lines if line.startswith("event:"))
    data = next(line[5:].strip() for line in lines if line.startswith("data:"))
    return event, json.loads(data)


@pytest.mark.asyncio
async def test_hook_stream_runs_agent_once_and_persists_that_result(monkeypatch):
    calls = 0

    async def fake_run(starting_agent, input_items, **kwargs):
        nonlocal calls
        calls += 1
        return RunResult(
            input=input_items,
            new_items=[],
            raw_responses=[],
            final_output="done",
            input_guardrail_results=[],
            output_guardrail_results=[],
            _last_agent=starting_agent,
        )

    monkeypatch.setattr("cai.api.streaming.Runner.run", fake_run)
    session = SessionState(
        agent_name="test",
        model_name="model",
        stateful=True,
        metadata=None,
        agent_factory=_agent_factory,
    )

    chunks = [
        chunk
        async for chunk in sse_stream_via_hooks(
            session.agent, [{"role": "user", "content": "hello"}], session=session
        )
    ]

    assert calls == 1
    assert session.history[-1]["content"] == "hello"
    events = [_decode_event(chunk) for chunk in chunks]
    assert events[-1][0] == "final"
    model_states = [data for event, data in events if event == "model_state"]
    assert model_states[0]["state"] == "preparing"
    assert model_states[-1]["state"] == "completed"
    assert model_states[0]["thinking"]["mode"] == "not_applicable"
    assert session._current_task is None


@pytest.mark.asyncio
async def test_hook_stream_reports_safe_deepseek_thinking_policy(monkeypatch):
    async def fake_run(starting_agent, input_items, **kwargs):
        return RunResult(
            input=input_items,
            new_items=[],
            raw_responses=[],
            final_output="done",
            input_guardrail_results=[],
            output_guardrail_results=[],
            _last_agent=starting_agent,
        )

    monkeypatch.setattr("cai.api.streaming.Runner.run", fake_run)
    agent = types.SimpleNamespace(
        name="Orchestration Agent",
        model=types.SimpleNamespace(
            model="deepseek/deepseek-chat",
            agent_type="orchestration_agent",
        ),
        model_settings=types.SimpleNamespace(reasoning_effort=None),
    )

    chunks = [chunk async for chunk in sse_stream_via_hooks(agent, [])]
    states = [
        data for event, data in map(_decode_event, chunks) if event == "model_state"
    ]

    assert states[0]["thinking"] == {
        "mode": "disabled",
        "effort": "none",
        "source": "agent_role",
    }
    assert all("reasoning_content" not in state for state in states)


@pytest.mark.asyncio
async def test_hook_stream_reports_thinking_while_specialist_waits(monkeypatch):
    async def delayed_run(starting_agent, input_items, **kwargs):
        await asyncio.sleep(0.12)
        return RunResult(
            input=input_items,
            new_items=[],
            raw_responses=[],
            final_output="done",
            input_guardrail_results=[],
            output_guardrail_results=[],
            _last_agent=starting_agent,
        )

    monkeypatch.setattr("cai.api.streaming.Runner.run", delayed_run)
    monkeypatch.setenv("CAI_API_HEARTBEAT_SECONDS", "0.1")
    monkeypatch.setenv("CAI_API_IDLE_TIMEOUT", "1")
    agent = types.SimpleNamespace(
        name="Web Pentester",
        model=types.SimpleNamespace(
            model="deepseek/deepseek-chat",
            agent_type="web_pentester_agent",
        ),
        model_settings=types.SimpleNamespace(reasoning_effort=None),
    )

    chunks = [chunk async for chunk in sse_stream_via_hooks(agent, [])]
    states = [
        data for event, data in map(_decode_event, chunks) if event == "model_state"
    ]

    state_names = [state["state"] for state in states]
    assert state_names[0] == "preparing"
    assert state_names[-1] == "completed"
    assert "thinking" in state_names[1:-1]
    assert all(
        state["thinking"]["mode"] == "enabled"
        for state in states
        if state["state"] == "thinking"
    )


def test_stream_endpoint_does_not_run_a_hidden_second_inference(monkeypatch):
    calls = 0

    async def fake_run(starting_agent, input_items, **kwargs):
        nonlocal calls
        calls += 1
        return RunResult(
            input=input_items,
            new_items=[],
            raw_responses=[],
            final_output="done",
            input_guardrail_results=[],
            output_guardrail_results=[],
            _last_agent=starting_agent,
        )

    monkeypatch.setattr("cai.api.streaming.Runner.run", fake_run)
    monkeypatch.setenv("ALIAS_API_KEY", "test-key")
    manager = SessionManager(agent_factory=_agent_factory)
    app = create_cai_api_app(session_manager=manager)

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/sessions",
            headers={"X-CAI-API-Key": "test-key"},
            json={"agent": "test", "model": "model"},
        )
        session_id = created.json()["id"]
        with client.stream(
            "POST",
            f"/api/v1/sessions/{session_id}/messages/stream",
            headers={"X-CAI-API-Key": "test-key"},
            json={"input": "hello"},
        ) as response:
            body = b"".join(response.iter_bytes())

    assert response.status_code == 200
    assert b"event: final" in body
    assert calls == 1


@pytest.mark.asyncio
async def test_hook_stream_times_out_a_silent_run(monkeypatch):
    cancelled = asyncio.Event()

    async def silent_run(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr("cai.api.streaming.Runner.run", silent_run)
    monkeypatch.setenv("CAI_API_HEARTBEAT_SECONDS", "0.1")
    monkeypatch.setenv("CAI_API_IDLE_TIMEOUT", "0.2")

    chunks = [
        chunk
        async for chunk in sse_stream_via_hooks(
            types.SimpleNamespace(name="agent"), [], session=None
        )
    ]
    events = [_decode_event(chunk) for chunk in chunks]

    assert any(event == "error" and "无响应" in data["message"] for event, data in events if event)
    await asyncio.wait_for(cancelled.wait(), timeout=1)


@pytest.mark.asyncio
async def test_interrupt_wait_has_a_hard_deadline():
    release = asyncio.Event()

    async def cancellation_resistant():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()

    session = SessionState(
        agent_name="test",
        model_name="model",
        stateful=True,
        metadata=None,
        agent_factory=_agent_factory,
    )
    task = asyncio.create_task(cancellation_resistant())
    session.set_running_task(task)
    await asyncio.sleep(0)  # let the task enter its cancellation handler

    assert await session.interrupt_and_wait(timeout=0.05) is False
    assert not task.done()

    release.set()
    await asyncio.wait_for(task, timeout=1)
    session.clear_running_task(task)
