# -*- coding: utf-8 -*-
"""T010: IntentEvaluator——LLM 实现方（注入 fake completion）+ Fake（契约 module.md §4）。

TDD Red 阶段：先于实现编写，运行应失败。
"""

import asyncio
import sys
from types import SimpleNamespace

import pytest

from spectator.evaluator import EvaluationError, LLMIntentEvaluator, make_litellm_completer
from spectator.models import IntentAssessment
from spectator.tests.fakes import FakeIntentEvaluator


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


WINDOW = [
    ("user", "redis 未授权访问是什么原理"),
    ("assistant", "Redis 未授权访问指未设密码…"),
    ("user", "怎么利用它写 ssh key"),
]

VALID_JSON = (
    '{"intent": "knowledge_seeking", "confidence": 0.82, '
    '"topic_summary": "Redis 未授权访问利用", '
    '"keywords": ["redis", "未授权访问", "ssh key"]}'
)


class TestLLMIntentEvaluator:
    def _evaluator(self, completer):
        return LLMIntentEvaluator(completer=completer)

    def test_valid_json_parsed(self):
        ev = self._evaluator(lambda messages, **kw: VALID_JSON)
        result = _run(ev.assess(WINDOW))
        assert isinstance(result, IntentAssessment)
        assert result.intent == "knowledge_seeking"
        assert result.confidence == 0.82
        assert result.keywords == ["redis", "未授权访问", "ssh key"]

    def test_prompt_contains_window_and_json_instruction(self):
        captured = {}

        def completer(messages, **kw):
            captured["messages"] = messages
            return VALID_JSON

        _run(self._evaluator(completer).assess(WINDOW))
        prompt = json_text = "".join(m.get("content", "") for m in captured["messages"])
        assert "redis 未授权访问是什么原理" in prompt
        assert "怎么利用它写 ssh key" in prompt
        assert "json" in json_text.lower()

    def test_invalid_json_raises_evaluation_error(self):
        ev = self._evaluator(lambda messages, **kw: "not json at all {{{")
        with pytest.raises(EvaluationError):
            _run(ev.assess(WINDOW))

    def test_out_of_enum_intent_raises(self):
        bad = '{"intent": "gossip", "confidence": 0.9, "topic_summary": "", "keywords": []}'
        ev = self._evaluator(lambda messages, **kw: bad)
        with pytest.raises(EvaluationError):
            _run(ev.assess(WINDOW))

    def test_completer_exception_wrapped(self):
        def boom(messages, **kw):
            raise TimeoutError("gateway 503")

        ev = self._evaluator(boom)
        with pytest.raises(EvaluationError):
            _run(ev.assess(WINDOW))

    def test_json_in_code_fence_extracted(self):
        fenced = "```json\n" + VALID_JSON + "\n```"
        ev = self._evaluator(lambda messages, **kw: fenced)
        result = _run(ev.assess(WINDOW))
        assert result.intent == "knowledge_seeking"


class TestFakeIntentEvaluator:
    def test_returns_scripted_assessment(self):
        fake = FakeIntentEvaluator(
            IntentAssessment(
                intent="chitchat", confidence=0.9, topic_summary="闲聊", keywords=[]
            )
        )
        result = _run(fake.assess(WINDOW))
        assert result.intent == "chitchat"
        assert fake.calls == 1
        assert fake.last_transcript == WINDOW

    def test_can_raise_scripted_error(self):
        fake = FakeIntentEvaluator(error=EvaluationError("boom"))
        with pytest.raises(EvaluationError):
            _run(fake.assess(WINDOW))


class TestLiteLLMCompleter:
    @pytest.mark.parametrize(
        ("configured_model", "expected_model"),
        [
            ("deepseek-flash", "deepseek/deepseek-flash"),
            ("deepseek/deepseek-flash", "deepseek/deepseek-flash"),
        ],
    )
    def test_normalizes_known_bare_model_without_double_prefix(
        self, monkeypatch, configured_model, expected_model
    ):
        captured = {}

        async def acompletion(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))]
            )

        monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=acompletion))
        completer = make_litellm_completer(
            configured_model,
            api_base="https://api.deepseek.com",
            api_key="sk-test",
        )

        assert _run(completer([{"role": "user", "content": "test"}])) == "{}"
        assert captured["model"] == expected_model
        assert captured["api_base"] == "https://api.deepseek.com"

    def test_disables_deepseek_thinking_by_default(self, monkeypatch):
        captured = {}

        async def acompletion(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))]
            )

        monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=acompletion))
        completer = make_litellm_completer(
            "deepseek-chat", api_base="https://api.deepseek.com", api_key="sk-test"
        )

        _run(completer([{"role": "user", "content": "test"}]))

        assert captured["extra_body"] == {"thinking": {"type": "disabled"}}
        assert captured["reasoning_effort"] == "none"

    def test_does_not_add_deepseek_thinking_options_to_other_models(self, monkeypatch):
        captured = {}

        async def acompletion(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))]
            )

        monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=acompletion))
        completer = make_litellm_completer(
            "gpt-4o", api_base="https://api.openai.com/v1", api_key="sk-test"
        )

        _run(completer([{"role": "user", "content": "test"}]))

        assert "extra_body" not in captured
        assert "reasoning_effort" not in captured

    def test_call_kwargs_override_deepseek_thinking_defaults(self, monkeypatch):
        captured = {}

        async def acompletion(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))]
            )

        monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=acompletion))
        completer = make_litellm_completer(
            "deepseek-chat", api_base="https://api.deepseek.com", api_key="sk-test"
        )

        _run(
            completer(
                [{"role": "user", "content": "test"}],
                extra_body={"thinking": {"type": "enabled"}},
                reasoning_effort="high",
            )
        )

        assert captured["extra_body"] == {"thinking": {"type": "enabled"}}
        assert captured["reasoning_effort"] == "high"
