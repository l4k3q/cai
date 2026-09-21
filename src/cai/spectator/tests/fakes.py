# -*- coding: utf-8 -*-
"""测试用 fakes: FakeIntentEvaluator / FakeQuestionSearcher（宪法 III 可替换依赖）。"""

from __future__ import annotations

from typing import List, Optional, Tuple

from spectator.evaluator import EvaluationError
from spectator.matcher import QuestionSearcher
from spectator.models import IntentAssessment, QuestionDoc

ConversationTurn = Tuple[str, str]


class FakeIntentEvaluator:
    """脚本化意图评估器: 返回预设结果或抛出预设错误。"""

    def __init__(
        self,
        scripted: Optional[IntentAssessment] = None,
        error: Optional[Exception] = None,
        transient_failures: int = 0,
    ) -> None:
        self._scripted = scripted or IntentAssessment(
            intent="unclear", confidence=0.5, topic_summary="", keywords=[]
        )
        self._error = error
        self._transient_failures = transient_failures
        self.calls = 0
        self.last_transcript: List[ConversationTurn] = []

    async def assess(self, transcript: List[ConversationTurn]) -> IntentAssessment:
        self.calls += 1
        self.last_transcript = list(transcript)
        if self.calls <= self._transient_failures:
            raise EvaluationError("temporary gateway failure", retryable=True)
        if self._error is not None:
            raise self._error
        return self._scripted


class FakeQuestionSearcher:
    """脚本化题目检索器: 返回预设文档或抛出错误。"""

    def __init__(
        self,
        docs: Optional[List[QuestionDoc]] = None,
        error: Optional[Exception] = None,
        recall_docs: Optional[List[QuestionDoc]] = None,
    ) -> None:
        self._docs = docs or []
        self._recall_docs = list(self._docs if recall_docs is None else recall_docs)
        self._error = error
        self.calls = 0
        self.recall_calls = 0

    async def usable_questions(self) -> List[QuestionDoc]:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return list(self._docs)

    async def recall_test_questions(self) -> List[QuestionDoc]:
        self.recall_calls += 1
        if self._error is not None:
            raise self._error
        return list(self._recall_docs)


# QuestionSearcher Protocol 运行时兼容性（结构化协议, 显式声明便于阅读）
if False:  # pragma: no cover
    _q: QuestionSearcher = FakeQuestionSearcher()  # type: ignore[assignment]


# 避免FakeIntentEvaluator引用循环导入的真实基类
_ = EvaluationError
