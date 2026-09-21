# -*- coding: utf-8 -*-
"""纯逻辑: RoundTrigger / CooldownPolicy / CardLifecycle（契约 module.md §3）。

无 IO、无外部依赖; CardLifecycle 在 US2 阶段（T015）补齐。
"""

from __future__ import annotations

from typing import Dict, Optional

from pydantic import BaseModel, Field

from .models import (
    CardStatus,
    CooldownState,
    SpectatorCard,
    UserAction,
)


class CardStateError(Exception):
    """卡片状态机非法转移/非法参数。"""


class RoundTrigger(BaseModel):
    """评估触发的轮次边界逻辑（FR-002）。

    应当评估 ⇔ round 是 N 的正整数倍 且 round > last_eval_round。
    """

    interval_rounds: int = Field(ge=1)

    def should_evaluate(self, round_count: int, last_eval_round: Optional[int]) -> bool:
        if round_count <= 0:
            return False
        if round_count % self.interval_rounds != 0:
            return False
        if last_eval_round is not None and round_count <= last_eval_round:
            return False
        return True


class CooldownPolicy(BaseModel):
    """冷却策略（FR-009）: rejected/accepted 屏蔽; ignored 惩罚 0.5。"""

    ignored_penalty: float = Field(default=0.5, ge=0.0, le=1.0)

    def is_blocked(self, question_id: str, cooldowns: Dict[str, CooldownState]) -> bool:
        cd = cooldowns.get(question_id)
        if cd is None:
            return False
        return cd.action in (UserAction.REJECTED, UserAction.ACCEPTED)

    def penalty(self, question_id: str, cooldowns: Dict[str, CooldownState]) -> float:
        """评分乘数: 无冷却 1.0; ignored → ignored_penalty; 已屏蔽 0.0。"""
        cd = cooldowns.get(question_id)
        if cd is None:
            return 1.0
        if self.is_blocked(question_id, cooldowns):
            return 0.0
        if cd.action == UserAction.IGNORED:
            return self.ignored_penalty
        return 1.0

    def record(self, question_id: str, action: UserAction) -> CooldownState:
        return CooldownState(question_id=question_id, action=action)


class CardLifecycle(BaseModel):
    """卡片状态机（data-model.md §3.1）: active 为唯一非终态。

    - accept: active → accepted（question 必须是卡片候选）
    - decline: active → rejected
    - supersede: active → superseded, 候选落 ignored 冷却（供上层合并）
    """

    def accept(self, card: SpectatorCard, question_id: str) -> None:
        if card.status != CardStatus.ACTIVE:
            raise CardStateError(
                f"card {card.card_id} is {card.status.value}, not active"
            )
        if question_id not in card.candidate_ids():
            raise CardStateError(
                f"question {question_id} is not a candidate of card {card.card_id}"
            )
        card.mark(CardStatus.ACCEPTED, UserAction.ACCEPTED)

    def decline(self, card: SpectatorCard) -> None:
        if card.status != CardStatus.ACTIVE:
            raise CardStateError(
                f"card {card.card_id} is {card.status.value}, not active"
            )
        card.mark(CardStatus.REJECTED, UserAction.REJECTED)

    def supersede(self, card: SpectatorCard) -> Dict[str, CooldownState]:
        """旧 active 卡 → superseded; 返回候选的 ignored 冷却（未含已冷却项）。"""
        if card.status != CardStatus.ACTIVE:
            return {}
        card.mark(CardStatus.SUPERSEDED, None)
        return {
            qid: CooldownState(question_id=qid, action=UserAction.IGNORED)
            for qid in card.candidate_ids()
        }
