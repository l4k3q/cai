# -*- coding: utf-8 -*-
"""T003: SpectatorConfig 环境变量解析与校验（契约 module.md §1）。

TDD Red 阶段：先于实现编写，运行应失败。
"""

import pytest

from spectator.config import SpectatorConfig


class TestDefaults:
    def test_defaults(self):
        cfg = SpectatorConfig()
        assert cfg.enabled is False
        assert cfg.interval_rounds == 1
        assert cfg.eval_timeout_seconds == 10.0
        assert cfg.eval_retry_attempts == 3
        assert cfg.eval_retry_backoff_seconds == 0.5
        assert cfg.max_candidates == 3
        assert cfg.min_intent_confidence == 0.6
        assert cfg.match_threshold == 0.15
        assert cfg.window_messages == 6
        assert cfg.model is None
        assert cfg.recall_test_mode is False


class TestFromEnv:
    def test_from_env_reads_overrides(self, monkeypatch):
        monkeypatch.setenv("CAI_SPECTATOR_ENABLED", "true")
        monkeypatch.setenv("CAI_SPECTATOR_INTERVAL_ROUNDS", "5")
        monkeypatch.setenv("CAI_SPECTATOR_EVAL_TIMEOUT_SECONDS", "7.5")
        monkeypatch.setenv("CAI_SPECTATOR_EVAL_RETRY_ATTEMPTS", "4")
        monkeypatch.setenv("CAI_SPECTATOR_EVAL_RETRY_BACKOFF_SECONDS", "0.25")
        monkeypatch.setenv("CAI_SPECTATOR_MAX_CANDIDATES", "2")
        monkeypatch.setenv("CAI_SPECTATOR_MIN_INTENT_CONFIDENCE", "0.8")
        monkeypatch.setenv("CAI_SPECTATOR_MATCH_THRESHOLD", "0.3")
        monkeypatch.setenv("CAI_SPECTATOR_WINDOW_MESSAGES", "5")
        monkeypatch.setenv("CAI_SPECTATOR_WINDOW_ROUNDS", "4")
        monkeypatch.setenv("CAI_SPECTATOR_MODEL", "deepseek/deepseek-v4-flash")
        monkeypatch.setenv("CAI_SPECTATOR_RECALL_TEST_MODE", "true")
        cfg = SpectatorConfig.from_env()
        assert cfg.enabled is True
        assert cfg.interval_rounds == 5
        assert cfg.eval_timeout_seconds == 7.5
        assert cfg.eval_retry_attempts == 4
        assert cfg.eval_retry_backoff_seconds == 0.25
        assert cfg.max_candidates == 2
        assert cfg.min_intent_confidence == 0.8
        assert cfg.match_threshold == 0.3
        assert cfg.window_messages == 5
        assert cfg.model == "deepseek/deepseek-v4-flash"
        assert cfg.recall_test_mode is True

    def test_from_env_defaults_when_unset(self, monkeypatch):
        for name in (
            "CAI_SPECTATOR_ENABLED",
            "CAI_SPECTATOR_INTERVAL_ROUNDS",
            "CAI_SPECTATOR_EVAL_TIMEOUT_SECONDS",
            "CAI_SPECTATOR_MAX_CANDIDATES",
            "CAI_SPECTATOR_MIN_INTENT_CONFIDENCE",
            "CAI_SPECTATOR_MATCH_THRESHOLD",
            "CAI_SPECTATOR_WINDOW_MESSAGES",
            "CAI_SPECTATOR_WINDOW_ROUNDS",
            "CAI_SPECTATOR_MODEL",
        ):
            monkeypatch.delenv(name, raising=False)
        cfg = SpectatorConfig.from_env()
        assert cfg.enabled is False
        assert cfg.interval_rounds == 1
        assert cfg.window_messages == 6

    def test_from_env_compatibly_converts_legacy_window_rounds(self, monkeypatch):
        monkeypatch.delenv("CAI_SPECTATOR_WINDOW_MESSAGES", raising=False)
        monkeypatch.setenv("CAI_SPECTATOR_WINDOW_ROUNDS", "4")

        cfg = SpectatorConfig.from_env()

        assert cfg.window_messages == 8

    def test_from_env_bool_variants(self, monkeypatch):
        monkeypatch.setenv("CAI_SPECTATOR_ENABLED", "1")
        assert SpectatorConfig.from_env().enabled is True
        monkeypatch.setenv("CAI_SPECTATOR_ENABLED", "false")
        assert SpectatorConfig.from_env().enabled is False
        monkeypatch.setenv("CAI_SPECTATOR_ENABLED", "0")
        assert SpectatorConfig.from_env().enabled is False
        monkeypatch.setenv("CAI_SPECTATOR_ENABLED", "yes")
        assert SpectatorConfig.from_env().enabled is True


class TestValidation:
    def test_interval_rounds_must_be_positive(self):
        with pytest.raises(Exception):
            SpectatorConfig(interval_rounds=0)

    def test_max_candidates_range(self):
        with pytest.raises(Exception):
            SpectatorConfig(max_candidates=0)
        with pytest.raises(Exception):
            SpectatorConfig(max_candidates=6)

    def test_thresholds_range(self):
        with pytest.raises(Exception):
            SpectatorConfig(min_intent_confidence=1.5)
        with pytest.raises(Exception):
            SpectatorConfig(match_threshold=-0.1)

    def test_timeout_positive(self):
        with pytest.raises(Exception):
            SpectatorConfig(eval_timeout_seconds=0)

    def test_window_messages_positive(self):
        with pytest.raises(Exception):
            SpectatorConfig(window_messages=0)
