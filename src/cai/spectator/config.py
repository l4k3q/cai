# -*- coding: utf-8 -*-
"""SpectatorConfig: CAI_SPECTATOR_* 环境变量配置（契约 module.md §1）。

非法配置值在构造时抛错（快速失败，不静默取默认）。
"""

from __future__ import annotations

import os
from typing import Optional

from pydantic import BaseModel, Field, field_validator

_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n", ""}


class SpectatorConfig(BaseModel):
    """旁观 agent 运行配置。"""

    enabled: bool = Field(default=False, description="功能总开关，默认关闭")
    interval_rounds: int = Field(default=1, ge=1, description="评估触发的轮次边界 N")
    eval_timeout_seconds: float = Field(
        default=10.0, gt=0, description="单次评估超时上限"
    )
    eval_retry_attempts: int = Field(
        default=3, ge=1, le=5, description="旁观模型瞬时失败时的总尝试次数"
    )
    eval_retry_backoff_seconds: float = Field(
        default=0.5, ge=0.0, le=10.0, description="旁观模型重试的指数退避基数"
    )
    max_candidates: int = Field(default=3, ge=1, le=5, description="卡片候选上限")
    min_intent_confidence: float = Field(
        default=0.6, ge=0.0, le=1.0, description="低于此值按不明确处理(静默)"
    )
    match_threshold: float = Field(
        default=0.15, ge=0.0, le=1.0, description="候选最低评分"
    )
    window_messages: int = Field(default=6, ge=1, description="评估窗口覆盖最近消息数")
    model: Optional[str] = Field(default=None, description="评估用模型; 空=调用方回退")
    recall_test_mode: bool = Field(
        default=False,
        description="显式召回测试模式；允许检索 incomplete 题目，但不允许学习绑定",
    )

    @field_validator("enabled", mode="before")
    @classmethod
    def _coerce_bool(cls, v: object) -> object:
        """环境变量字符串 → bool（宽松解析，未知值按 False，快速失败留给显式构造）。"""
        if isinstance(v, str):
            low = v.strip().lower()
            if low in _TRUE:
                return True
            if low in _FALSE:
                return False
            raise ValueError(f"invalid boolean value: {v!r}")
        return v

    @classmethod
    def from_env(cls, prefix: str = "CAI_SPECTATOR_") -> "SpectatorConfig":
        """从环境变量构造; 未设置的变量取默认值。"""
        env = os.environ

        def _get(name: str) -> object:
            raw = env.get(prefix + name)
            if raw is None or raw == "":
                return None
            return raw

        kwargs: dict[str, object] = {}
        pairs = {
            "ENABLED": ("enabled", str),
            "INTERVAL_ROUNDS": ("interval_rounds", int),
            "EVAL_TIMEOUT_SECONDS": ("eval_timeout_seconds", float),
            "EVAL_RETRY_ATTEMPTS": ("eval_retry_attempts", int),
            "EVAL_RETRY_BACKOFF_SECONDS": ("eval_retry_backoff_seconds", float),
            "MAX_CANDIDATES": ("max_candidates", int),
            "MIN_INTENT_CONFIDENCE": ("min_intent_confidence", float),
            "MATCH_THRESHOLD": ("match_threshold", float),
            "WINDOW_MESSAGES": ("window_messages", int),
            "MODEL": ("model", str),
            "RECALL_TEST_MODE": ("recall_test_mode", str),
        }
        for env_key, (field_name, cast) in pairs.items():
            raw = _get(env_key)
            if raw is None:
                continue
            try:
                kwargs[field_name] = cast(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"invalid value for {prefix}{env_key}: {raw!r}"
                ) from exc
        if "window_messages" not in kwargs:
            raw = _get("WINDOW_ROUNDS")
            if raw is not None:
                try:
                    kwargs["window_messages"] = int(raw) * 2
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"invalid value for {prefix}WINDOW_ROUNDS: {raw!r}"
                    ) from exc
        return cls(**kwargs)  # type: ignore[arg-type]
