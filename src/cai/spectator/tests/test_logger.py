# -*- coding: utf-8 -*-
"""T007: JSONL 结构化日志（data-model.md §5）。

TDD Red 阶段：先于实现编写，运行应失败。
"""

import json

import pytest

from spectator.logging_utils import SpectatorLogger


@pytest.fixture()
def log_path(tmp_path):
    return tmp_path / "spectator_test.jsonl"


def read_lines(path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class TestJsonlWriting:
    def test_append_events(self, log_path):
        logger = SpectatorLogger(log_dir=str(log_path.parent), filename=log_path.name)
        logger.log("evaluation", "s1", 6, {"intent": "chitchat"})
        logger.log("recommendation", "s1", 6, {"card_id": "c1"})
        lines = read_lines(log_path)
        assert len(lines) == 2
        assert lines[0]["event"] == "evaluation"
        assert lines[0]["session_id"] == "s1"
        assert lines[0]["round"] == 6
        assert "timestamp" in lines[0]
        assert lines[0]["payload"]["intent"] == "chitchat"
        assert lines[1]["event"] == "recommendation"

    def test_invalid_event_name_rejected(self, log_path):
        logger = SpectatorLogger(log_dir=str(log_path.parent), filename=log_path.name)
        with pytest.raises(Exception):
            logger.log("bogus_event", "s1", 1, {})

    def test_write_failure_is_silent(self):
        # 坏目录: 写失败不得抛出（日志失败不反噬主流程）
        logger = SpectatorLogger(log_dir="Z:/nonexistent/dir/xyz", filename="x.jsonl")
        logger.log("evaluation", "s1", 3, {})  # 不应抛出

    def test_sensitive_keys_not_required_but_never_special_cased(self, log_path):
        # 日志层不主动加入敏感字段; 断言行内不含凭据类键
        logger = SpectatorLogger(log_dir=str(log_path.parent), filename=log_path.name)
        logger.log("evaluation", "s1", 3, {"intent": "knowledge_seeking"})
        line = read_lines(log_path)[0]
        flat = json.dumps(line)
        for banned in ("api_key", "flag{", "success_criteria"):
            assert banned not in flat
