"""严格、幂等地导入仅含召回元数据的题库种子。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .fingerprints import canonicalize_statement, compute_instance_fingerprint
from .models import Question

SEED_FIELDS = frozenset({"title", "aliases", "keywords", "recall_queries"})


@dataclass(frozen=True)
class RecallTopic:
    title: str
    aliases: tuple[str, ...]
    keywords: tuple[str, ...]
    recall_queries: tuple[str, ...]

    def as_metadata(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "aliases": list(self.aliases),
            "keywords": list(self.keywords),
            "recall_queries": list(self.recall_queries),
        }


@dataclass(frozen=True)
class ImportSummary:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0

    @property
    def total(self) -> int:
        return self.inserted + self.updated + self.unchanged

    def as_dict(self) -> dict[str, int]:
        return {
            "inserted": self.inserted,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "total": self.total,
        }


def _string_list(value: object, field: str, index: int) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError(f"seed item {index} field {field!r} must be a non-empty string list")
    return tuple(item.strip() for item in value)


def parse_topic(raw: object, index: int) -> RecallTopic:
    if not isinstance(raw, Mapping):
        raise ValueError(f"seed item {index} must be an object")
    if frozenset(raw) != SEED_FIELDS:
        raise ValueError(
            f"seed item {index} must contain only {sorted(SEED_FIELDS)}"
        )
    title = raw["title"]
    if not isinstance(title, str) or not title.strip():
        raise ValueError(f"seed item {index} field 'title' must be a non-empty string")
    return RecallTopic(
        title=title.strip(),
        aliases=_string_list(raw["aliases"], "aliases", index),
        keywords=_string_list(raw["keywords"], "keywords", index),
        recall_queries=_string_list(raw["recall_queries"], "recall_queries", index),
    )


def load_recall_seed(path: str | Path) -> list[RecallTopic]:
    """读取并校验种子；拒绝混入解法或其他字段。"""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not payload:
        raise ValueError("recall seed must be a non-empty JSON array")
    topics = [parse_topic(item, index) for index, item in enumerate(payload)]
    fingerprints = [
        compute_instance_fingerprint(topic.title) for topic in topics
    ]
    if len(set(fingerprints)) != len(fingerprints):
        raise ValueError("recall seed contains duplicate titles")
    return topics


async def import_recall_topics(repo, sess, topics: Iterable[RecallTopic]) -> ImportSummary:
    """在调用方事务中导入题目，不创建材料、构建任务或解法。"""
    inserted = updated = unchanged = 0
    for topic in topics:
        metadata = topic.as_metadata()
        canonical = canonicalize_statement(topic.title)
        fingerprint = compute_instance_fingerprint(topic.title)
        question = await repo.get_question_by_fingerprint(sess, fingerprint)
        if question is None:
            question = Question(
                statement_raw=topic.title,
                statement_canonical=canonical,
                instance_fingerprint=fingerprint,
                success_criteria={},
                recall_metadata=metadata,
                status="incomplete",
            )
            sess.add(question)
            await sess.flush()
            inserted += 1
            continue

        changed = question.recall_metadata != metadata
        question.recall_metadata = metadata
        has_solution = await repo.has_usable_solution(sess, question.question_id)
        if not has_solution and question.status != "incomplete":
            question.status = "incomplete"
            changed = True
        if changed:
            updated += 1
        else:
            unchanged += 1

    return ImportSummary(inserted=inserted, updated=updated, unchanged=unchanged)
