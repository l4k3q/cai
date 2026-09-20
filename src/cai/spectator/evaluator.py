# -*- coding: utf-8 -*-
"""意图评估: IntentEvaluator Protocol + LLM 实现方（契约 module.md §4）。

LLM 实现方通过可注入的 completer 函数发起调用（真实环境注入 litellm 版本,
测试注入 fake）; prompt 要求 JSON 输出并经 Pydantic 严格校验; 任何失败统一为
EvaluationError（不重试, 宪法 II）。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from typing import Awaitable, Callable, List, Protocol, Tuple, runtime_checkable

from pydantic import BaseModel, Field, ValidationError

from .models import IntentAssessment

try:
    from cai.model_utils import normalize_model_id
except ModuleNotFoundError:
    from model_utils import normalize_model_id

ConversationTurn = Tuple[str, str]
"""(role, content); role ∈ {"user", "assistant"}。"""

Completer = Callable[..., Awaitable[str]]
"""异步补全函数: completer(messages, **kwargs) → 文本。

真实实现方由集成层注入（lazy import litellm, 复用会话 provider）;
本模块自身不依赖 litellm（宪法 III: Windows 本地可测）。
"""

_SYSTEM_PROMPT = (
    "You are a conversation intent classifier for a cybersecurity learning "
    "platform. Classify the user's dominant intent in the recent conversation "
    "window and extract the technical topic. Respond with ONLY a JSON object, "
    "no extra text, in exactly this schema: "
    '{"intent": "knowledge_seeking|task_delegation|chitchat|unclear", '
    '"confidence": <float 0..1>, '
    '"topic_summary": "<short summary, same language as conversation>", '
    '"keywords": ["<topic keyword>", ...]}\n'
    "Rules:\n"
    "- knowledge_seeking: user is asking to understand a technical concept, "
    "mechanism, or know-how\n"
    "- task_delegation: user is ordering execution of a concrete task "
    "(run/exploit/solve something)\n"
    "- chitchat: greetings or non-technical talk\n"
    "- unclear: mixed, vague, or too little substance\n"
    "Keywords must be technical terms usable for library search "
    "(both Chinese and English are fine)."
)

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


class EvaluationError(Exception):
    """意图评估失败（超时/解析失败/校验失败）——由 service 捕获并降级。"""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.attempts = 1


@runtime_checkable
class IntentEvaluator(Protocol):
    """意图评估器协议。"""

    async def assess(self, transcript: List[ConversationTurn]) -> IntentAssessment: ...


class LLMIntentEvaluator(BaseModel):
    """基于 LLM 的意图评估实现（completer 注入）。"""

    model_config = {"arbitrary_types_allowed": True}

    completer: Callable[..., Awaitable[str]]
    timeout_seconds: float = Field(default=10.0, gt=0)

    async def assess(self, transcript: List[ConversationTurn]) -> IntentAssessment:
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            *[{"role": role, "content": content} for role, content in transcript],
            {
                "role": "user",
                "content": "Classify the intent of the conversation above. "
                "Reply with the JSON object only.",
            },
        ]
        try:
            call = self.completer(messages)
            if inspect.isawaitable(call):
                call = await asyncio.wait_for(call, timeout=self.timeout_seconds)
            raw = call
        except asyncio.TimeoutError as exc:
            raise EvaluationError(
                "intent evaluation timed out", retryable=True
            ) from exc
        except Exception as exc:  # completer/网关异常统一包装
            raise EvaluationError(
                f"intent evaluation failed: {exc}",
                retryable=_is_retryable_gateway_error(exc),
            ) from exc

        payload = _extract_json(raw)
        try:
            return IntentAssessment.model_validate(payload)
        except ValidationError as exc:
            raise EvaluationError(f"assessment schema invalid: {exc}") from exc


def _is_retryable_gateway_error(exc: Exception) -> bool:
    """识别连接、限流和服务端故障；认证、参数和响应格式错误不重试。"""
    if isinstance(exc, (ConnectionError, OSError, asyncio.TimeoutError)):
        return True
    status_code = getattr(exc, "status_code", None)
    if status_code == 429 or (isinstance(status_code, int) and status_code >= 500):
        return True
    text = f"{type(exc).__name__}: {exc}".lower()
    markers = (
        "cannot connect",
        "connection error",
        "connection reset",
        "timed out",
        "timeout",
        "rate limit",
        "internalservererror",
        "serviceunavailable",
    )
    return any(marker in text for marker in markers)


def _extract_json(raw: str) -> dict:
    """从模型回复中提取 JSON 对象（容忍代码围栏/前后杂文）。"""
    text = (raw or "").strip()
    if not text:
        raise EvaluationError("empty evaluation response")
    fenced = _FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    # 定位首个 { 与最后一个 }
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise EvaluationError("no JSON object found in evaluation response")
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise EvaluationError(f"evaluation response is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise EvaluationError("evaluation JSON is not an object")
    return payload


def make_litellm_completer(model: str, api_base=None, api_key=None) -> Completer:
    """构造真实 completer（lazy import litellm; 仅集成层调用）。"""

    normalized_model = normalize_model_id(model)

    async def _completer(messages, **kwargs):
        import litellm  # noqa: PLC0415 —— 宪法 III: 仅此函数内 lazy import

        call_kwargs = {"model": normalized_model, "messages": messages, "timeout": 30}
        if api_base:
            call_kwargs["api_base"] = api_base
        if api_key:
            call_kwargs["api_key"] = api_key
        if isinstance(normalized_model, str) and normalized_model.lower().startswith(
            "deepseek/"
        ):
            call_kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
            call_kwargs["reasoning_effort"] = "none"
        call_kwargs.update(kwargs)
        resp = await litellm.acompletion(**call_kwargs)
        return resp.choices[0].message.content or ""

    return _completer
