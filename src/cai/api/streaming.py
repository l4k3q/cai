"""Streaming helpers for CAI API.

Implements Server-Sent Events (SSE) for high-level reasoning steps:
- No token-level streaming.
- Emits events for tools, tool outputs, messages, handoffs, and agent switches.
- Sends a final summary event with accumulated reasoning steps and final output.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any, Dict, List

from cai.sdk.agents.items import (
    HandoffOutputItem,
    ItemHelpers,
    MessageOutputItem,
    ToolCallItem,
    ToolCallOutputItem,
)
from cai.sdk.agents.lifecycle import RunHooks
from cai.sdk.agents.result import RunResult, RunResultStreaming
from cai.sdk.agents.run import DEFAULT_MAX_TURNS, Runner
from cai.sdk.agents.models.chatcompletions.litellm_adapter import (
    resolve_deepseek_thinking_policy,
)
from cai.sdk.agents.stream_events import (
    AgentUpdatedStreamEvent,
    RawResponsesStreamEvent,
    RunItemStreamEvent,
)


def _sse(event: str, data: Dict[str, Any]) -> bytes:
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")


def _sse_heartbeat() -> bytes:
    """Keep proxies and browsers from treating a quiet agent run as a dead stream."""
    return b": heartbeat\n\n"


def _safe_model_state(starting_agent: Any, state: str, label: str, **extra: Any) -> Dict[str, Any]:
    """Build public execution metadata without exposing hidden reasoning text."""
    model = getattr(starting_agent, "model", None)
    model_name = str(getattr(model, "model", model) or "")
    agent_name = getattr(starting_agent, "name", None)
    agent_type = getattr(model, "agent_type", None)
    model_settings = getattr(starting_agent, "model_settings", None)
    if model_settings is None:
        model_settings = type("ModelSettingsView", (), {"reasoning_effort": None})()
    thinking = resolve_deepseek_thinking_policy(
        model_name=model_name,
        model_settings=model_settings,
        agent_name=agent_name,
        agent_type=agent_type,
    )
    payload = {
        "state": state,
        "label": label,
        "agent": agent_name,
        "model": model_name,
        "thinking": {
            "mode": thinking["mode"],
            "effort": thinking["effort"],
            "source": thinking["source"],
        },
    }
    payload.update(extra)
    return payload


def _step_from_run_item_event(evt: RunItemStreamEvent) -> Dict[str, Any] | None:
    name = evt.name
    item = evt.item
    # Message produced by the assistant (full message, not token deltas)
    if isinstance(item, MessageOutputItem):
        text = ItemHelpers.text_message_output(item)
        return {
            "type": "message",
            "agent": getattr(item.agent, "name", None),
            "text": text,
        }
    # Tool call request
    if isinstance(item, ToolCallItem):
        raw = item.raw_item
        tool_name = getattr(raw, "name", None) or getattr(raw, "type", None)
        args = getattr(raw, "arguments", None)
        return {
            "type": "tool_call",
            "agent": getattr(item.agent, "name", None),
            "tool": tool_name,
            "arguments": args,
        }
    # Tool call output
    if isinstance(item, ToolCallOutputItem):
        return {
            "type": "tool_output",
            "agent": getattr(item.agent, "name", None),
            "output": item.output,
        }
    # Handoff
    if isinstance(item, HandoffOutputItem):
        return {
            "type": "handoff",
            "from_agent": getattr(item.source_agent, "name", None),
            "to_agent": getattr(item.target_agent, "name", None),
        }
    # Reasoning or other
    return {
        "type": name,
        "agent": getattr(item.agent, "name", None),
    }


async def sse_stream_for_run(result: RunResultStreaming) -> AsyncIterator[bytes]:
    """Yield SSE events as the run progresses and a final summary at completion."""
    steps: List[Dict[str, Any]] = []
    last_message: str | None = None

    # Avoid contextvar reset issues when Starlette closes the stream in a different task context
    # (RunResultStreaming.stream_events() will try to finish the trace). We disable trace finishing
    # here by clearing the internal trace reference. Tracing for API streaming can be handled
    # at a higher level if needed.
    try:
        result._trace = None  # type: ignore[attr-defined]
    except Exception:
        pass

    try:
        async for evt in result.stream_events():
            # Skip raw token-level events entirely
            if isinstance(evt, RawResponsesStreamEvent):
                continue

            if isinstance(evt, RunItemStreamEvent):
                step = _step_from_run_item_event(evt)
                if step:
                    steps.append(step)
                    if step.get("type") == "message":
                        last_message = step.get("text")
                    yield _sse("reasoning_step", step)
                continue

            if isinstance(evt, AgentUpdatedStreamEvent):
                step = {"type": "agent_switched", "agent": getattr(evt.new_agent, "name", None)}
                steps.append(step)
                yield _sse("reasoning_step", step)
                continue
    except Exception as e:  # Ensure the SSE never explodes the response
        yield _sse("error", {"message": str(e)})

    # When complete, emit a final summary event
    # Derive a text_output from all message output items observed (no tokens)
    text_output = last_message
    final_output = result.final_output
    if hasattr(final_output, "model_dump"):
        final_output = final_output.model_dump(exclude_unset=True)
    summary = {
        "steps": steps,
        "final_message": text_output,
        "final_output": final_output,
    }
    yield _sse("final", summary)


class _SSEHooks(RunHooks[Any]):
    def __init__(self, q: asyncio.Queue):
        self.q = q

    async def on_agent_start(self, context, agent):  # type: ignore[override]
        await self.q.put({"type": "agent_switched", "agent": getattr(agent, "name", None)})

    async def on_handoff(self, context, from_agent, to_agent):  # type: ignore[override]
        await self.q.put({
            "type": "handoff",
            "from_agent": getattr(from_agent, "name", None),
            "to_agent": getattr(to_agent, "name", None),
        })

    async def on_tool_start(self, context, agent, tool):  # type: ignore[override]
        await self.q.put({
            "type": "tool_call",
            "agent": getattr(agent, "name", None),
            "tool": getattr(tool, "name", None),
            "arguments": getattr(tool, "_last_args", None),
        })

    async def on_tool_end(self, context, agent, tool, result):  # type: ignore[override]
        await self.q.put({
            "type": "tool_output",
            "agent": getattr(agent, "name", None),
            "output": result,
        })


async def sse_stream_via_hooks(
    starting_agent,
    input_items,
    *,
    context=None,
    max_turns: int | float | None = None,
    session: Any | None = None,
) -> AsyncIterator[bytes]:
    """SSE stream built on top of non-streaming model runs, using RunHooks.

    - No token streaming; emits high-level steps only.
    - Yields a final event with the last assistant message and final_output.
    """
    queue: asyncio.Queue = asyncio.Queue()
    hooks = _SSEHooks(queue)
    steps: list[dict] = []
    last_message: str | None = None

    async def _run_agent():
        return await Runner.run(
            starting_agent,
            input_items,
            context=context,
            max_turns=int(max_turns) if isinstance(max_turns, (int, float)) else DEFAULT_MAX_TURNS,
            hooks=hooks,
        )

    heartbeat_seconds = max(float(os.getenv("CAI_API_HEARTBEAT_SECONDS", "5")), 0.1)
    # A model call may legitimately spend several minutes reasoning over a
    # large obfuscated/crypto prompt without emitting a hook event.  The old
    # 180-second default cancelled those valid runs. Keep a finite safety net,
    # but make it long enough for complex CTF tasks and report progress below.
    idle_timeout_seconds = max(float(os.getenv("CAI_API_IDLE_TIMEOUT", "600")), heartbeat_seconds)
    loop = asyncio.get_running_loop()
    started_at = loop.time()
    last_activity = loop.time()

    task = asyncio.create_task(_run_agent())
    if session is not None:
        try:
            if not session.try_set_running_task(task):
                task.cancel()
                yield _sse("error", {"message": "该会话已有任务在运行，请先停止或等待完成"})
                return
        except Exception:
            task.cancel()
            raise

    try:
        yield _sse(
            "model_state",
            _safe_model_state(starting_agent, "preparing", "正在准备模型请求", elapsed_seconds=0),
        )
        yield _sse(
            "status",
            {"type": "started", "message": "Agent 已启动，正在准备模型请求"},
        )
        while True:
            if task.done() and queue.empty():
                break

            idle_remaining = idle_timeout_seconds - (loop.time() - last_activity)
            if idle_remaining <= 0:
                task.cancel()
                yield _sse(
                    "model_state",
                    _safe_model_state(
                        starting_agent,
                        "failed",
                        "模型长时间无响应",
                        elapsed_seconds=int(loop.time() - started_at),
                    ),
                )
                yield _sse(
                    "error",
                    {"message": f"Agent 超过 {idle_timeout_seconds:g} 秒无响应，任务已取消"},
                )
                return

            try:
                item = await asyncio.wait_for(
                    queue.get(), timeout=min(heartbeat_seconds, idle_remaining)
                )
            except asyncio.TimeoutError:
                elapsed = int(loop.time() - started_at)
                policy = _safe_model_state(starting_agent, "thinking", "模型正在思考")
                if policy["thinking"]["mode"] != "enabled":
                    policy["state"] = "generating"
                    policy["label"] = "模型正在生成回答"
                policy["elapsed_seconds"] = elapsed
                yield _sse("model_state", policy)
                yield _sse(
                    "status",
                    {
                        "type": "waiting",
                        "message": f"模型仍在处理中（已等待约 {elapsed} 秒）",
                        "elapsed_seconds": elapsed,
                    },
                )
                yield _sse_heartbeat()
                continue

            last_activity = loop.time()
            steps.append(item)
            if item.get("type") == "tool_call":
                yield _sse(
                    "model_state",
                    _safe_model_state(
                        starting_agent,
                        "tool_running",
                        "Agent 正在调用工具",
                        elapsed_seconds=int(loop.time() - started_at),
                        agent=item.get("agent") or getattr(starting_agent, "name", None),
                    ),
                )
            elif item.get("type") == "tool_output":
                resumed = _safe_model_state(starting_agent, "thinking", "工具完成，模型继续思考")
                if resumed["thinking"]["mode"] != "enabled":
                    resumed["state"] = "generating"
                    resumed["label"] = "工具完成，模型继续生成"
                resumed["elapsed_seconds"] = int(loop.time() - started_at)
                yield _sse("model_state", resumed)
            yield _sse("reasoning_step", item)

        if task.cancelled():
            yield _sse(
                "model_state",
                _safe_model_state(starting_agent, "cancelled", "任务已取消"),
            )
            yield _sse("error", {"message": "任务已取消"})
            return
        exc = task.exception()
        if exc is not None:
            yield _sse(
                "model_state",
                _safe_model_state(starting_agent, "failed", "模型调用失败"),
            )
            yield _sse("error", {"message": f"{exc.__class__.__name__}: {exc}"})
            return

        result: RunResult = task.result()

        # Persist the result from this exact run.  The API route previously ran
        # Runner.run() a second time after yielding `final`, doubling provider
        # calls and keeping the browser waiting when that hidden run stalled.
        if session is not None:
            try:
                session.history = result.to_input_list()
                session.updated_at = datetime.now(timezone.utc)
            except Exception:
                pass

        # Extract last assistant text message
        for it in result.new_items:
            if isinstance(it, MessageOutputItem):
                last = ItemHelpers.text_message_output(it)
                if last:
                    last_message = last
        final_output = result.final_output
        if hasattr(final_output, "model_dump"):
            final_output = final_output.model_dump(exclude_unset=True)
        # Emit a final message step so simple chats (no tools) still produce reasoning steps
        if last_message:
            msg_step = {
                "type": "message",
                "agent": getattr(result.last_agent, "name", None),
                "text": last_message,
            }
            steps.append(msg_step)
            yield _sse("reasoning_step", msg_step)
        # Persist steps into session for later UX summaries
        if session is not None:
            try:
                session.last_steps = steps
            except Exception:
                pass
        yield _sse(
            "model_state",
            _safe_model_state(
                starting_agent,
                "completed",
                "回答已完成",
                elapsed_seconds=int(loop.time() - started_at),
                agent=getattr(result.last_agent, "name", None),
            ),
        )
        yield _sse(
            "final",
            {
                "steps": steps,
                "final_message": last_message,
                "final_output": final_output,
            },
        )
    finally:
        if not task.done():
            task.cancel()
        if session is not None:
            try:
                session.clear_running_task(task)
            except Exception:
                pass


def _token_event_from_raw(raw_evt: Any) -> Dict[str, Any] | None:
    """Translate a raw Responses stream event to a token-level SSE dict.

    We focus on message boundaries and text deltas. Event objects have a 'type' string.
    """
    etype = getattr(raw_evt, "type", None)
    if not etype:
        return None
    # Message boundaries
    if etype == "response.output_item.added":
        return {"type": "message_start"}
    if etype == "response.output_item.done":
        return {"type": "message_end"}
    # Text deltas
    if etype == "response.output_text.delta":
        text = getattr(raw_evt, "delta", None)
        if text:
            return {"type": "token_delta", "text": text}
    return None


async def sse_stream_tokens_for_run(result: RunResultStreaming, session: Any | None = None) -> AsyncIterator[bytes]:
    """Yield SSE with token-level events plus high-level steps.

    - Emits token_delta/message_start/message_end from raw model events.
    - Also emits reasoning_step events for tools/handoffs/messages/agent switches.
    - Finishes with a final event including last_message and final_output.
    """
    steps: List[Dict[str, Any]] = []
    last_message: str | None = None

    # Avoid contextvar reset mismatch on stream completion
    try:
        result._trace = None  # type: ignore[attr-defined]
    except Exception:
        pass

    token_buffer: List[str] = []
    current_agent_name: str | None = None

    try:
        async for evt in result.stream_events():
            if isinstance(evt, RawResponsesStreamEvent):
                tok = _token_event_from_raw(evt.data)
                if tok:
                    if tok["type"] == "message_start":
                        token_buffer = []
                    elif tok["type"] == "token_delta":
                        text = tok.get("text")
                        if isinstance(text, str):
                            token_buffer.append(text)
                    elif tok["type"] == "message_end":
                        text = "".join(token_buffer).strip()
                        if text:
                            step = {"type": "message", "agent": current_agent_name, "text": text}
                            steps.append(step)
                            last_message = text
                            yield _sse("reasoning_step", step)
                    yield _sse("token", tok)
                continue

            if isinstance(evt, RunItemStreamEvent):
                step = _step_from_run_item_event(evt)
                if step:
                    steps.append(step)
                    if step.get("type") == "message":
                        last_message = step.get("text")
                    yield _sse("reasoning_step", step)
                continue

            if isinstance(evt, AgentUpdatedStreamEvent):
                current_agent_name = getattr(evt.new_agent, "name", None)
                step = {"type": "agent_switched", "agent": current_agent_name}
                steps.append(step)
                yield _sse("reasoning_step", step)
                continue
    except Exception as e:
        yield _sse("error", {"message": str(e)})

    final_output = result.final_output
    if hasattr(final_output, "model_dump"):
        final_output = final_output.model_dump(exclude_unset=True)
    # Persist steps into session for UX summaries
    if session is not None:
        try:
            session.last_steps = steps
        except Exception:
            pass
    yield _sse("final", {"steps": steps, "final_message": last_message, "final_output": final_output})
