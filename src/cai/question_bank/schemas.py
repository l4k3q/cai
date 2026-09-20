"""Pydantic API 模型 — 题库管理接口的输入输出 (§7.4)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


# ── 题目创建 ───────────────────────────────────────────────────────────────

class MaterialRef(BaseModel):
    """创建题目时引用的已上传资料."""
    ordinal: int
    material_type: str
    object_key: str
    sha256: str = ""
    mime_type: str = "application/octet-stream"
    size_bytes: int = 0


class QuestionCreate(BaseModel):
    """POST /api/v1/question-bank/questions 请求体."""
    statement: str = Field(..., description="原始题面")
    success_criteria: dict = Field(default_factory=dict)
    materials: list[MaterialRef] = Field(default_factory=list)
    runtime_config: Optional[dict] = None
    force_new_version: bool = False


# ── 题目响应 ───────────────────────────────────────────────────────────────

class MaterialResponse(BaseModel):
    material_id: uuid.UUID
    ordinal: int
    material_type: str
    sha256: str
    mime_type: str = "application/octet-stream"
    size_bytes: int = 0
    status: str
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class QuestionBrief(BaseModel):
    """GET /questions 列表项."""
    question_id: uuid.UUID
    version: int
    status: str
    instance_fingerprint: str
    statement_preview: str = ""
    material_count: int = 0
    has_usable_method: bool = False
    recall_metadata: dict = Field(default_factory=dict)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class QuestionResponse(BaseModel):
    """GET /questions/{id} 完整响应."""
    question_id: uuid.UUID
    version: int
    status: str
    instance_fingerprint: str
    statement_raw: str
    statement_canonical: str
    success_criteria: dict
    recall_metadata: dict = Field(default_factory=dict)
    current_solution_method_id: Optional[uuid.UUID] = None
    build_job_id: Optional[uuid.UUID] = None
    materials: list[MaterialResponse] = Field(default_factory=list)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# ── 构建任务 ───────────────────────────────────────────────────────────────

class BuildRunResponse(BaseModel):
    """GET /build-runs/{run_id} 响应."""
    job_id: uuid.UUID
    question_id: uuid.UUID
    run_id: Optional[uuid.UUID] = None
    status: str
    progress: float
    retryable: bool
    blocker: Optional[str] = None
    error_code: Optional[str] = None
    runtime_config: dict = Field(default_factory=dict)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class BuildTriggerResponse(BaseModel):
    """POST /questions/{id}/build 响应."""
    job_id: uuid.UUID
    question_id: uuid.UUID
    status: str = "queued"
    idempotency_key: Optional[str] = None


# ── SolutionMethod / SolutionStep ──────────────────────────────────────────

class SolutionStepResponse(BaseModel):
    step_id: uuid.UUID
    ordinal: int
    goal: str
    why: str
    principle: str
    action: str
    result: str
    evidence_refs: list = Field(default_factory=list)

    model_config = {"from_attributes": True}


class SolutionResponse(BaseModel):
    """GET /questions/{id}/solution 响应."""
    solution_id: uuid.UUID
    question_id: uuid.UUID
    version: int
    status: str
    overall_approach: Optional[str] = None
    principles: Optional[dict] = None
    verified_run_id: Optional[uuid.UUID] = None
    steps: list[SolutionStepResponse] = Field(default_factory=list)
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# ── 题目定位 ───────────────────────────────────────────────────────────────

class ResolutionResponse(BaseModel):
    """题目定位结果 (§7.3)."""
    status: str  # matched | insufficient | unmatched | skipped
    question_id: Optional[uuid.UUID] = None
    method_status: Optional[str] = None  # usable | unavailable | null
    solution_method_id: Optional[uuid.UUID] = None
    solution_version: Optional[int] = None
    candidate_ids: list[uuid.UUID] = Field(default_factory=list)
    confidence: float = 0.0
    reasons: list[str] = Field(default_factory=list)
    resolver_version: str = "resolver-v1"
    latency_ms: int = 0
    locked: bool = True


# ── Session 学习上下文 ─────────────────────────────────────────────────────

class LearningContextResponse(BaseModel):
    """GET /sessions/{id}/learning-context."""
    question_id: Optional[uuid.UUID] = None
    solution_method_id: Optional[uuid.UUID] = None
    solution_version: Optional[int] = None
    learning_mode: Optional[str] = None  # explain | replay | live_solve
    resolution_status: Optional[str] = None
    resolution_locked: bool = False
    current_step_id: Optional[uuid.UUID] = None
    question: Optional[QuestionResponse] = None
    solution: Optional[SolutionResponse] = None


class ReplayRequest(BaseModel):
    """POST /sessions/{id}/replay."""
    step_id: Optional[uuid.UUID] = None
    confirm: bool = True


# ── 分页 ───────────────────────────────────────────────────────────────────

class PaginatedResponse(BaseModel):
    items: list[Any] = Field(default_factory=list)
    total: int = 0
    limit: int = 25
    cursor: Optional[str] = None
    next_cursor: Optional[str] = None


# ── 资料上传响应 ───────────────────────────────────────────────────────────

class MaterialUploadResponse(BaseModel):
    material_id: uuid.UUID
    question_id: Optional[uuid.UUID] = None
    ordinal: int
    material_type: str
    object_key: str
    sha256: str
    mime_type: str
    size_bytes: int
    status: str
