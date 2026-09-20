"""S3/MinIO 对象存储 — 题库资料管理 (§5.2 + §7.1).

保存 Writeup、参考答案、源码、脚本、PCAP、Trace 等大文件。
数据库只保存 object_key + 元数据，实际文件由本模块管理。
"""

from __future__ import annotations

import hashlib
import tarfile
import uuid
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Optional

import boto3
from botocore.config import Config as BotoConfig

# ── 默认 MinIO 配置 (与 docker-compose.yml 一致) ────────────────────────────

DEFAULT_ENDPOINT = "http://localhost:9000"
DEFAULT_BUCKET = "cai-question-bank"
DEFAULT_ACCESS_KEY = "minioadmin"
DEFAULT_SECRET_KEY = "minioadmin"

# 单个文件最大 100 MiB (§5.2)
MAX_FILE_SIZE = 100 * 1024 * 1024  # 104_857_600

# 文本类资料提取时截断到的最大字符数（避免超长文件撑爆上下文）
MAX_TEXT_EXTRACT_CHARS = 20_000

# 可直接作为文本读取的资料类型（其余按二进制处理）
TEXT_MATERIAL_TYPES = {"writeup", "source", "environment"}

# ── 归档内文本提取（zip/tar 内嵌的源码、脚本、说明等） ───────────────────────
# 只提取这些扩展名的文本成员，跳过二进制（图片/pcap/可执行文件等）。
ARCHIVE_TEXT_EXTS = {
    "py", "js", "ts", "go", "java", "c", "cpp", "h", "hpp", "rs", "sh", "ps1",
    "rb", "php", "pl", "txt", "md", "json", "xml", "yml", "yaml", "toml", "ini",
    "cfg", "env", "html", "css", "sql", "log", "pem", "key", "pub", "crt",
}
# 防御 zip-bomb：限制成员数量与单个成员解压大小。
MAX_ARCHIVE_FILES = 60
MAX_ARCHIVE_MEMBER_BYTES = 200_000


def infer_material_type(filename: str, mime: str) -> str:
    """根据文件名/MIME 推断 material_type (§7.1)."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext in ("py", "js", "ts", "go", "java", "c", "cpp", "rs", "sh", "ps1", "rb"):
        return "source"
    if ext in ("pcap", "pcapng", "cap"):
        return "pcap"
    if ext in ("png", "jpg", "jpeg", "gif", "bmp", "svg", "webp"):
        return "image"
    if ext in ("zip", "tar", "gz", "bz2", "xz", "7z", "rar"):
        return "archive"
    if ext in ("pdf", "md", "txt", "html", "xml", "json"):
        return "writeup"
    if ext in ("yml", "yaml", "toml", "env", "dockerfile"):
        return "environment"
    if mime.startswith("text/") or mime == "application/json":
        return "writeup"
    return "writeup"


def _looks_like_text(data: bytes) -> bool:
    """启发式判断字节是否为文本（含 NUL 字节视为二进制）."""
    return b"\x00" not in data


def _extract_archive_text(content: bytes) -> str:
    """从 zip/tar 归档中提取内嵌文本文件的内容（用于喂给 Agent 阅读）."""
    chunks: list[str] = []

    def _add_text(name: str, data: bytes) -> None:
        if len(chunks) >= MAX_ARCHIVE_FILES:
            return
        if _looks_like_text(data):
            text = data.decode("utf-8", errors="replace")
            chunks.append(f"--- {name} ---\n{text}")
        else:
            chunks.append(f"[{name}] (二进制，跳过)")

    def _want_ext(name: str) -> bool:
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        return ext in ARCHIVE_TEXT_EXTS

    # 先按 zip 处理，失败则回退到 tar（支持 .tar/.tar.gz/.tgz/.tar.bz2/.tar.xz）
    try:
        with zipfile.ZipFile(BytesIO(content)) as zf:
            for info in zf.infolist():
                if info.is_dir() or len(chunks) >= MAX_ARCHIVE_FILES:
                    continue
                if not _want_ext(info.filename):
                    continue
                if info.file_size > MAX_ARCHIVE_MEMBER_BYTES:
                    chunks.append(f"[{info.filename}] (跳过：文件过大 {info.file_size} bytes)")
                    continue
                _add_text(info.filename, zf.read(info))
    except zipfile.BadZipFile:
        try:
            with tarfile.open(fileobj=BytesIO(content), mode="r:*") as tf:
                for member in tf.getmembers():
                    if not member.isfile() or len(chunks) >= MAX_ARCHIVE_FILES:
                        continue
                    if not _want_ext(member.name):
                        continue
                    if member.size > MAX_ARCHIVE_MEMBER_BYTES:
                        chunks.append(f"[{member.name}] (跳过：文件过大 {member.size} bytes)")
                        continue
                    f = tf.extractfile(member)
                    if f is None:
                        continue
                    _add_text(member.name, f.read())
        except (tarfile.TarError, EOFError):
            pass
    except Exception:
        # 归档损坏/异常时静默降级，不阻断上传流程
        pass

    if not chunks:
        return ""
    return "\n\n".join(chunks)[:MAX_TEXT_EXTRACT_CHARS]


def extract_material_text(content: bytes, material_type: str) -> str:
    """从资料字节中提取可读文本。

    - 文本类（writeup/source/environment）：直接解码。
    - archive（zip/tar）：提取内嵌文本文件内容。
    - 其余二进制类型（pcap/image）：返回空串。
    """
    if material_type == "archive":
        return _extract_archive_text(content)
    if material_type not in TEXT_MATERIAL_TYPES:
        return ""
    text = content.decode("utf-8", errors="replace")
    return text[:MAX_TEXT_EXTRACT_CHARS]


class MaterialStore:
    """S3-compatible 对象存储（MinIO 后端）.

    用法::

        store = MaterialStore()
        key = await store.upload("writeups/abc.txt", b"...", "text/plain")
        data = await store.download(key)
    """

    def __init__(
        self,
        endpoint: str = DEFAULT_ENDPOINT,
        bucket: str = DEFAULT_BUCKET,
        access_key: str = DEFAULT_ACCESS_KEY,
        secret_key: str = DEFAULT_SECRET_KEY,
        region: str = "us-east-1",
    ):
        self._bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=BotoConfig(
                signature_version="s3v4",
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        """确保 bucket 存在（幂等）."""
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except Exception:
            self._client.create_bucket(Bucket=self._bucket)

    # ── 上传 ────────────────────────────────────────────────────────────

    def upload(
        self,
        object_key: str,
        data: bytes,
        mime_type: str = "application/octet-stream",
        metadata: Optional[dict] = None,
    ) -> dict:
        """上传资料到对象存储。返回 {object_key, sha256, size_bytes, mime_type}."""
        if len(data) > MAX_FILE_SIZE:
            raise ValueError(f"文件过大: {len(data)} bytes (最大 {MAX_FILE_SIZE})")

        sha256 = hashlib.sha256(data).hexdigest()
        extra_args: dict = {"ContentType": mime_type}
        if metadata:
            extra_args["Metadata"] = {k: str(v) for k, v in metadata.items()}

        self._client.upload_fileobj(
            BytesIO(data),
            self._bucket,
            object_key,
            ExtraArgs=extra_args,
        )

        return {
            "object_key": object_key,
            "sha256": sha256,
            "size_bytes": len(data),
            "mime_type": mime_type,
        }

    def upload_file(
        self,
        object_key: str,
        file_path: Path,
        mime_type: str = "application/octet-stream",
        metadata: Optional[dict] = None,
    ) -> dict:
        """从本地文件上传."""
        data = file_path.read_bytes()
        return self.upload(object_key, data, mime_type, metadata)

    # ── 下载 ────────────────────────────────────────────────────────────

    def download(self, object_key: str) -> bytes:
        """下载资料内容."""
        buf = BytesIO()
        self._client.download_fileobj(self._bucket, object_key, buf)
        return buf.getvalue()

    def download_to_file(self, object_key: str, dest: Path) -> None:
        """下载到本地文件."""
        self._client.download_file(self._bucket, object_key, str(dest))

    # ── 预签名 URL（限时临时访问）────────────────────────────────────────

    def presigned_get_url(self, object_key: str, expires_seconds: int = 3600) -> str:
        """生成预签名下载 URL（供前端限时访问，不暴露 object_key）。"""
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": object_key},
            ExpiresIn=expires_seconds,
        )

    # ── 存在性 ──────────────────────────────────────────────────────────

    def exists(self, object_key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=object_key)
            return True
        except Exception:
            return False

    def head(self, object_key: str) -> dict | None:
        """返回对象元数据 {size, sha256, mime_type}."""
        try:
            resp = self._client.head_object(Bucket=self._bucket, Key=object_key)
            return {
                "size": resp.get("ContentLength", 0),
                "sha256": resp.get("Metadata", {}).get("sha256", ""),
                "mime_type": resp.get("ContentType", "application/octet-stream"),
            }
        except Exception:
            return None

    # ── 删除 ────────────────────────────────────────────────────────────

    def delete(self, object_key: str) -> None:
        self._client.delete_object(Bucket=self._bucket, Key=object_key)

    # ── 上传用临时 key 生成 ──────────────────────────────────────────────

    @staticmethod
    def make_object_key(question_id: uuid.UUID, material_type: str, filename: str) -> str:
        """生成标准化的对象存储 key."""
        safe_name = "".join(c for c in filename if c.isalnum() or c in "._-")
        return f"questions/{question_id.hex}/{material_type}/{uuid.uuid4().hex[:8]}_{safe_name}"

    @staticmethod
    def make_trace_key(run_id: uuid.UUID) -> str:
        return f"runs/{run_id.hex}/trace.jsonl"

    @staticmethod
    def make_event_key(run_id: uuid.UUID, event_index: int) -> str:
        return f"runs/{run_id.hex}/events/{event_index:06d}.json"

    @staticmethod
    def make_log_key(job_id: uuid.UUID) -> str:
        return f"build_jobs/{job_id.hex}/build.log"
