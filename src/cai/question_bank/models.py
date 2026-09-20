"""SQLAlchemy 2.0 ORM - 映射已建 PostgreSQL 题库表 (§5 领域模型).

所有表已通过 001_question_bank.sql 在 PostgreSQL 中创建。
本文件的 ORM 映射与 DDL 保持一致，使用 async 引擎。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# ── Question (§5.1) ────────────────────────────────────────────────────────

class Question(Base):
    __tablename__ = "questions"

    question_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    statement_raw: Mapped[str] = mapped_column(Text, nullable=False)
    statement_canonical: Mapped[str] = mapped_column(Text, nullable=False)
    instance_fingerprint: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    success_criteria: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    recall_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    current_solution_method_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="draft",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    # Relationships
    materials: Mapped[list["QuestionMaterial"]] = relationship(
        "QuestionMaterial", back_populates="question", lazy="selectin",
        order_by="QuestionMaterial.ordinal",
    )
    run_records: Mapped[list["RunRecord"]] = relationship(
        "RunRecord", back_populates="question", lazy="selectin",
    )
    solution_methods: Mapped[list["SolutionMethod"]] = relationship(
        "SolutionMethod", back_populates="question", lazy="selectin",
    )
    build_jobs: Mapped[list["BuildJob"]] = relationship(
        "BuildJob", back_populates="question", lazy="selectin",
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'building', 'usable', 'incomplete')",
            name="questions_status_check",
        ),
        Index("idx_questions_status", "status"),
        Index("idx_questions_fingerprint", "instance_fingerprint"),
    )


# ── QuestionMaterial (§5.2) ───────────────────────────────────────────────

class QuestionMaterial(Base):
    __tablename__ = "question_materials"

    material_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()")
    )
    question_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("questions.question_id", ondelete="CASCADE"),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    material_type: Mapped[str] = mapped_column(String, nullable=False)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(String, nullable=False, default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # BIGINT in DDL
    sha256: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="uploaded",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    question: Mapped["Question"] = relationship("Question", back_populates="materials")

    __table_args__ = (
        CheckConstraint(
            "status IN ('uploaded', 'available', 'failed')",
            name="question_materials_status_check",
        ),
        CheckConstraint(
            "material_type IN ('writeup','answer','source','script','pcap','image','archive','environment')",
            name="question_materials_type_check",
        ),
        Index("idx_materials_question", "question_id"),
    )


# ── RunRecord (§5.3) ──────────────────────────────────────────────────────

class RunRecord(Base):
    __tablename__ = "run_records"

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()")
    )
    question_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("questions.question_id", ondelete="CASCADE"),
        nullable=False,
    )
    session_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    source_solution_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    temporary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    trigger: Mapped[str] = mapped_column(
        String, nullable=False, default="autonomous",
    )
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="queued",
    )
    trace_object_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    final_output_object_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    verification_result: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    blocker: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    worker_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    question: Mapped["Question"] = relationship("Question", back_populates="run_records")

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','verified','incomplete','failed','timeout','stopped')",
            name="run_records_status_check",
        ),
        CheckConstraint(
            "trigger IN ('reference_validation','autonomous','replay')",
            name="run_records_trigger_check",
        ),
        Index("idx_run_records_question", "question_id"),
        Index("idx_run_records_status", "status"),
    )


# ── SolutionMethod (§5.4) ─────────────────────────────────────────────────

class SolutionMethod(Base):
    __tablename__ = "solution_methods"

    solution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()")
    )
    question_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("questions.question_id", ondelete="CASCADE"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="draft",
    )
    overall_approach: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    principles: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    verified_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("run_records.run_id"),
        nullable=True,
    )
    verification_result: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    replay_plan: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    superseded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    question: Mapped["Question"] = relationship("Question", back_populates="solution_methods")
    steps: Mapped[list["SolutionStep"]] = relationship(
        "SolutionStep", back_populates="solution", lazy="selectin",
        order_by="SolutionStep.ordinal",
    )
    verified_run: Mapped[Optional["RunRecord"]] = relationship("RunRecord")

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','verifying','usable','superseded','failed')",
            name="solution_methods_status_check",
        ),
        Index("idx_solution_methods_question", "question_id"),
        Index("idx_solution_methods_status", "status"),
    )


# ── SolutionStep (§5.4) ───────────────────────────────────────────────────

class SolutionStep(Base):
    __tablename__ = "solution_steps"

    step_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()")
    )
    solution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("solution_methods.solution_id", ondelete="CASCADE"),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    goal: Mapped[str] = mapped_column(Text, nullable=False, default="")
    why: Mapped[str] = mapped_column(Text, nullable=False, default="")
    principle: Mapped[str] = mapped_column(Text, nullable=False, default="")
    action: Mapped[str] = mapped_column(Text, nullable=False, default="")
    result: Mapped[str] = mapped_column(Text, nullable=False, default="")
    evidence_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    solution: Mapped["SolutionMethod"] = relationship("SolutionMethod", back_populates="steps")

    __table_args__ = (
        Index("idx_solution_steps_solution", "solution_id"),
    )


# ── BuildJob (§7.2) ───────────────────────────────────────────────────────

class BuildJob(Base):
    __tablename__ = "build_jobs"

    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()")
    )
    question_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("questions.question_id", ondelete="CASCADE"),
        nullable=False,
    )
    run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("run_records.run_id"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="queued",
    )
    progress: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    blocker: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_code: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(Text, unique=True, nullable=True)
    request_sha256: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    runtime_config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    question: Mapped["Question"] = relationship("Question", back_populates="build_jobs")

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','verifying_result','synthesizing_method','verifying_method','usable','incomplete','failed')",
            name="build_jobs_status_check",
        ),
        Index("idx_build_jobs_question", "question_id"),
        Index("idx_build_jobs_status", "status"),
    )


# ── QuestionResolutionEvent (§7.3) ─────────────────────────────────────────

class QuestionResolutionEvent(Base):
    __tablename__ = "question_resolution_events"

    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()")
    )
    session_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    question_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("questions.question_id"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String, nullable=False)
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    candidate_ids: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    reasons: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    resolver_version: Mapped[str] = mapped_column(String, nullable=False, default="resolver-v1")
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    message_sha256: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    model_version: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


# ── OutboxEvent (§7.2) ────────────────────────────────────────────────────

class OutboxEvent(Base):
    __tablename__ = "outbox_events"

    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()")
    )
    aggregate_type: Mapped[str] = mapped_column(Text, nullable=False)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    dispatched_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("idx_outbox_dispatched", "dispatched_at", postgresql_where="dispatched_at IS NULL"),
    )
