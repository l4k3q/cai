"""LiteLLM adapter for OpenAI and Ollama/Qwen model calls.

Wraps ``litellm.acompletion`` with provider-specific parameter filtering,
tool_call_id truncation retry, and Response object construction for streaming.

Extracted from openai_chatcompletions.py [F] to reduce monolith size.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from typing import TYPE_CHECKING, Any, Literal, cast

import litellm
from litellm.exceptions import ServiceUnavailableError
from openai import NOT_GIVEN, NotGiven
from openai.types.responses import Response

from cai.util import get_ollama_api_base
from ..fake_id import FAKE_RESPONSES_ID

if TYPE_CHECKING:
    from openai.types.chat import (
        ChatCompletion,
        ChatCompletionChunk,
        ChatCompletionToolChoiceOptionParam,
    )
    from openai import AsyncStream
    from ...model_settings import ModelSettings


_logger = logging.getLogger("cai.litellm_adapter")

# Global registry of in-flight model calls.
# Each entry: {"start": float, "state": str, "model": str, "kwargs": dict,
#               "last_update": float, "error": str | None}
_active_calls: dict[str, dict] = {}

_NON_REASONING_AGENT_TYPES = {
    "flag_discriminator",
    "injection_detector",
    "orchestration_agent",
    "reporting_agent",
    "selection_agent",
    "thought_agent",
    "use_case_agent",
}


def _is_deepseek_model(model_name: str) -> bool:
    return "deepseek" in model_name.lower()


def _normalize_agent_type(agent_type: str | None, agent_name: str | None) -> str:
    value = agent_type or agent_name or ""
    return value.strip().lower().replace("-", "_").replace(" ", "_")


def resolve_deepseek_thinking_policy(
    *,
    model_name: str,
    model_settings: "ModelSettings",
    agent_name: str | None = None,
    agent_type: str | None = None,
) -> dict[str, str | bool | None]:
    """Return safe policy metadata without exposing model reasoning content."""
    if not _is_deepseek_model(model_name):
        return {
            "mode": "not_applicable",
            "enabled": False,
            "effort": None,
            "source": "provider",
        }

    explicit_effort = getattr(model_settings, "reasoning_effort", None)
    environment_override = os.getenv("CAI_DEEPSEEK_THINKING", "").strip().lower()
    normalized_type = _normalize_agent_type(agent_type, agent_name)

    if explicit_effort:
        enabled = str(explicit_effort).lower() not in {"none", "disabled", "off"}
        reasoning_effort = str(explicit_effort)
        source = "model_settings"
    elif environment_override in {"enabled", "on", "true", "1"}:
        enabled = True
        reasoning_effort = "high"
        source = "environment"
    elif environment_override in {"disabled", "off", "false", "0"}:
        enabled = False
        reasoning_effort = "none"
        source = "environment"
    else:
        enabled = normalized_type not in _NON_REASONING_AGENT_TYPES
        reasoning_effort = "high" if enabled else "none"
        source = "agent_role"

    return {
        "mode": "enabled" if enabled else "disabled",
        "enabled": enabled,
        "effort": reasoning_effort,
        "source": source,
    }


def apply_deepseek_thinking_policy(
    kwargs: dict,
    *,
    model_name: str,
    model_settings: "ModelSettings",
    agent_name: str | None = None,
    agent_type: str | None = None,
) -> dict:
    """Apply CAI's explicit DeepSeek thinking policy to request kwargs.

    Explicit ``model_settings.reasoning_effort`` wins. Otherwise routing and
    utility agents disable thinking, while specialist solving agents enable it.
    ``CAI_DEEPSEEK_THINKING=enabled|disabled`` can override the role policy.
    """
    policy = resolve_deepseek_thinking_policy(
        model_name=model_name,
        model_settings=model_settings,
        agent_name=agent_name,
        agent_type=agent_type,
    )
    if policy["mode"] == "not_applicable":
        return kwargs

    extra_body = dict(kwargs.get("extra_body") or {})
    extra_body["thinking"] = {"type": policy["mode"]}
    kwargs["extra_body"] = extra_body
    kwargs["reasoning_effort"] = policy["effort"]
    return kwargs


def get_active_litellm_calls():
    """Return a snapshot of currently tracked litellm calls."""
    now = time.time()
    return {
        call_id: {
            "start": info["start"],
            "duration": now - info["start"],
            "state": info["state"],
            "model": info["model"],
            "last_update": info["last_update"],
            "error": info["error"],
        }
        for call_id, info in _active_calls.items()
    }


def _register_call(call_id: str, model: str, kwargs: dict) -> None:
    now = time.time()
    _active_calls[call_id] = {
        "start": now,
        "state": "initializing",
        "model": model,
        "kwargs": {k: v for k, v in kwargs.items() if k not in ("messages", "api_key")},
        "last_update": now,
        "error": None,
    }
    _logger.info("[litellm] call %s started (model=%s)", call_id, model)


def _update_call(call_id: str, state: str) -> None:
    info = _active_calls.get(call_id)
    if info is None:
        return
    info["state"] = state
    info["last_update"] = time.time()
    _logger.info("[litellm] call %s state -> %s", call_id, state)


def _finish_call(call_id: str, error: str | None = None) -> None:
    info = _active_calls.get(call_id)
    if info is None:
        return
    info["error"] = error
    info["last_update"] = time.time()
    duration = info["last_update"] - info["start"]
    if error:
        _logger.info("[litellm] call %s finished with error after %.2fs: %s", call_id, duration, error)
    else:
        _logger.info("[litellm] call %s finished successfully after %.2fs", call_id, duration)
    _active_calls.pop(call_id, None)


def _build_response_obj(
    model: str,
    model_settings: "ModelSettings",
    tool_choice: "ChatCompletionToolChoiceOptionParam | NotGiven",
    parallel_tool_calls: bool,
) -> Response:
    """Create a stub Response object used for streaming wrappers."""
    return Response(
        id=FAKE_RESPONSES_ID,
        created_at=time.time(),
        model=model,
        object="response",
        output=[],
        tool_choice="auto"
        if tool_choice is None or tool_choice == NOT_GIVEN
        else cast(Literal["auto", "required", "none"], tool_choice),
        top_p=model_settings.top_p,
        temperature=model_settings.temperature,
        tools=[],
        parallel_tool_calls=parallel_tool_calls or False,
    )


async def fetch_response_litellm_openai(
    *,
    kwargs: dict,
    model_name: str,
    model_settings: "ModelSettings",
    tool_choice: "ChatCompletionToolChoiceOptionParam | NotGiven",
    stream: bool,
    parallel_tool_calls: bool,
    agent_name: str | None = None,
    agent_type: str | None = None,
) -> "ChatCompletion | tuple[Response, AsyncStream[ChatCompletionChunk]]":
    """Handle standard LiteLLM API calls for OpenAI and compatible models.

    If a ContextWindowExceededError occurs due to a tool_call id being
    too long, truncate all tool_call ids in the messages to 40 characters
    and retry once silently.
    """
    # Disable LiteLLM's internal retries.  Use a generous HTTP timeout so normal
    # model calls are not killed, while still preventing a totally unresponsive
    # upstream from holding the connection open forever.
    apply_deepseek_thinking_policy(
        kwargs,
        model_name=str(kwargs.get("model") or model_name),
        model_settings=model_settings,
        agent_name=agent_name,
        agent_type=agent_type,
    )
    kwargs.setdefault("num_retries", 0)
    # Fail fast: if the upstream cannot even start the response in 30s,
    # something is wrong with the provider.  Reading the response body has a
    # separate, longer safety net below.
    kwargs.setdefault("timeout", 30)

    # Upper bound the whole model call (including any synchronous setup LiteLLM
    # performs) so the asyncio event loop never stays blocked indefinitely.
    _LITELLM_TIMEOUT = 60  # seconds

    async def _tracked_acompletion():
        call_id = str(uuid.uuid4())[:8]
        _register_call(call_id, model_name, kwargs)
        _update_call(call_id, "sending_request")
        try:
            result = await asyncio.wait_for(litellm.acompletion(**kwargs), timeout=_LITELLM_TIMEOUT)
            _update_call(call_id, "response_received")
            _finish_call(call_id)
            return result
        except Exception as exc:
            _finish_call(call_id, error=f"{exc.__class__.__name__}: {exc}")
            raise

    try:
        if stream:
            stream_obj = await _tracked_acompletion()
            return _build_response_obj(model_name, model_settings, tool_choice, parallel_tool_calls), stream_obj
        else:
            return await _tracked_acompletion()
    except asyncio.TimeoutError as e:
        raise Exception("模型调用超时（上游响应过慢或无响应），请稍后重试") from e
    except ServiceUnavailableError as e:
        error_msg = str(e)
        if "SERVICE_BUSY" in error_msg or "503" in error_msg:
            raise Exception(
                "上游网关繁忙（503 SERVICE_BUSY），请稍后重试"
            ) from e
        raise
    except Exception as e:
        error_msg = str(e)
        if (
            "string too long" in error_msg
            or "Invalid 'messages" in error_msg
            and "tool_call_id" in error_msg
            and "maximum length" in error_msg
        ):
            # Truncate all tool_call ids to 40 characters and retry once
            messages = kwargs.get("messages", [])
            for msg in messages:
                if (
                    "tool_call_id" in msg
                    and isinstance(msg["tool_call_id"], str)
                    and len(msg["tool_call_id"]) > 40
                ):
                    msg["tool_call_id"] = msg["tool_call_id"][:40]
                if "tool_calls" in msg and isinstance(msg["tool_calls"], list):
                    for tool_call in msg["tool_calls"]:
                        if (
                            isinstance(tool_call, dict)
                            and "id" in tool_call
                            and isinstance(tool_call["id"], str)
                            and len(tool_call["id"]) > 40
                        ):
                            tool_call["id"] = tool_call["id"][:40]
            kwargs["messages"] = messages

            if stream:
                ret = await litellm.acompletion(**kwargs)
                stream_obj = await litellm.acompletion(**kwargs)
                return _build_response_obj(model_name, model_settings, tool_choice, parallel_tool_calls), stream_obj
            else:
                return await litellm.acompletion(**kwargs)
        else:
            raise


async def fetch_response_litellm_ollama(
    *,
    kwargs: dict,
    model_name: str,
    model_settings: "ModelSettings",
    tool_choice: "ChatCompletionToolChoiceOptionParam | NotGiven",
    stream: bool,
    parallel_tool_calls: bool,
) -> "ChatCompletion | tuple[Response, AsyncStream[ChatCompletionChunk]]":
    """Fetch a response from an Ollama or Qwen model using LiteLLM.

    Ensures that the 'format' parameter is not set to a JSON string, which
    can cause issues with the Ollama API, and filters to only supported params.
    """
    # Extract only supported parameters for Ollama
    ollama_supported_params = {
        "model": kwargs.get("model", ""),
        "messages": kwargs.get("messages", []),
        "stream": kwargs.get("stream", False),
    }

    for param in ["temperature", "top_p", "max_tokens"]:
        if param in kwargs and kwargs[param] is not NOT_GIVEN:
            ollama_supported_params[param] = kwargs[param]

    if "extra_headers" in kwargs:
        ollama_supported_params["extra_headers"] = kwargs["extra_headers"]

    if "tools" in kwargs and kwargs.get("tools") and kwargs.get("tools") is not NOT_GIVEN:
        ollama_supported_params["tools"] = kwargs.get("tools")

    ollama_kwargs = {
        k: v
        for k, v in ollama_supported_params.items()
        if v is not None and k not in ["response_format", "store"]
    }

    api_base = get_ollama_api_base()

    if stream:
        response = _build_response_obj(model_name, model_settings, tool_choice, parallel_tool_calls)
        stream_obj = await litellm.acompletion(
            **ollama_kwargs, api_base=api_base, custom_llm_provider="openai"
        )
        return response, stream_obj
    else:
        return await litellm.acompletion(
            **ollama_kwargs,
            api_base=api_base,
            custom_llm_provider="openai",
        )
