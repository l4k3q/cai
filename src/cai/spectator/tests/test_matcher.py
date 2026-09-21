# -*- coding: utf-8 -*-
"""T014: score_candidates 评分器 + StaticQuestionSearcher（契约 module.md §5）。

TDD Red 阶段：先于实现编写，运行应失败。

评分规则（research.md D5）:
- 关键词在 statement 命中权重 1.0, principles_text 命中 0.6（子串, 大小写不敏感）
- 题目得分 = Σ(命中关键词权重) / (关键词数 × 1.6) 归一到 [0,1]
- cooldown: rejected/accepted 屏蔽; ignored ×0.5
- 过滤: 得分 < match_threshold 剔除; 取前 max_candidates 按分数降序
"""

import pytest

from spectator.config import SpectatorConfig
from spectator.matcher import StaticQuestionSearcher, score_candidates
from spectator.models import CooldownState, IntentAssessment, QuestionDoc, UserAction


def assessment(keywords, topic="Redis 未授权访问利用"):
    return IntentAssessment(
        intent="knowledge_seeking",
        confidence=0.9,
        topic_summary=topic,
        keywords=keywords,
    )


REDIS = QuestionDoc(
    question_id="q-redis",
    statement="某内网 Redis 4.0.14 未授权访问，可通过写入 SSH key 获取服务器权限。",
    principles_text="利用 Redis 未授权访问写 SSH 公钥实现登录",
)
LOG4J = QuestionDoc(
    question_id="q-log4j",
    statement="Web 应用存在 Log4Shell 漏洞，通过 JNDI 注入获取反向 Shell。",
    principles_text="Log4j JNDI 注入加载远程类执行命令",
)
MIXED = QuestionDoc(
    question_id="q-mixed",
    statement="Redis 持久化与 Log4j 日志系统联合排障手册",
    principles_text="Redis 配置与日志分析",
)


class TestScoringRules:
    def test_statement_hit_weights_1(self):
        cands = score_candidates(assessment(["redis"]), [REDIS], {}, SpectatorConfig())
        # 1 keyword × statement(1.0) / (1×1.6) = 0.625
        assert len(cands) == 1
        assert cands[0].question_id == "q-redis"
        assert cands[0].score == pytest.approx(0.625)

    def test_principles_hit_weights_06(self):
        doc = QuestionDoc(
            question_id="q",
            statement="无关题面",
            principles_text="这里讲 rce 横向移动",
        )
        cands = score_candidates(assessment(["rce"]), [doc], {}, SpectatorConfig())
        assert cands[0].score == pytest.approx(0.6 / 1.6)

    def test_case_insensitive_and_chinese_substring(self):
        cands = score_candidates(
            assessment(["REDIS", "未授权访问"]), [REDIS], {}, SpectatorConfig()
        )
        assert cands[0].score == pytest.approx(2.0 / (2 * 1.6))

    def test_mixed_hit_statement_and_principles(self):
        # redis 命中 statement(1.0), "ssh key" 命中 statement(1.0)（含于题面）
        cands = score_candidates(
            assessment(["redis", "ssh key"]), [REDIS], {}, SpectatorConfig()
        )
        assert cands[0].score == pytest.approx(2.0 / (2 * 1.6))

    def test_descending_order_and_topk(self):
        docs = [LOG4J, MIXED, REDIS]
        cands = score_candidates(
            assessment(["redis", "ssh key", "log4j"]), docs, {}, SpectatorConfig()
        )
        scores = [c.score for c in cands]
        assert scores == sorted(scores, reverse=True)
        assert len(cands) <= 3

    def test_threshold_filters_weak(self):
        docs = [LOG4J]
        cands = score_candidates(
            assessment(["redis", "未授权访问", "ssh key"]), docs, {}, SpectatorConfig()
        )
        # log4j 无任何命中 → 0 分 → 低于阈值被剔除
        assert cands == []

    def test_empty_keywords_no_candidates(self):
        assert score_candidates(assessment([]), [REDIS], {}, SpectatorConfig()) == []

    def test_verbose_llm_keywords_do_not_dilute_exact_question_match(self):
        palindrome = QuestionDoc(
            question_id="q-palindrome",
            statement="Write a Python function is_palindrome(s) that ignores case.",
            principles_text="Normalize the string before comparing it.",
        )
        result = score_candidates(
            assessment(
                [
                    "is_palindrome",
                    "回文判断",
                    "Python 字符串处理",
                    "时间复杂度",
                    "空间复杂度",
                    "双指针",
                    "切片反转",
                    "isalnum",
                ]
            ),
            [palindrome],
            {},
            SpectatorConfig(),
        )

        assert [candidate.question_id for candidate in result] == ["q-palindrome"]
        assert result[0].score == pytest.approx(2.0 / (4 * 1.6))

    def test_reason_contains_hit_keywords_no_sensitive(self):
        cands = score_candidates(assessment(["redis"]), [REDIS], {}, SpectatorConfig())
        reason = cands[0].reason
        assert "redis" in reason.lower()
        for banned in ("flag{", "success_criteria", "api_key"):
            assert banned not in reason.lower()

    def test_title_preview_truncated(self):
        long_doc = QuestionDoc(
            question_id="q",
            statement="x" * 300,
            principles_text="",
        )
        cands = score_candidates(assessment(["x"]), [long_doc], {}, SpectatorConfig())
        assert len(cands[0].title_preview) <= 121
        assert cands[0].title_preview.endswith("…")


class TestCooldownEffects:
    def test_rejected_blocked(self):
        cds = {"q-redis": CooldownState(question_id="q-redis", action="rejected")}
        cands = score_candidates(assessment(["redis"]), [REDIS], cds, SpectatorConfig())
        assert cands == []

    def test_ignored_penalty_half(self):
        cds = {"q-redis": CooldownState(question_id="q-redis", action="ignored")}
        cands = score_candidates(assessment(["redis"]), [REDIS], cds, SpectatorConfig())
        assert cands[0].score == pytest.approx(0.625 * 0.5)

    def test_max_candidates_respected(self):
        docs = [
            QuestionDoc(
                question_id=f"q{i}", statement=f"redis 变体{i}", principles_text=""
            )
            for i in range(5)
        ]
        cands = score_candidates(assessment(["redis"]), docs, {}, SpectatorConfig())
        assert len(cands) == 3  # 默认 max_candidates


class TestStaticQuestionSearcher:
    async def test_returns_scripted_docs(self):
        searcher = StaticQuestionSearcher(docs=[REDIS, LOG4J])
        docs = await searcher.usable_questions()
        assert [d.question_id for d in docs] == ["q-redis", "q-log4j"]

    async def test_empty_default(self):
        assert await StaticQuestionSearcher().usable_questions() == []
