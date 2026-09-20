# -*- coding: utf-8 -*-
"""SpectatorService 编排: observe_round / accept / decline（契约 module.md §6）。

不变式:
- observe_round 永不抛出（内部全捕获降级, 宪法 II/VI）
- accept/decline 抛 CardStateError（API 层转 409）
- 教学绑定不在本模块: accept 仅返回绑定事实, 由 API 层写 SessionState
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from pydantic import BaseModel

from .config import SpectatorConfig
from .core import CardLifecycle, CooldownPolicy, RoundTrigger
from .evaluator import EvaluationError, IntentEvaluator
from .logging_utils import SpectatorLogger
from .matcher import QuestionSearcher, score_candidates
from .models import (
    CardStatus,
    CooldownState,
    IntentAssessment,
    RecommendationCandidate,
    SpectatorCard,
    UserAction,
)

logger = logging.getLogger("cai.spectator")

ConversationTurn = Tuple[str, str]


class CardStateError(Exception):
    """卡片状态非法（非 active / 未知卡片 / 候选不符）——API 层转 409。"""


@dataclass
class AcceptResult:
    """accept 产物: 由 API 层消费并写入 SessionState 绑定字段。"""

    question_id: str
    card_id: str
    idempotent: bool = False


@dataclass(frozen=True)
class ProviderHint:
    """会话级 provider 透传（research.md D4: 优先会话 override, 回退 .env）。

    由 API 钩子从 SessionState.provider_base/provider_key/model_name 提取;
    三字段全空视为无效 hint（回退服务默认评估器）。
    """

    api_base: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None

    def is_effective(self) -> bool:
        return bool(self.api_base or self.api_key or self.model)


@dataclass(frozen=True)
class _RetryContext:
    window: List[ConversationTurn]
    session_bound: bool
    round_no: int
    provider_hint: Optional[ProviderHint]


# 评估器构造委托: service 层不 import litellm（宪法 III）, 由集成层注入
EvaluatorFactory = Callable[[ProviderHint], "IntentEvaluator"]


@dataclass
class _SessionRuntime:
    """单会话旁观运行时状态（进程内存, 会话结束即弃）。"""

    round_count: int = 0
    last_eval_round: Optional[int] = None
    in_flight_rounds: set[int] = field(default_factory=set)
    card: Optional[SpectatorCard] = None
    cooldowns: Dict[str, CooldownState] = field(default_factory=dict)
    retry_context: Optional[_RetryContext] = None


class SpectatorService(BaseModel):
    """旁观 agent 本体: 编排评估 → 匹配 → 卡片生命周期。"""

    model_config = {"arbitrary_types_allowed": True}

    config: SpectatorConfig
    evaluator: IntentEvaluator
    searcher: QuestionSearcher
    logger: SpectatorLogger
    evaluator_factory: Optional[EvaluatorFactory] = None  # hint → 评估器（集成层注入）

    # 运行时状态（非 Pydantic 字段语义, 用私有属性承载）
    def __init__(self, **data):
        super().__init__(**data)
        object.__setattr__(self, "_sessions", {})
        object.__setattr__(
            self, "_trigger", RoundTrigger(interval_rounds=self.config.interval_rounds)
        )
        object.__setattr__(self, "_cooldown_policy", CooldownPolicy())
        object.__setattr__(self, "_lifecycle", CardLifecycle())
        object.__setattr__(self, "_evaluator_cache", {})

    # ── 观察入口（API 钩子每轮调用）────────────────────────────────────

    async def observe_round(
        self,
        session_id: str,
        transcript_window: List[ConversationTurn],
        session_bound: bool,
        round_count: int,
        provider_hint: Optional[ProviderHint] = None,
    ) -> Optional[SpectatorCard]:
        """一轮对话完成后调用（应在后台 task 中执行）。

        - round_count: 该会话已完成的完整轮次（由 API 钩子从会话历史计算,
          避免内部计数与真实历史漂移）。
        - provider_hint: 会话级 provider（D4）; 有效时优先于默认评估器。
        - 返回新生成的 active 卡片; 静默/降级/未触发时返回 None。
        - 永不抛出。
        """
        try:
            return await self._observe_round_inner(
                session_id,
                transcript_window,
                session_bound,
                round_count,
                provider_hint,
            )
        except Exception:  # noqa: BLE001 —— 不变式: 永不外抛
            logger.exception("spectator observe_round crashed for %s", session_id)
            self._log_safe(
                "degraded", session_id, round_count, {"reason": "internal_error"}
            )
            return None

    async def _observe_round_inner(
        self,
        session_id: str,
        transcript_window: List[ConversationTurn],
        session_bound: bool,
        round_no: int,
        provider_hint: Optional[ProviderHint] = None,
    ) -> Optional[SpectatorCard]:
        runtime = self._runtime(session_id)

        if not self.config.enabled:
            return None
        if session_bound:
            self._log_safe(
                "degraded", session_id, round_no, {"reason": "session_bound"}
            )
            return None
        if not self._trigger.should_evaluate(round_no, runtime.last_eval_round):
            return None
        if round_no in runtime.in_flight_rounds:
            return None

        window = transcript_window[-self.config.window_messages :]

        # ── 评估（D4: 会话 provider hint 优先, 回退服务默认评估器）──
        evaluator = self._evaluator_for(provider_hint)
        runtime.in_flight_rounds.add(round_no)
        try:
            try:
                assessment = await self._assess_with_retry(evaluator, window)
            except EvaluationError as exc:
                self._supersede_existing(runtime)
                failure_card = SpectatorCard(
                    kind="failure",
                    created_at_round=round_no,
                    topic_summary="旁观 Agent 连接失败",
                    error_message=(
                        "无法连接旁观模型网关，请检查网络或 API 配置后重试。"
                        if exc.retryable
                        else "旁观模型调用失败，请检查 API 配置后重试。"
                    ),
                    retryable=True,
                )
                runtime.card = failure_card
                runtime.retry_context = _RetryContext(
                    window=list(window),
                    session_bound=session_bound,
                    round_no=round_no,
                    provider_hint=provider_hint,
                )
                self._log_safe(
                    "degraded",
                    session_id,
                    round_no,
                    {
                        "reason": "eval_error",
                        "detail": str(exc)[:200],
                        "attempts": exc.attempts,
                        "retryable": exc.retryable,
                        "card_id": failure_card.card_id,
                    },
                )
                return failure_card
        finally:
            runtime.in_flight_rounds.discard(round_no)

        runtime.last_eval_round = max(runtime.last_eval_round or 0, round_no)
        runtime.retry_context = None
        if runtime.card is not None and runtime.card.kind == "failure":
            runtime.card.mark(CardStatus.SUPERSEDED, None)
            runtime.card = None

        self._log_safe(
            "evaluation",
            session_id,
            round_no,
            {
                "intent": assessment.intent,
                "confidence": assessment.confidence,
                "topic_summary": assessment.topic_summary,
                "keywords": assessment.keywords,
            },
        )

        if assessment.intent != "knowledge_seeking":
            return None  # 静默（FR-004）
        if assessment.confidence < self.config.min_intent_confidence:
            return None  # 低置信度按不明确处理（FR-003 闸门）

        # ── 匹配 ──
        try:
            if self.config.recall_test_mode:
                recall_searcher = getattr(self.searcher, "recall_test_questions", None)
                if not callable(recall_searcher):
                    raise RuntimeError(
                        "recall test mode requires a recall_test_questions searcher"
                    )
                docs = await recall_searcher()
            else:
                docs = await self.searcher.usable_questions()
        except Exception as exc:  # noqa: BLE001
            self._log_safe(
                "degraded",
                session_id,
                round_no,
                {"reason": "repository_error", "detail": str(exc)[:200]},
            )
            return None

        # 旧卡候选视为 ignored（评分惩罚）; 仅在真正产生新卡时才落冷却
        # （无新卡时旧卡保持 active, FR-015）
        effective_cooldowns = dict(runtime.cooldowns)
        old = runtime.card
        if old is not None and old.status not in (
            CardStatus.ACCEPTED,
            CardStatus.REJECTED,
        ):
            for qid in old.candidate_ids():
                effective_cooldowns.setdefault(
                    qid, self._cooldown_policy.record(qid, UserAction.IGNORED)
                )

        candidates = score_candidates(
            assessment, docs, effective_cooldowns, self.config
        )
        if not candidates:
            return None  # 无有效候选 → 静默（FR-007 场景 2）

        # ── 卡片生命周期: 新卡产生 → 旧 active 卡替换 + 冷却落账 ──
        self._supersede_existing(runtime)
        card = SpectatorCard(
            created_at_round=round_no,
            topic_summary=assessment.topic_summary,
            keywords=assessment.keywords,
            candidates=candidates,
        )
        runtime.card = card
        self._log_safe(
            "recommendation",
            session_id,
            round_no,
            {
                "card_id": card.card_id,
                "candidates": [
                    {
                        "question_id": c.question_id,
                        "score": round(c.score, 4),
                        "disabled": c.disabled,
                    }
                    for c in card.candidates
                ],
                "recall_test_mode": self.config.recall_test_mode,
            },
        )
        return card

    async def _assess_with_retry(
        self,
        evaluator: IntentEvaluator,
        window: List[ConversationTurn],
    ) -> IntentAssessment:
        """对瞬时模型网关故障做有限重试，不让一次抖动吞掉轮次触发。"""
        last_error: Optional[EvaluationError] = None
        for attempt in range(self.config.eval_retry_attempts):
            try:
                return await evaluator.assess(window)
            except EvaluationError as exc:
                last_error = exc
                exc.attempts = attempt + 1
                if not exc.retryable or attempt + 1 >= self.config.eval_retry_attempts:
                    break
                delay = self.config.eval_retry_backoff_seconds * (2**attempt)
                if delay:
                    await asyncio.sleep(delay)
        assert last_error is not None
        raise last_error

    # ── 卡片查询与动作 ──────────────────────────────────────────────────

    def get_card(self, session_id: str) -> Optional[SpectatorCard]:
        """仅返回 active 卡片（轮询端点用; 非 active → None → HTTP 204）。"""
        runtime = self._sessions.get(session_id)
        if runtime is None or runtime.card is None:
            return None
        if runtime.card.status != CardStatus.ACTIVE:
            return None
        return runtime.card

    async def retry_failed(self, session_id: str, card_id: str) -> Optional[SpectatorCard]:
        """按失败时保存的同一轮上下文重新执行旁观评估。"""
        runtime = self._require_runtime(session_id)
        card = runtime.card
        context = runtime.retry_context
        if (
            card is None
            or card.card_id != card_id
            or card.kind != "failure"
            or card.status != CardStatus.ACTIVE
            or context is None
        ):
            raise CardStateError(f"failure card {card_id} is not retryable")
        return await self.observe_round(
            session_id,
            context.window,
            context.session_bound,
            context.round_no,
            context.provider_hint,
        )

    async def abandon_failed(self, session_id: str, card_id: str) -> None:
        """放弃本次失败评估并清理重试上下文。"""
        runtime = self._require_runtime(session_id)
        card = runtime.card
        if (
            card is None
            or card.card_id != card_id
            or card.kind != "failure"
            or card.status != CardStatus.ACTIVE
        ):
            raise CardStateError(f"failure card {card_id} is not active")
        card.mark(CardStatus.REJECTED, UserAction.REJECTED)
        runtime.retry_context = None
        self._log_safe(
            "user_action",
            session_id,
            card.created_at_round,
            {"card_id": card_id, "action": "abandoned", "question_id": None},
        )

    async def accept(
        self, session_id: str, card_id: str, question_id: str
    ) -> AcceptResult:
        """用户点击「开始学习」: 卡片 → accepted + cooldown(accepted)。

        幂等: 对同一张已 accepted 卡片重复 accept 同一题目返回原结果。
        """
        runtime = self._require_runtime(session_id)
        card = runtime.card
        if card is None or card.card_id != card_id:
            raise CardStateError(f"card {card_id} not found in session {session_id}")

        candidate = next(
            (item for item in card.candidates if item.question_id == question_id),
            None,
        )
        if candidate is None:
            raise CardStateError(
                f"question {question_id} is not a candidate of card {card_id}"
            )
        if candidate.disabled or not candidate.learnable:
            reason = candidate.disabled_reason or "candidate has no usable solution"
            raise CardStateError(
                f"question {question_id} is not learnable: {reason}"
            )

        if card.status == CardStatus.ACCEPTED:
            if card.user_action == UserAction.ACCEPTED and question_id in (
                card.candidate_ids() or [question_id]
            ):
                return AcceptResult(
                    question_id=question_id, card_id=card_id, idempotent=True
                )
            raise CardStateError(f"card {card_id} already accepted")

        card.mark(CardStatus.ACCEPTED, UserAction.ACCEPTED)
        runtime.cooldowns[question_id] = self._cooldown_policy.record(
            question_id, UserAction.ACCEPTED
        )
        self._log_safe(
            "user_action",
            session_id,
            card.created_at_round,
            {"card_id": card_id, "action": "accepted", "question_id": question_id},
        )
        return AcceptResult(question_id=question_id, card_id=card_id)

    async def decline(self, session_id: str, card_id: str) -> None:
        """用户点击「不感兴趣」: 卡片 → rejected, 全候选 cooldown(rejected)。"""
        runtime = self._require_runtime(session_id)
        card = runtime.card
        if card is None or card.card_id != card_id:
            raise CardStateError(f"card {card_id} not found in session {session_id}")
        if card.status != CardStatus.ACTIVE:
            raise CardStateError(f"card {card_id} is {card.status.value}, not active")

        card.mark(CardStatus.REJECTED, UserAction.REJECTED)
        for qid in card.candidate_ids():
            runtime.cooldowns[qid] = self._cooldown_policy.record(
                qid, UserAction.REJECTED
            )
        self._log_safe(
            "user_action",
            session_id,
            card.created_at_round,
            {"card_id": card_id, "action": "rejected", "question_id": None},
        )

    def drop_session(self, session_id: str) -> None:
        """会话删除时清理运行时状态（防泄漏）。"""
        self._sessions.pop(session_id, None)

    # ── 内部 ────────────────────────────────────────────────────────────

    def _evaluator_for(self, hint: Optional[ProviderHint]) -> IntentEvaluator:
        """解析本次评估用的评估器。

        - hint 无效（None/全空）→ 服务默认评估器（UI 文件 > env, 构造时解析）
        - hint 有效 → 工厂构造并按 (api_base, api_key, model) 缓存复用
        """
        if hint is None or not hint.is_effective():
            return self.evaluator
        key = (hint.api_base, hint.api_key, hint.model)
        cached = self._evaluator_cache.get(key)
        if cached is not None:
            return cached
        factory = self.evaluator_factory
        if factory is None:
            return self.evaluator  # 无工厂（纯测试场景）→ 默认
        evaluator = factory(hint)
        self._evaluator_cache[key] = evaluator
        return evaluator

    def _runtime(self, session_id: str) -> _SessionRuntime:
        runtime = self._sessions.get(session_id)
        if runtime is None:
            runtime = _SessionRuntime()
            self._sessions[session_id] = runtime
        return runtime

    def _require_runtime(self, session_id: str) -> _SessionRuntime:
        runtime = self._sessions.get(session_id)
        if runtime is None:
            raise CardStateError(f"session {session_id} has no spectator state")
        return runtime

    def _supersede_existing(self, runtime: _SessionRuntime) -> None:
        """旧 active 卡片 → superseded, 候选落 ignored 冷却（FR-015/FR-009）。"""
        old = runtime.card
        if old is None:
            return
        if old.status == CardStatus.ACTIVE:
            old.mark(CardStatus.SUPERSEDED, None)
        if old.status in (CardStatus.SUPERSEDED, CardStatus.IGNORED):
            for qid in old.candidate_ids():
                if qid not in runtime.cooldowns:
                    runtime.cooldowns[qid] = self._cooldown_policy.record(
                        qid, UserAction.IGNORED
                    )

    def _log_safe(
        self, event: str, session_id: str, round_no: int, payload: dict
    ) -> None:
        try:
            self.logger.log(event, session_id, round_no, payload)
        except Exception:  # noqa: BLE001 —— 日志失败不反噬
            logger.debug("spectator log write failed", exc_info=True)
