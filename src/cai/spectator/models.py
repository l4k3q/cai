# -*- coding: utf-8 -*-
"""领域模型: IntentAssessment / RecommendationCandidate / SpectatorCard /
CooldownState / SpectatorEvaluationEvent（data-model.md §1-§2）。

全部为 API 进程内存对象; 校验规则由 Pydantic 强制。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

_INTERVAL_ROUNDS = 3  # 与 SpectatorConfig.interval_rounds 默认一致（构造守卫用）


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UserAction(str, Enum):
    """用户对卡片/候选的显式动作。"""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    IGNORED = "ignored"


class CardStatus(str, Enum):
    """卡片状态机（data-model.md §3.1）: active 为唯一非终态。"""

    ACTIVE = "active"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    IGNORED = "ignored"
    SUPERSEDED = "superseded"


class IntentAssessment(BaseModel):
    """一次意图评估的产出（evaluator 接口出参）。"""

    intent: str = Field(
        description="knowledge_seeking|task_delegation|chitchat|unclear"
    )
    confidence: float = Field(ge=0.0, le=1.0)
    topic_summary: str = ""
    keywords: List[str] = Field(default_factory=list)

    @field_validator("intent")
    @classmethod
    def _intent_in_enum(cls, v: str) -> str:
        allowed = {"knowledge_seeking", "task_delegation", "chitchat", "unclear"}
        if v not in allowed:
            raise ValueError(f"intent must be one of {sorted(allowed)}, got {v!r}")
        return v

    @field_validator("keywords")
    @classmethod
    def _clean_keywords(cls, v: List[str]) -> List[str]:
        cleaned: List[str] = []
        for kw in v:
            item = kw.strip()
            if not item:
                continue
            if len(item) > 40:
                raise ValueError(f"keyword too long (>40 chars): {item[:40]}…")
            if item not in cleaned:
                cleaned.append(item)
        return cleaned[:12]


class QuestionDoc(BaseModel):
    """检索方返回的轻量题目投影（无 ORM 依赖）。"""

    question_id: str
    statement: str
    principles_text: str = ""
    recall_text: str = ""
    learnable: bool = True
    disabled: bool = False
    disabled_reason: str = ""

    @model_validator(mode="after")
    def _availability_flags(self) -> "QuestionDoc":
        if self.disabled or not self.learnable:
            self.disabled = True
            self.learnable = False
        return self


class RecommendationCandidate(BaseModel):
    """卡片内一道候选题。"""

    question_id: str
    title_preview: str = Field(max_length=121)  # 120 字符内容 + 省略号（data-model §2）
    score: float = Field(ge=0.0, le=1.0)
    reason: str = ""
    learnable: bool = True
    disabled: bool = False
    disabled_reason: str = ""

    @model_validator(mode="after")
    def _availability_flags(self) -> "RecommendationCandidate":
        if self.disabled or not self.learnable:
            self.disabled = True
            self.learnable = False
        return self

    @staticmethod
    def make_preview(statement: str, limit: int = 120) -> str:
        """题面摘要: 前 limit 字符 + 省略号（服务端截断）。"""
        text = statement.strip()
        if len(text) <= limit:
            return text
        return text[:limit] + "…"


class SpectatorCard(BaseModel):
    """一次推荐呈现（每会话至多一张活跃卡片）。"""

    card_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    kind: Literal["recommendation", "failure"] = "recommendation"
    created_at_round: int = Field(ge=0)
    topic_summary: str = ""
    keywords: List[str] = Field(default_factory=list)
    candidates: List[RecommendationCandidate] = Field(default_factory=list)
    error_message: str = ""
    retryable: bool = False
    status: CardStatus = CardStatus.ACTIVE
    user_action: Optional[UserAction] = None
    user_action_at: Optional[datetime] = None

    @field_validator("candidates")
    @classmethod
    def _candidates_rules(
        cls, v: List[RecommendationCandidate]
    ) -> List[RecommendationCandidate]:
        if len(v) > 5:
            raise ValueError(f"too many candidates ({len(v)} > 5)")
        scores = [c.score for c in v]
        if scores != sorted(scores, reverse=True):
            raise ValueError("candidates must be sorted by score descending")
        return v

    @model_validator(mode="after")
    def _round_boundary(self) -> "SpectatorCard":
        # FR-002: 卡片只能在 N 的整数倍轮边界产生（默认 N=3; 非默认 N 由
        # service 层在构造前保证, 此处校验默认边界的倍数性）
        if self.created_at_round % _INTERVAL_ROUNDS != 0:
            raise ValueError(
                f"created_at_round must be a multiple of "
                f"{_INTERVAL_ROUNDS}, got {self.created_at_round}"
            )
        return self

    def mark(self, status: CardStatus, action: Optional[UserAction]) -> None:
        """状态转移（仅允许 active → 终态; 由 CardLifecycle 调用）。"""
        if self.status != CardStatus.ACTIVE:
            raise ValueError(f"card {self.card_id} is {self.status}, not active")
        self.status = status
        self.user_action = action
        self.user_action_at = _utcnow()

    def candidate_ids(self) -> List[str]:
        return [c.question_id for c in self.candidates]


class CooldownState(BaseModel):
    """防重复推荐状态（会话级）。"""

    question_id: str
    action: UserAction
    created_at: datetime = Field(default_factory=_utcnow)


class SpectatorEvaluationEvent(BaseModel):
    """JSONL 日志行模型（data-model.md §5）。"""

    event: str = Field(description="evaluation|recommendation|user_action|degraded")
    session_id: str
    round: int = Field(ge=0)
    timestamp: datetime = Field(default_factory=_utcnow)
    payload: dict = Field(default_factory=dict)

    @field_validator("event")
    @classmethod
    def _event_in_enum(cls, v: str) -> str:
        allowed = {"evaluation", "recommendation", "user_action", "degraded"}
        if v not in allowed:
            raise ValueError(f"event must be one of {sorted(allowed)}, got {v!r}")
        return v
