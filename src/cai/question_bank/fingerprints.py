"""题面规范化与实例指纹 (§5.1).

instance_fingerprint = SHA-256(statement_canonical + sorted(material_fingerprints))
- 规范化: 统一换行、去首尾空白、标准化代码块缩进
- 指纹: 题面实质内容 + 不可忽略的资料内容摘要
"""

from __future__ import annotations

import hashlib
import re
from typing import Sequence


def canonicalize_statement(raw: str) -> str:
    """规范化题面文本，消除不影响语义的差异.

    - 统一换行为 LF
    - 删除行尾空白
    - 统一连续空行为单个空行
    - 标准化前导/尾随空白
    """
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    # 删除行尾空格
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    # 压缩连续空行
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def compute_instance_fingerprint(
    statement_raw: str,
    material_fingerprints: Sequence[str] = (),
) -> str:
    """计算题目实例指纹.

    fingerprint = SHA-256(
        canonicalize_statement(statement_raw)
        + "\x00".join(sorted(material_fingerprints))
    )
    """
    canonical = canonicalize_statement(statement_raw)
    parts = [canonical]
    parts.extend(sorted(material_fingerprints))
    joined = "\x00".join(parts)
    return "sha256:" + hashlib.sha256(joined.encode("utf-8")).hexdigest()


def compute_content_fingerprint(content: bytes) -> str:
    """计算单个资料内容 SHA-256."""
    return "sha256:" + hashlib.sha256(content).hexdigest()


def extract_instance_params(statement: str) -> dict:
    """从题面中提取实例参数（IP、端口、flag 格式等）.

    第一版只做简单标记提取，不做语义解析。
    """
    params: dict = {}
    # 提取 IPv4 地址
    ipv4 = re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", statement)
    if ipv4:
        params["ipv4_addresses"] = sorted(set(ipv4))
    # 提取端口
    ports = re.findall(r"\bport\s*[:=]?\s*(\d{1,5})\b", statement, re.IGNORECASE)
    if ports:
        params["ports"] = sorted(set(int(p) for p in ports if 1 <= int(p) <= 65535))
    # 提取 flag 格式提示
    flag_formats = re.findall(r"flag\{[^}]+\}", statement)
    if flag_formats:
        params["flag_format_examples"] = flag_formats
    return params
