# -*- coding: utf-8 -*-
"""SpectatorLogger: JSONL 结构化日志（data-model.md §5）。

写失败静默（日志故障不反噬主流程）; 事件行经 Pydantic 校验。
禁止记录: API key / 凭据 / flag / 判据值 / 完整对话原文。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from .models import SpectatorEvaluationEvent


class SpectatorLogger:
    """追加写 JSONL 事件日志。线程安全性由单事件单行追加语义保证（v1 单进程事件循环内使用）。"""

    def __init__(
        self,
        log_dir: Optional[str] = None,
        filename: Optional[str] = None,
    ) -> None:
        self._log_dir = Path(log_dir) if log_dir else Path.home() / ".cai" / "logs"
        self._filename = filename  # None → 按日期命名 spectator_YYYYMMDD.jsonl

    def log(self, event: str, session_id: str, round_count: int, payload: dict) -> None:
        """追加一条事件。

        - 非法事件名/字段: 构造失败直接抛出（调用方编程错误, 快速失败）
        - 磁盘/IO 故障: 静默吞掉（环境故障不反噬主流程, 宪法 II 降级语义）
        """
        record = SpectatorEvaluationEvent(
            event=event, session_id=session_id, round=round_count, payload=payload
        )
        try:
            self._write(record)
        except Exception:
            return

    def _target(self) -> Path:
        if self._filename:
            return self._log_dir / self._filename
        from datetime import date

        return self._log_dir / f"spectator_{date.today():%Y%m%d}.jsonl"

    def _write(self, record: SpectatorEvaluationEvent) -> None:
        target = self._target()
        os.makedirs(self._log_dir, exist_ok=True)
        line = record.model_dump_json()
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(json.loads(line), ensure_ascii=False) + "\n")
