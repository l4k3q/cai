# -*- coding: utf-8 -*-
"""召回测试模式的隔离、候选标识和不可学习拒绝。"""

import pytest

from spectator.config import SpectatorConfig
from spectator.models import IntentAssessment, QuestionDoc
from spectator.service import CardStateError, SpectatorService
from spectator.tests.fakes import FakeIntentEvaluator, FakeQuestionSearcher


WINDOW = [("user", "请解释 SSRF 风险")]
USABLE = QuestionDoc(
    question_id="q-usable",
    statement="SSRF 风险基础题",
    recall_text="SSRF 服务端请求伪造",
)
INCOMPLETE = QuestionDoc(
    question_id="q-incomplete",
    statement="SSRF 召回测试题",
    recall_text="SSRF 服务端请求伪造",
    learnable=False,
    disabled=True,
    disabled_reason="仅召回测试：尚无 usable 解法",
)
ASSESSMENT = IntentAssessment(
    intent="knowledge_seeking",
    confidence=0.95,
    topic_summary="SSRF",
    keywords=["SSRF"],
)


def make_recall_service(recall_test_mode: bool):
    config = SpectatorConfig(enabled=True, recall_test_mode=recall_test_mode)
    searcher = FakeQuestionSearcher(
        docs=[USABLE], recall_docs=[USABLE, INCOMPLETE]
    )
    service = SpectatorService(
        config=config,
        evaluator=FakeIntentEvaluator(ASSESSMENT),
        searcher=searcher,
        logger=__import__("spectator.logging_utils", fromlist=["SpectatorLogger"]).SpectatorLogger(
            log_dir="/nonexistent-recall-test", filename="recall.jsonl"
        ),
    )
    return service, searcher


@pytest.mark.asyncio
async def test_default_mode_isolated_to_usable_search():
    service, searcher = make_recall_service(False)

    card = await service.observe_round("session", WINDOW, False, 3)

    assert card is not None
    assert [candidate.question_id for candidate in card.candidates] == ["q-usable"]
    assert searcher.calls == 1
    assert searcher.recall_calls == 0


@pytest.mark.asyncio
async def test_recall_test_mode_returns_disabled_incomplete_candidate():
    service, searcher = make_recall_service(True)

    card = await service.observe_round("session", WINDOW, False, 3)

    assert card is not None
    candidate = next(
        item for item in card.candidates if item.question_id == "q-incomplete"
    )
    assert candidate.disabled is True
    assert candidate.learnable is False
    assert "usable" in candidate.disabled_reason
    assert searcher.calls == 0
    assert searcher.recall_calls == 1

    with pytest.raises(CardStateError, match="not learnable"):
        await service.accept("session", card.card_id, "q-incomplete")
    assert card.status.value == "active"
