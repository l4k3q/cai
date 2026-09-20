# -*- coding: utf-8 -*-
"""召回种子结构与幂等导入专项测试。"""

import json
from pathlib import Path

import pytest

from question_bank.recall_import import import_recall_topics, load_recall_seed


SEED = Path(__file__).resolve().parents[2] / "seeds" / "security_recall_topics.json"


class FakeSession:
    def __init__(self, repository):
        self.repository = repository

    def add(self, question):
        self.repository.questions[question.instance_fingerprint] = question

    async def flush(self):
        return None


class FakeRepository:
    def __init__(self):
        self.questions = {}

    async def get_question_by_fingerprint(self, _sess, fingerprint):
        return self.questions.get(fingerprint)

    async def has_usable_solution(self, _sess, _question_id):
        return False


def test_seed_has_exactly_twenty_metadata_only_topics():
    topics = load_recall_seed(SEED)

    assert len(topics) == 20
    assert all(set(topic.as_metadata()) == {
        "title", "aliases", "keywords", "recall_queries"
    } for topic in topics)


def test_seed_rejects_solution_fields(tmp_path):
    path = tmp_path / "invalid.json"
    path.write_text(
        json.dumps([{
            "title": "test",
            "aliases": ["test"],
            "keywords": ["test"],
            "recall_queries": ["test"],
            "solution": "must not be present",
        }]),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="only"):
        load_recall_seed(path)


@pytest.mark.asyncio
async def test_import_is_idempotent_and_keeps_questions_incomplete():
    topics = load_recall_seed(SEED)
    repository = FakeRepository()
    session = FakeSession(repository)

    first = await import_recall_topics(repository, session, topics)
    second = await import_recall_topics(repository, session, topics)

    assert first.as_dict() == {"inserted": 20, "updated": 0, "unchanged": 0, "total": 20}
    assert second.as_dict() == {"inserted": 0, "updated": 0, "unchanged": 20, "total": 20}
    assert len(repository.questions) == 20
    assert all(question.status == "incomplete" for question in repository.questions.values())
    assert all(not question.solution_methods for question in repository.questions.values())
