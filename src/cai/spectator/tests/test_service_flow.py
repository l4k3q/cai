# -*- coding: utf-8 -*-
"""T011/T016/T019/T022/T025: SpectatorService 编排全流程（契约 module.md §6）。

分故事推进（TDD Red → Green）:
- US1 (T011): 守卫 + 评估 + 静默 + 降级
- US2 (T016): 匹配 → 卡片生成
- US3 (T019): accept
- US4 (T022): decline + 冷却应用
- US5 (T025): searcher 故障降级 + 日志链回溯
"""

import json

import pytest

from spectator.config import SpectatorConfig
from spectator.evaluator import EvaluationError
from spectator.models import (
    IntentAssessment,
    QuestionDoc,
    UserAction,
)
from spectator.service import CardStateError, SpectatorService
from spectator.tests.fakes import FakeIntentEvaluator, FakeQuestionSearcher

# ── 公共装置 ────────────────────────────────────────────────────────────────

WINDOW = [
    ("user", "redis 未授权访问是什么原理"),
    ("assistant", "Redis 未授权访问指未设密码直接连 6379…"),
    ("user", "怎么利用它写 ssh key"),
]

REDIS_DOC = QuestionDoc(
    question_id="q-redis",
    statement="某内网 Redis 4.0.14 未授权访问，可通过写入 SSH key 获取服务器权限。请复现攻击过程并获取 flag。",
    principles_text="利用 Redis 未授权访问写 SSH 公钥到 root 的 authorized_keys 实现登录",
)
LOG4J_DOC = QuestionDoc(
    question_id="q-log4j",
    statement="Web 应用存在 Log4Shell (CVE-2021-44228) 漏洞，请通过 JNDI 注入获取反向 Shell。",
    principles_text="Log4j JNDI 注入加载远程类执行命令",
)


def knowledge_assessment():
    return IntentAssessment(
        intent="knowledge_seeking",
        confidence=0.82,
        topic_summary="Redis 未授权访问利用",
        keywords=["redis", "未授权访问", "ssh key"],
    )


def make_service(
    scripted_assessment=None,
    scripted_error=None,
    docs=None,
    searcher_error=None,
    enabled=True,
    config=None,
    tmp_path=None,
    recall_docs=None,
):
    from spectator.logging_utils import SpectatorLogger

    cfg = config or SpectatorConfig(
        enabled=enabled,
        eval_retry_backoff_seconds=0,
    )
    evaluator = FakeIntentEvaluator(scripted_assessment, scripted_error)
    searcher = FakeQuestionSearcher(docs, searcher_error, recall_docs=recall_docs)
    logger = SpectatorLogger(
        log_dir=str(tmp_path) if tmp_path else "/nonexistent-should-be-silent",
        filename="spectator_test.jsonl" if tmp_path else "x.jsonl",
    )
    service = SpectatorService(
        config=cfg, evaluator=evaluator, searcher=searcher, logger=logger
    )
    return service, evaluator, searcher


# ── US1: 守卫 + 评估 + 静默 + 降级（T011）──────────────────────────────────


class TestObserveRoundGuards:
    async def test_disabled_never_evaluates(self, tmp_path):
        service, evaluator, _ = make_service(enabled=False, tmp_path=tmp_path)
        card = await service.observe_round(
            "s1", WINDOW, session_bound=False, round_count=3
        )
        assert card is None
        assert evaluator.calls == 0

    async def test_default_interval_evaluates_each_round(self, tmp_path):
        service, evaluator, _ = make_service(tmp_path=tmp_path)
        for r in (1, 2):
            card = await service.observe_round(
                "s1", WINDOW[:1], session_bound=False, round_count=r
            )
            assert card is None
        assert evaluator.calls == 2

    async def test_session_bound_skips_and_logs(self, tmp_path):
        service, evaluator, _ = make_service(tmp_path=tmp_path)
        card = await service.observe_round(
            "s1", WINDOW, session_bound=True, round_count=3
        )
        assert card is None
        assert evaluator.calls == 0
        events = _read_log(tmp_path)
        assert any(
            e["event"] == "degraded" and e["payload"].get("reason") == "session_bound"
            for e in events
        )

    async def test_re_evaluation_at_same_round_skipped(self, tmp_path):
        service, evaluator, _ = make_service(tmp_path=tmp_path)
        await service.observe_round(
            "s1", WINDOW, session_bound=False, round_count=3
        )  # 第 3 轮
        assert evaluator.calls == 1
        # 同一轮次重复通知（竞态/重放）: 不重复评估
        card = await service.observe_round(
            "s1", WINDOW, session_bound=False, round_count=3
        )
        assert evaluator.calls == 1


class TestObserveRoundAssessment:
    async def test_non_knowledge_silent_with_evaluation_log(self, tmp_path):
        for intent in ("task_delegation", "chitchat", "unclear"):
            service, _, _ = make_service(
                scripted_assessment=IntentAssessment(
                    intent=intent, confidence=0.9, topic_summary="t", keywords=[]
                ),
                tmp_path=tmp_path,
            )
            card = await service.observe_round(
                "s1", WINDOW, session_bound=False, round_count=3
            )
            assert card is None
        events = _read_log(tmp_path)
        assert len([e for e in events if e["event"] == "evaluation"]) >= 3
        assert not [e for e in events if e["event"] == "recommendation"]

    async def test_low_confidence_treated_as_unclear_silent(self, tmp_path):
        service, _, _ = make_service(
            scripted_assessment=IntentAssessment(
                intent="knowledge_seeking",
                confidence=0.3,
                topic_summary="t",
                keywords=["redis"],
            ),
            tmp_path=tmp_path,
        )
        card = await service.observe_round(
            "s1", WINDOW, session_bound=False, round_count=3
        )
        assert card is None

    async def test_evaluator_error_degrades_silently(self, tmp_path):
        service, evaluator, _ = make_service(
            scripted_error=EvaluationError("gateway 503"), tmp_path=tmp_path
        )
        card = await service.observe_round(
            "s1", WINDOW, session_bound=False, round_count=3
        )
        assert card is not None
        assert card.kind == "failure"
        assert card.retryable is True
        assert evaluator.calls == 1
        events = _read_log(tmp_path)
        assert any(
            e["event"] == "degraded" and "eval" in e["payload"].get("reason", "")
            for e in events
        )

    async def test_transient_evaluator_error_retries_same_round(self, tmp_path):
        assessment = IntentAssessment(
            intent="knowledge_seeking",
            confidence=0.95,
            topic_summary="Redis 未授权访问",
            keywords=["redis", "未授权访问"],
        )
        service, evaluator, _ = make_service(
            scripted_assessment=assessment,
            docs=[REDIS_DOC],
            tmp_path=tmp_path,
        )
        evaluator._transient_failures = 1

        card = await service.observe_round(
            "s1", WINDOW, session_bound=False, round_count=3
        )

        assert card is not None
        assert evaluator.calls == 2

    async def test_failed_evaluation_does_not_consume_round(self, tmp_path):
        service, evaluator, _ = make_service(
            scripted_assessment=knowledge_assessment(),
            scripted_error=EvaluationError("invalid credentials"),
            docs=[REDIS_DOC],
            tmp_path=tmp_path,
        )

        failed = await service.observe_round("s1", WINDOW, False, 3)
        assert failed is not None and failed.kind == "failure"
        evaluator._error = None
        card = await service.retry_failed("s1", failed.card_id)

        assert card is not None
        assert card.kind == "recommendation"
        assert evaluator.calls == 2

    async def test_user_can_abandon_failure_card(self, tmp_path):
        service, _, _ = make_service(
            scripted_error=EvaluationError("invalid credentials"), tmp_path=tmp_path
        )
        card = await service.observe_round("s1", WINDOW, False, 3)
        assert card is not None

        await service.abandon_failed("s1", card.card_id)

        assert service.get_card("s1") is None

    async def test_window_truncated_to_recent_six_messages(self, tmp_path):
        service, evaluator, _ = make_service(tmp_path=tmp_path)
        long_window = [("user", f"msg{i}") for i in range(10)]
        await service.observe_round(
            "s1", long_window, session_bound=False, round_count=1
        )
        assert evaluator.last_transcript == long_window[-6:]

    async def test_observe_round_never_raises(self, tmp_path):
        # searcher 在 US2 才被调用; 这里 evaluator 直接抛非预期异常类型
        service, _, _ = make_service(
            scripted_error=RuntimeError("surprise"), tmp_path=tmp_path
        )
        card = await service.observe_round(
            "s1", WINDOW, session_bound=False, round_count=3
        )
        assert card is None  # 任何异常都被吞掉并降级


def _read_log(tmp_path):
    log_file = tmp_path / "spectator_test.jsonl"
    if not log_file.exists():
        return []
    return [
        json.loads(line)
        for line in log_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ── US2: 匹配 → 卡片（T016）────────────────────────────────────────────────


class TestObserveRoundRecommendation:
    async def test_knowledge_with_candidates_returns_card(self, tmp_path):
        service, _, _ = make_service(
            scripted_assessment=knowledge_assessment(),
            docs=[REDIS_DOC, LOG4J_DOC],
            tmp_path=tmp_path,
        )
        card = await service.observe_round(
            "s1", WINDOW, session_bound=False, round_count=3
        )
        assert card is not None
        assert card.created_at_round == 3
        assert card.topic_summary == "Redis 未授权访问利用"
        assert card.candidates[0].question_id == "q-redis"
        assert len(card.candidates) <= 3
        # reason 含命中关键词, 不含敏感内容
        assert "redis" in card.candidates[0].reason.lower()

    async def test_knowledge_without_usable_candidates_silent(self, tmp_path):
        service, _, _ = make_service(
            scripted_assessment=knowledge_assessment(), docs=[], tmp_path=tmp_path
        )
        card = await service.observe_round(
            "s1", WINDOW, session_bound=False, round_count=3
        )
        assert card is None
        # 题面完全不相关的候选同样静默
        service2, _, _ = make_service(
            scripted_assessment=knowledge_assessment(),
            docs=[
                QuestionDoc(
                    question_id="q-x",
                    statement="量子加密协议分析",
                    principles_text="格密码",
                )
            ],
            tmp_path=tmp_path,
        )
        assert (
            await service2.observe_round(
                "s1", WINDOW, session_bound=False, round_count=3
            )
            is None
        )

    async def test_new_card_supersedes_old_and_ignored_cooldown(self, tmp_path):
        service, _, _ = make_service(
            scripted_assessment=knowledge_assessment(),
            docs=[REDIS_DOC],
            tmp_path=tmp_path,
        )
        card1 = await service.observe_round(
            "s1", WINDOW, session_bound=False, round_count=3
        )
        assert card1 is not None and card1.status.value == "active"
        # 第 6 轮新评估: 旧卡被替换, 候选进入 ignored 冷却
        card2 = await service.observe_round(
            "s1", WINDOW, session_bound=False, round_count=6
        )
        assert card2 is not None
        assert card2.card_id != card1.card_id
        assert card1.status.value == "superseded"
        # ignored 惩罚 0.5 后 redis 仍应高分命中（0.61*0.5=0.305 > 0.15）
        assert card2.candidates[0].question_id == "q-redis"

    async def test_recommendation_log_written(self, tmp_path):
        service, _, _ = make_service(
            scripted_assessment=knowledge_assessment(),
            docs=[REDIS_DOC],
            tmp_path=tmp_path,
        )
        await service.observe_round("s1", WINDOW, session_bound=False, round_count=3)
        events = _read_log(tmp_path)
        rec = [e for e in events if e["event"] == "recommendation"]
        assert len(rec) == 1
        assert rec[0]["payload"]["candidates"][0]["question_id"] == "q-redis"


# ── US3: accept（T019）─────────────────────────────────────────────────────


class TestAccept:
    async def _card(self, tmp_path):
        service, _, _ = make_service(
            scripted_assessment=knowledge_assessment(),
            docs=[REDIS_DOC, LOG4J_DOC],
            tmp_path=tmp_path,
        )
        card = await service.observe_round(
            "s1", WINDOW, session_bound=False, round_count=3
        )
        return service, card

    async def test_accept_active_card(self, tmp_path):
        service, card = await self._card(tmp_path)
        result = await service.accept("s1", card.card_id, "q-redis")
        assert result.question_id == "q-redis"
        # accepted 卡片不再经轮询端点下发
        assert service.get_card("s1") is None
        # 幂等: 同卡片同题目重复 accept 返回原结果
        again = await service.accept("s1", card.card_id, "q-redis")
        assert again.question_id == "q-redis" and again.idempotent is True

    async def test_accept_wrong_question_raises(self, tmp_path):
        service, card = await self._card(tmp_path)
        with pytest.raises(CardStateError):
            await service.accept("s1", card.card_id, "q-not-in-card")

    async def test_accept_not_active_raises(self, tmp_path):
        service, card = await self._card(tmp_path)
        await service.accept("s1", card.card_id, "q-redis")
        # 契约: 同卡片同题目重复 accept 幂等返回; decline 已 accepted 的卡片 → 409
        idem = await service.accept("s1", card.card_id, "q-redis")
        assert idem.idempotent is True
        with pytest.raises(CardStateError):
            await service.decline("s1", card.card_id)

    async def test_accept_idempotent_same_question(self, tmp_path):
        service, card = await self._card(tmp_path)
        r1 = await service.accept("s1", card.card_id, "q-redis")
        r2 = await service.accept("s1", card.card_id, "q-redis")
        assert r1.question_id == r2.question_id == "q-redis"

    async def test_accept_unknown_card_raises(self, tmp_path):
        service, _ = await self._card(tmp_path)
        with pytest.raises(CardStateError):
            await service.accept("s1", "no-such-card", "q-redis")

    async def test_user_action_logged(self, tmp_path):
        service, card = await self._card(tmp_path)
        await service.accept("s1", card.card_id, "q-redis")
        events = _read_log(tmp_path)
        ua = [e for e in events if e["event"] == "user_action"]
        assert ua and ua[-1]["payload"]["action"] == "accepted"


# ── US4: decline + 冷却（T022）─────────────────────────────────────────────


class TestDecline:
    async def _card(self, tmp_path):
        service, _, _ = make_service(
            scripted_assessment=knowledge_assessment(),
            docs=[REDIS_DOC],
            tmp_path=tmp_path,
        )
        card = await service.observe_round(
            "s1", WINDOW, session_bound=False, round_count=3
        )
        return service, card

    async def test_decline_marks_rejected(self, tmp_path):
        service, card = await self._card(tmp_path)
        await service.decline("s1", card.card_id)
        assert service.get_card("s1") is None  # 非 active 不再下发
        assert card.status.value == "rejected"

    async def test_decline_blocks_future_recommendation(self, tmp_path):
        service, card = await self._card(tmp_path)
        await service.decline("s1", card.card_id)
        # 同主题再评估（第 6 轮）: redis 已被拒绝 → 无候选 → 静默
        card2 = await service.observe_round(
            "s1", WINDOW, session_bound=False, round_count=6
        )
        assert card2 is None

    async def test_decline_not_active_raises(self, tmp_path):
        service, card = await self._card(tmp_path)
        await service.decline("s1", card.card_id)
        with pytest.raises(CardStateError):
            await service.decline("s1", card.card_id)

    async def test_ignored_orders_below_new_candidates(self, tmp_path):
        # 评估关键词同时命中两道题: 旧卡只含 redis（被替换→ignored×0.5）;
        # log4j 为无惩罚新候选 → 必须排在 redis 之前（FR-009/SC-006）
        both_hit = IntentAssessment(
            intent="knowledge_seeking",
            confidence=0.9,
            topic_summary="Redis 与 Log4j 利用",
            keywords=["redis", "log4j"],
        )
        service, _, _ = make_service(
            scripted_assessment=both_hit,
            docs=[REDIS_DOC, LOG4J_DOC],
            tmp_path=tmp_path,
        )
        # 第一次评估: 只召回 redis（模拟当时 log4j 尚未入库）
        service.searcher = FakeQuestionSearcher([REDIS_DOC])
        card1 = await service.observe_round(
            "s1", WINDOW, session_bound=False, round_count=3
        )
        assert card1.candidates[0].question_id == "q-redis"
        # 第二次评估: 两道都召回, redis 处于 ignored 冷却
        service.searcher = FakeQuestionSearcher([REDIS_DOC, LOG4J_DOC])
        card2 = await service.observe_round(
            "s1", WINDOW, session_bound=False, round_count=6
        )
        assert card2 is not None
        ids = [c.question_id for c in card2.candidates]
        assert set(ids) == {"q-redis", "q-log4j"}
        # 原始同分(各 1 关键词命中 statement) → redis ×0.5 惩罚后必然靠后
        assert ids[0] == "q-log4j"


# ── US5: 降级 + 回溯（T025）────────────────────────────────────────────────


class TestDegradationAndTraceability:
    async def test_searcher_error_degrades(self, tmp_path):
        service, _, _ = make_service(
            scripted_assessment=knowledge_assessment(),
            searcher_error=RuntimeError("pg down"),
            tmp_path=tmp_path,
        )
        card = await service.observe_round(
            "s1", WINDOW, session_bound=False, round_count=3
        )
        assert card is None
        events = _read_log(tmp_path)
        assert any(
            e["event"] == "degraded" and "repository" in e["payload"].get("reason", "")
            for e in events
        )

    async def test_full_chain_traceable_in_log(self, tmp_path):
        service, _, _ = make_service(
            scripted_assessment=knowledge_assessment(),
            docs=[REDIS_DOC],
            tmp_path=tmp_path,
        )
        card = await service.observe_round(
            "s1", WINDOW, session_bound=False, round_count=3
        )
        await service.accept("s1", card.card_id, "q-redis")
        events = _read_log(tmp_path)
        kinds = [e["event"] for e in events]
        assert kinds == ["evaluation", "recommendation", "user_action"]

    async def test_drop_session_clears_state(self, tmp_path):
        service, _, _ = make_service(
            scripted_assessment=knowledge_assessment(),
            docs=[REDIS_DOC],
            tmp_path=tmp_path,
        )
        await service.observe_round("s1", WINDOW, session_bound=False, round_count=3)
        service.drop_session("s1")
        assert service.get_card("s1") is None
        # 会话状态清空后重新评估可正常进行
        card = await service.observe_round(
            "s1", WINDOW, session_bound=False, round_count=3
        )
        assert card is not None
