# -*- coding: utf-8 -*-
"""旁观 agent 的 provider UI 配置（specs/001-spectator-agent T029/T033）。

手动配置模式（T033, 用户要求"所有 api 和 baseurl 都需要手动填入"）:
webui「API 设置」窗口是**唯一**配置来源——不回退 .env、不借用会话
provider、不读取任何环境变量。BASE URL、API 密钥、模型三项全部必填;
任一缺失时评估不运行（集成层记 degraded: provider_not_configured）。

字段三态语义（POST body）: 缺省=不变; 空串=清除; 非空=设置（strip）。

安全（宪法）: 密钥绝不回显明文（mask_key）、绝不写日志/SSE; 文件 0600。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, List, Optional

from pydantic import BaseModel, field_validator

DEFAULT_PROVIDER_FILE = Path.home() / ".cai" / "spectator_provider.json"

_REQUIRED_FIELDS = ("api_base", "api_key", "model")

_UNSET: Any = object()


class ProviderConfig(BaseModel):
    """UI 保存的 provider 配置; None = 该字段未配置（手动模式: 视为缺失）。"""

    api_base: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None

    @field_validator("api_base", "api_key", "model", mode="before")
    @classmethod
    def _strip(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip()
            return v or None
        return v

    def missing_fields(self) -> List[str]:
        """缺失的必填字段名（用于 API 回显与降级日志）。"""
        return [name for name in _REQUIRED_FIELDS if not getattr(self, name)]


def is_complete(cfg: Optional[ProviderConfig]) -> bool:
    """三项全部填写才算配置完整——评估运行的前提（T033）。"""
    return cfg is not None and not cfg.missing_fields()


def resolve_provider(stored: Optional[ProviderConfig]) -> ProviderConfig:
    """解析配置。T033: UI 存储是唯一来源（env 回退链已移除）, 原样返回。"""
    return stored if stored is not None else ProviderConfig()


def apply_changes(
    stored: Optional[ProviderConfig],
    *,
    api_base: Any = _UNSET,
    api_key: Any = _UNSET,
    model: Any = _UNSET,
) -> ProviderConfig:
    """对已保存配置应用 UI 变更。

    字段语义（HTTP POST body 约定）:
    - 缺省（未提供）= 保持不变
    - 空字符串 / 纯空白 = 清除
    - 非空字符串 = 设置（自动 strip）
    """
    current = stored.model_dump() if stored is not None else {}
    for key, value in zip(_REQUIRED_FIELDS, (api_base, api_key, model)):
        if value is _UNSET:
            continue
        if isinstance(value, str):
            value = value.strip() or None
        else:
            value = None
        current[key] = value
    return ProviderConfig(
        api_base=current.get("api_base"),
        api_key=current.get("api_key"),
        model=current.get("model"),
    )


def save_provider_config(path: Path, cfg: ProviderConfig) -> None:
    """持久化到 JSON 文件（0600, group/other 无权限）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(cfg.model_dump(), ensure_ascii=False)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
    finally:
        pass
    # Windows 上 os.open 的 mode 部分生效; POSIX 补一次显式 chmod
    try:
        os.chmod(str(path), 0o600)
    except OSError:
        pass


def load_provider_config(path: Path) -> Optional[ProviderConfig]:
    """读取持久化配置; 文件缺失/损坏返回 None（快速降级不抛出）。"""
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        return ProviderConfig(
            api_base=data.get("api_base"),
            api_key=data.get("api_key"),
            model=data.get("model"),
        )
    except Exception:  # noqa: BLE001 —— 损坏文件视同未配置
        return None


def mask_key(key: Optional[str]) -> Optional[str]:
    """密钥脱敏: 短键全掩码, 长键保留前3后4。绝不返回明文中间段。"""
    if not key:
        return None
    if len(key) <= 8:
        return "***"
    return f"{key[:3]}***{key[-4:]}"
