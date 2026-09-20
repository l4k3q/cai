# -*- coding: utf-8 -*-
"""T029: 旁观 provider UI 配置（specs/001-spectator-agent 增补）。

TDD Red 阶段：先于实现编写，运行应失败。

语义:
- ProviderConfig: UI 保存的 {api_base, api_key, model}（均可空 = 回退）
- 持久化: JSON 文件（0600 权限, 默认 ~/.cai/spectator_provider.json）
- 优先级: UI(文件) > CAI_SPECTATOR_* env > DEEPSEEK/OPENAI/ANTHROPIC env > 默认
- apply_changes: 字段缺省=不变; 空串=清除; 非空=设置（strip）
- mask_key: 绝不返回明文
"""

import json
import os
import sys

import pytest

from spectator.provider_config import (
    ProviderConfig,
    apply_changes,
    load_provider_config,
    mask_key,
    resolve_provider,
    save_provider_config,
)


class TestProviderConfig:
    def test_defaults_all_none(self):
        cfg = ProviderConfig()
        assert cfg.api_base is None
        assert cfg.api_key is None
        assert cfg.model is None

    def test_strips_whitespace(self):
        cfg = ProviderConfig(api_base="  https://x.example/v1  ", model=" deepseek/x ")
        assert cfg.api_base == "https://x.example/v1"
        assert cfg.model == "deepseek/x"


class TestApplyChanges:
    def test_absent_fields_unchanged(self):
        stored = ProviderConfig(api_base="https://a/v1", model="m1")
        out = apply_changes(stored, api_key="sk-new")
        assert out.api_base == "https://a/v1"
        assert out.api_key == "sk-new"
        assert out.model == "m1"

    def test_empty_string_clears(self):
        stored = ProviderConfig(api_base="https://a/v1", api_key="sk-old", model="m1")
        out = apply_changes(stored, api_base="", api_key="")
        assert out.api_base is None
        assert out.api_key is None
        assert out.model == "m1"

    def test_set_overwrites(self):
        stored = ProviderConfig(api_base="https://a/v1", model="m1")
        out = apply_changes(stored, api_base="https://b/v1", model="m2")
        assert out.api_base == "https://b/v1"
        assert out.model == "m2"

    def test_none_stored_creates(self):
        out = apply_changes(None, api_base="https://a/v1")
        assert out.api_base == "https://a/v1"
        assert out.api_key is None

    def test_whitespace_only_string_clears(self):
        stored = ProviderConfig(api_base="https://a/v1")
        out = apply_changes(stored, api_base="   ")
        assert out.api_base is None


class TestPersistence:
    def test_save_load_roundtrip(self, tmp_path):
        path = tmp_path / "provider.json"
        cfg = ProviderConfig(api_base="https://a/v1", api_key="sk-123", model="m")
        save_provider_config(path, cfg)
        assert path.exists()
        loaded = load_provider_config(path)
        assert loaded == cfg

    def test_load_missing_returns_none(self, tmp_path):
        assert load_provider_config(tmp_path / "nope.json") is None

    def test_load_corrupt_returns_none(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        assert load_provider_config(path) is None

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Windows 无 POSIX 权限位（由 ACL 管理文件访问）",
    )
    def test_saved_file_not_world_readable(self, tmp_path):
        path = tmp_path / "provider.json"
        save_provider_config(path, ProviderConfig(api_key="sk-x"))
        mode = os.stat(path).st_mode & 0o777
        assert mode & 0o077 == 0  # group/other 无任何权限（0600）


class TestResolveProvider:
    ENV = {
        "CAI_SPECTATOR_API_BASE": "https://env-spectator/v1",
        "CAI_SPECTATOR_API_KEY": "sk-env-spectator",
        "CAI_SPECTATOR_MODEL": "env/spectator-model",
        "DEEPSEEK_API_BASE": "https://env-deepseek/v1",
        "DEEPSEEK_API_KEY": "sk-env-deepseek",
        "CAI_MODEL": "env/cai-model",
    }

    def test_ui_overrides_env(self):
        stored = ProviderConfig(
            api_base="https://ui/v1", api_key="sk-ui", model="ui/model"
        )
        r = resolve_provider(self.ENV, stored)
        assert r.api_base == "https://ui/v1"
        assert r.api_key == "sk-ui"
        assert r.model == "ui/model"

    def test_env_when_no_ui(self):
        r = resolve_provider(self.ENV, None)
        assert r.api_base == "https://env-spectator/v1"
        assert r.api_key == "sk-env-spectator"
        assert r.model == "env/spectator-model"

    def test_partial_ui_falls_back_per_field(self):
        stored = ProviderConfig(api_base="https://ui/v1")  # key/model 走 env
        r = resolve_provider(self.ENV, stored)
        assert r.api_base == "https://ui/v1"
        assert r.api_key == "sk-env-spectator"

    def test_env_chain_fallback(self):
        env = {"DEEPSEEK_API_BASE": "https://d/v1", "DEEPSEEK_API_KEY": "sk-d"}
        r = resolve_provider(env, None)
        assert r.api_base == "https://d/v1"
        assert r.api_key == "sk-d"
        assert r.model == "deepseek/deepseek-v4-flash"  # 最终兜底

    def test_all_empty_returns_nones_with_default_model(self):
        r = resolve_provider({}, None)
        assert r.api_base is None
        assert r.api_key is None
        assert r.model == "deepseek/deepseek-v4-flash"


class TestMaskKey:
    def test_none(self):
        assert mask_key(None) is None

    def test_short_key_fully_masked(self):
        assert mask_key("sk-1") == "***"

    def test_long_key_keeps_ends(self):
        m = mask_key("sk-tr-abcdefgh1234")
        assert m.startswith("sk-")
        assert m.endswith("1234")
        assert "abcdefgh" not in m
        assert "***" in m

    def test_mask_never_contains_middle(self):
        key = "sk-SECRETSECRETSECRET"
        m = mask_key(key)
        assert "SECRETSECRET" not in m
