# -*- coding: utf-8 -*-
"""题目匹配: QuestionSearcher Protocol + score_candidates 评分器（契约 module.md §5）。

评分规则（research.md D5, 确定性 v1）:
- 最多使用模型返回的前 4 个关键词，避免冗长关键词列表稀释有效命中
- 每个关键词在 statement 命中权重 1.0、principles_text 命中 0.6
  recall_text（题库召回元数据）命中权重 1.0
  （子串匹配, 大小写不敏感, 中文精确子串; 混合短语同时匹配 ASCII 技术词）
- 题目得分 = Σ(命中关键词权重) / (关键词数 × 1.6) 归一到 [0,1]
- cooldown: rejected/accepted 屏蔽; ignored ×0.5
- 过滤: 得分 < match_threshold 剔除; 取前 max_candidates 按分数降序

匹配依据仅题面 + 解法原理文本（宪法 V: 不用命令/flag/漏洞细节反推）。
"""

from __future__ import annotations

import re
from typing import Dict, List, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from .config import SpectatorConfig
from .core import CooldownPolicy
from .models import (
    CooldownState,
    IntentAssessment,
    QuestionDoc,
    RecommendationCandidate,
)

_STATEMENT_WEIGHT = 1.0
_PRINCIPLES_WEIGHT = 0.6
_NORMALIZER = 1.6  # 单关键词全命中的理论峰值 = 1.0/1.6 = 0.625
_MAX_SCORING_KEYWORDS = 4
_ASCII_TERM_RE = re.compile(r"[a-z0-9_+#.-]{2,}", re.IGNORECASE)


@runtime_checkable
class QuestionSearcher(Protocol):
    """题库检索协议: 返回全部 usable 题目的轻量投影。"""

    async def usable_questions(self) -> List[QuestionDoc]: ...

    async def recall_test_questions(self) -> List[QuestionDoc]: ...


class StaticQuestionSearcher(BaseModel):
    """内存检索器（测试/演示）; 真实 PG 实现方在集成层注入。"""

    docs: List[QuestionDoc] = Field(default_factory=list)

    async def usable_questions(self) -> List[QuestionDoc]:
        return [doc for doc in self.docs if doc.learnable and not doc.disabled]

    async def recall_test_questions(self) -> List[QuestionDoc]:
        return list(self.docs)


def score_candidates(
    assessment: IntentAssessment,
    questions: List[QuestionDoc],
    cooldowns: Dict[str, CooldownState],
    config: SpectatorConfig,
) -> List[RecommendationCandidate]:
    """纯函数: 评估结果 + 题目投影 + 冷却 → 排序候选列表。"""
    policy = CooldownPolicy()
    keywords = [kw.lower() for kw in assessment.keywords[:_MAX_SCORING_KEYWORDS]]
    if not keywords:
        return []

    scored: List[RecommendationCandidate] = []
    for doc in questions:
        if policy.is_blocked(doc.question_id, cooldowns):
            continue
        hit_keywords: List[str] = []
        weight_sum = 0.0
        for kw in keywords:
            weight = _hit_weight(kw, doc)
            if weight > 0:
                weight_sum += weight
                hit_keywords.append(kw)
        if not hit_keywords:
            continue
        raw_score = weight_sum / (len(keywords) * _NORMALIZER)
        raw_score = min(raw_score, 1.0)
        score = raw_score * policy.penalty(doc.question_id, cooldowns)
        if score < config.match_threshold:
            continue
        scored.append(
            RecommendationCandidate(
                question_id=doc.question_id,
                title_preview=RecommendationCandidate.make_preview(doc.statement),
                score=round(score, 4),
                reason=_build_reason(hit_keywords),
                learnable=doc.learnable,
                disabled=doc.disabled,
                disabled_reason=doc.disabled_reason,
            )
        )

    scored.sort(key=lambda c: c.score, reverse=True)
    return scored[: config.max_candidates]


def _hit_weight(keyword: str, doc: QuestionDoc) -> float:
    """关键词对一道题的命中权重; 未命中 0。"""
    statement = doc.statement.lower()
    principles = doc.principles_text.lower()
    recall_text = doc.recall_text.lower()
    terms = [keyword]
    terms.extend(term for term in _ASCII_TERM_RE.findall(keyword) if term != keyword)
    if any(term in statement for term in terms):
        return _STATEMENT_WEIGHT
    if any(term in principles for term in terms):
        return _PRINCIPLES_WEIGHT
    if any(term in recall_text for term in terms):
        return _STATEMENT_WEIGHT
    return 0.0


def _build_reason(hit_keywords: List[str]) -> str:
    shown = hit_keywords[:5]
    return "命中知识点：" + "、".join(shown)
