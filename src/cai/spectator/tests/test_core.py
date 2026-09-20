# -*- coding: utf-8 -*-
"""T005/T006: 纯逻辑 RoundTrigger / CooldownPolicy（契约 module.md §3）。

TDD Red 阶段：先于实现编写，运行应失败。
"""

import pytest

from spectator.core import CardLifecycle, CardStateError, CooldownPolicy, RoundTrigger
from spectator.models import (
    CardStatus,
    CooldownState,
    RecommendationCandidate,
    SpectatorCard,
    UserAction,
)


class TestRoundTrigger:
    def test_default_interval_boundaries(self):
        trigger = RoundTrigger(interval_rounds=3)
        assert trigger.should_evaluate(3, None) is True
        assert trigger.should_evaluate(6, None) is True
        assert trigger.should_evaluate(9, None) is True

    def test_non_boundary_rounds_do_not_trigger(self):
        trigger = RoundTrigger(interval_rounds=3)
        assert trigger.should_evaluate(1, None) is False
        assert trigger.should_evaluate(2, None) is False
        assert trigger.should_evaluate(4, None) is False
        assert trigger.should_evaluate(5, None) is False
        assert trigger.should_evaluate(7, None) is False

    def test_zero_round_never_triggers(self):
        trigger = RoundTrigger(interval_rounds=3)
        assert trigger.should_evaluate(0, None) is False

    def test_last_eval_round_prevents_re_evaluation(self):
        trigger = RoundTrigger(interval_rounds=3)
        # 已在第 3 轮评估过: 第 3 轮不重复, 第 6 轮可以
        assert trigger.should_evaluate(3, last_eval_round=3) is False
        assert trigger.should_evaluate(6, last_eval_round=3) is True
        # 已在第 6 轮评估过: 第 6 轮不重复
        assert trigger.should_evaluate(6, last_eval_round=6) is False

    def test_interval_one_every_round(self):
        trigger = RoundTrigger(interval_rounds=1)
        for r in (1, 2, 3, 4):
            assert trigger.should_evaluate(r, None) is True

    def test_invalid_interval_rejected(self):
        with pytest.raises(Exception):
            RoundTrigger(interval_rounds=0)


class TestCooldownPolicy:
    def setup_method(self):
        self.policy = CooldownPolicy()

    def _cds(self, **by_qid):
        return {
            qid: CooldownState(question_id=qid, action=action)
            for qid, action in by_qid.items()
        }

    def test_rejected_blocks(self):
        cds = self._cds(q1="rejected")
        assert self.policy.is_blocked("q1", cds) is True

    def test_accepted_blocks(self):
        cds = self._cds(q1="accepted")
        assert self.policy.is_blocked("q1", cds) is True

    def test_ignored_does_not_block(self):
        cds = self._cds(q1="ignored")
        assert self.policy.is_blocked("q1", cds) is False

    def test_unknown_question_not_blocked(self):
        assert self.policy.is_blocked("qX", {}) is False

    def test_penalty_ignored_is_half(self):
        cds = self._cds(q1="ignored")
        assert self.policy.penalty("q1", cds) == 0.5

    def test_penalty_no_cooldown_is_one(self):
        assert self.policy.penalty("q1", {}) == 1.0
        cds = self._cds(q1="rejected")
        assert self.policy.penalty("q1", cds) == 0.0  # 已屏蔽的题目惩罚为 0

    def test_record_creates_state(self):
        cd = self.policy.record("q9", UserAction.REJECTED)
        assert cd.question_id == "q9"
        assert cd.action == UserAction.REJECTED
        assert cd.created_at is not None


def _card(candidates=("q1",), round_no=3):
    return SpectatorCard(
        card_id="c-1",
        created_at_round=round_no,
        topic_summary="s",
        keywords=[],
        candidates=[
            RecommendationCandidate(
                question_id=qid, title_preview="t", score=0.5, reason="r"
            )
            for qid in candidates
        ],
    )


class TestCardLifecycle:
    def setup_method(self):
        self.lifecycle = CardLifecycle()

    def test_supersede_active_card(self):
        card = _card()
        cooldowns = self.lifecycle.supersede(card)
        assert card.status == CardStatus.SUPERSEDED
        assert card.user_action is None
        assert set(cooldowns.keys()) == {"q1"}
        assert all(cd.action == UserAction.IGNORED for cd in cooldowns.values())

    def test_supersede_non_active_noop(self):
        card = _card()
        card.mark(CardStatus.REJECTED, UserAction.REJECTED)
        assert self.lifecycle.supersede(card) == {}

    def test_accept_active_card(self):
        card = _card()
        self.lifecycle.accept(card, "q1")
        assert card.status == CardStatus.ACCEPTED
        assert card.user_action == UserAction.ACCEPTED

    def test_accept_invalid_question_raises(self):
        card = _card()
        with pytest.raises(CardStateError):
            self.lifecycle.accept(card, "qX")

    def test_accept_non_active_raises(self):
        card = _card()
        self.lifecycle.accept(card, "q1")
        with pytest.raises(CardStateError):
            self.lifecycle.accept(card, "q1")

    def test_decline_active_card(self):
        card = _card(("q1", "q2"))
        self.lifecycle.decline(card)
        assert card.status == CardStatus.REJECTED
        assert card.user_action == UserAction.REJECTED

    def test_decline_non_active_raises(self):
        card = _card()
        self.lifecycle.decline(card)
        with pytest.raises(CardStateError):
            self.lifecycle.decline(card)
