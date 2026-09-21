"""Small, dependency-free helpers for keeping secrets out of the question bank."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Iterable

try:
    from cai.model_utils import normalize_model_id
except ModuleNotFoundError:
    from model_utils import normalize_model_id


_SAFE_RUNTIME_FIELDS = frozenset({"agent", "model", "base_url"})
_SENSITIVE_NAME_RE = re.compile(
    r"(?:^|[_\-. ])(?:api[_\-. ]?key|access[_\-. ]?key|secret|token|password|authorization)(?:$|[_\-. ])",
    re.IGNORECASE,
)


def is_sensitive_name(name: object) -> bool:
    """Return whether a mapping key normally carries a credential."""
    if not isinstance(name, str):
        return False
    normalized = name.strip().lower()
    return bool(_SENSITIVE_NAME_RE.search(normalized)) or normalized in {
        "apikey",
        "accesskey",
        "secretkey",
        "privatekey",
    }


def _replace_known_secrets(value: str, secrets: Iterable[str]) -> str:
    result = value
    for secret in sorted(
        {item for item in secrets if isinstance(item, str) and item},
        key=len,
        reverse=True,
    ):
        result = result.replace(secret, "[REDACTED]")
    return result


def redact_value(value: Any, secrets: Iterable[str] = ()) -> Any:
    """Recursively redact credential-shaped fields and known secret values."""
    secret_values = tuple(secrets)
    if isinstance(value, Mapping):
        return {
            key: "[REDACTED]" if is_sensitive_name(key) else redact_value(item, secret_values)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item, secret_values) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item, secret_values) for item in value)
    if isinstance(value, str):
        return _replace_known_secrets(value, secret_values)
    return value


def redact_text(value: object, secrets: Iterable[str] = ()) -> str:
    """Return safe text for error fields and log messages."""
    return str(redact_value(str(value), secrets))


def collect_secret_values(value: Any) -> tuple[str, ...]:
    """Collect values from credential-shaped mapping fields for text scrubbing."""
    found: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                if is_sensitive_name(key) and isinstance(child, str) and child:
                    found.append(child)
                else:
                    visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)
    return tuple(found)


def sanitize_runtime_config(value: Any) -> dict[str, Any]:
    """Return the non-secret runtime settings safe for API/log output."""
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    for key in _SAFE_RUNTIME_FIELDS:
        item = value.get(key)
        if item is not None and item != "":
            result[key] = item
    return result


def runtime_config_for_execution(value: Any) -> dict[str, Any]:
    """Copy a stored config without dropping ``api_key`` needed by the runner."""
    if not isinstance(value, Mapping):
        return {}
    result = dict(value)
    if "model" in result:
        result["model"] = normalize_model_id(result["model"])
    return result
