"""Dependency-free helpers shared by model-backed subsystems."""

from __future__ import annotations

from typing import Any


def normalize_model_id(model: Any) -> Any:
    """Add the LiteLLM provider prefix for known bare model identifiers."""
    if not isinstance(model, str):
        return model
    model = model.strip()
    if not model or "/" in model:
        return model
    lowered = model.lower()
    prefixes = (
        (("claude-", "anthropic-"), "anthropic"),
        (("deepseek-",), "deepseek"),
        (("gpt-", "o1", "o3", "o4"), "openai"),
        (("gemini-",), "gemini"),
        (("mistral-",), "mistral"),
    )
    for markers, provider in prefixes:
        if lowered.startswith(markers):
            return f"{provider}/{model}"
    return model
