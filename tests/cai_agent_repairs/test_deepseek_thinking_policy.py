from types import SimpleNamespace

from cai.sdk.agents.models.chatcompletions.litellm_adapter import (
    apply_deepseek_thinking_policy,
)


def _apply(agent_type, effort=None, model="deepseek/deepseek-chat"):
    settings = SimpleNamespace(reasoning_effort=effort)
    return apply_deepseek_thinking_policy(
        {"model": model},
        model_name=model,
        model_settings=settings,
        agent_name=agent_type,
        agent_type=agent_type,
    )


def test_disables_thinking_for_main_orchestration_agent(monkeypatch):
    monkeypatch.delenv("CAI_DEEPSEEK_THINKING", raising=False)

    kwargs = _apply("orchestration_agent")

    assert kwargs["reasoning_effort"] == "none"
    assert kwargs["extra_body"]["thinking"] == {"type": "disabled"}


def test_enables_thinking_for_specialist_solving_agent(monkeypatch):
    monkeypatch.delenv("CAI_DEEPSEEK_THINKING", raising=False)

    kwargs = _apply("web_pentester_agent")

    assert kwargs["reasoning_effort"] == "high"
    assert kwargs["extra_body"]["thinking"] == {"type": "enabled"}


def test_explicit_model_setting_overrides_agent_role(monkeypatch):
    monkeypatch.delenv("CAI_DEEPSEEK_THINKING", raising=False)

    kwargs = _apply("web_pentester_agent", effort="none")

    assert kwargs["reasoning_effort"] == "none"
    assert kwargs["extra_body"]["thinking"] == {"type": "disabled"}


def test_does_not_change_non_deepseek_request(monkeypatch):
    monkeypatch.delenv("CAI_DEEPSEEK_THINKING", raising=False)

    kwargs = _apply("orchestration_agent", model="openai/gpt-4o")

    assert "reasoning_effort" not in kwargs
    assert "extra_body" not in kwargs
