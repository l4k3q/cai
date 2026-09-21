# -*- coding: utf-8 -*-
"""T004: 领域模型字段校验（data-model.md §1-§2）。

TDD Red 阶段：先于实现编写，运行应失败。
"""

import pytest

from spectator.models import (
    CardStatus,
    CooldownState,
    IntentAssessment,
    QuestionDoc,
    RecommendationCandidate,
    SpectatorCard,
    UserAction,
)


def _assessment(**kw) -> dict:
    base = {
        "intent": "knowledge_seeking",
        "confidence": 0.8,
        "topic_summary": "Redis 未授权访问",
        "keywords": ["redis", "未授权访问"],
    }
    base.update(kw)
    return base


class TestIntentAssessment:
    def test_valid(self):
        a = IntentAssessment(**_assessment())
        assert a.intent == "knowledge_seeking"
        assert a.keywords == ["redis", "未授权访问"]

    def test_intent_enum(self):
        for intent in ("knowledge_seeking", "task_delegation", "chitchat", "unclear"):
            IntentAssessment(**_assessment(intent=intent))
        with pytest.raises(Exception):
            IntentAssessment(**_assessment(intent="gossip"))

    def test_confidence_bounds(self):
        with pytest.raises(Exception):
            IntentAssessment(**_assessment(confidence=1.5))
        with pytest.raises(Exception):
            IntentAssessment(**_assessment(confidence=-0.1))

    def test_keywords_dedup_and_trim(self):
        a = IntentAssessment(**_assessment(keywords=[" redis ", "redis", "RCE"]))
        assert a.keywords == ["redis", "RCE"]

    def test_keywords_max_items_and_length(self):
        too_many = [f"kw{i}" for i in range(13)]
        assessment = IntentAssessment(**_assessment(keywords=too_many))
        assert assessment.keywords == too_many[:12]
        with pytest.raises(Exception):
            IntentAssessment(**_assessment(keywords=["x" * 41]))

    def test_empty_keywords_allowed(self):
        a = IntentAssessment(**_assessment(keywords=[]))
        assert a.keywords == []


class TestRecommendationCandidate:
    def test_valid(self):
        c = RecommendationCandidate(
            question_id="q-1",
            title_preview="某内网 Redis 4.0.14 未授权访问…",
            score=0.61,
            reason="命中知识点：redis、未授权访问",
        )
        assert c.score == 0.61

    def test_score_bounds(self):
        kw = {"question_id": "q-1", "title_preview": "t", "reason": "r"}
        with pytest.raises(Exception):
            RecommendationCandidate(**kw, score=1.2)
        with pytest.raises(Exception):
            RecommendationCandidate(**kw, score=-0.05)

    def test_title_preview_max_121_chars(self):
        kw = {"question_id": "q-1", "score": 0.5, "reason": "r"}
        # 121 = 120 内容 + 省略号（合法）; 122 越界
        RecommendationCandidate(**kw, title_preview="x" * 121)
        with pytest.raises(Exception):
            RecommendationCandidate(**kw, title_preview="x" * 122)


class TestSpectatorCard:
    def test_valid_and_defaults(self):
        card = SpectatorCard(
            card_id="c-1",
            created_at_round=6,
            topic_summary="Redis 未授权访问",
            keywords=["redis"],
            candidates=[
                RecommendationCandidate(
                    question_id="q-1", title_preview="t", score=0.7, reason="r"
                )
            ],
        )
        assert card.status == CardStatus.ACTIVE
        assert card.user_action is None

    def test_candidates_max_and_desc_order(self):
        def cand(i, score):
            return RecommendationCandidate(
                question_id=f"q-{i}", title_preview="t", score=score, reason="r"
            )

        # 模型层绝对上限 5（运营上限默认 3 由 matcher 按配置裁剪, 见 test_matcher）
        with pytest.raises(Exception):
            SpectatorCard(
                card_id="c",
                created_at_round=3,
                topic_summary="s",
                keywords=[],
                candidates=[cand(i, 0.5) for i in range(6)],
            )
        # 乱序输入必须被拒绝（按分数降序不变式）
        with pytest.raises(Exception):
            SpectatorCard(
                card_id="c",
                created_at_round=3,
                topic_summary="s",
                keywords=[],
                candidates=[cand(1, 0.3), cand(2, 0.7)],
            )

    def test_created_at_round_multiple_of_interval(self):
        kw = {"card_id": "c", "topic_summary": "s", "keywords": [], "candidates": []}
        with pytest.raises(Exception):
            SpectatorCard(**kw, created_at_round=4)  # N=3 时 4 不是边界

    def test_empty_candidates_allowed(self):
        card = SpectatorCard(
            **{
                "card_id": "c",
                "created_at_round": 3,
                "topic_summary": "s",
                "keywords": [],
                "candidates": [],
            }
        )
        assert card.candidates == []


class TestCooldownState:
    def test_valid(self):
        cd = CooldownState(question_id="q-1", action="rejected")
        assert cd.action == UserAction.REJECTED
        with pytest.raises(Exception):
            CooldownState(question_id="q-1", action="maybe")


class TestQuestionDoc:
    def test_valid(self):
        q = QuestionDoc(
            question_id="q-1",
            statement="某内网 Redis 未授权访问",
            principles_text="利用 Redis 写 SSH key 的原理与防护",
        )
        assert q.question_id == "q-1"
