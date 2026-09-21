"""异步 PostgreSQL 数据访问层 (§7.1).

提供题库所有表的 CRUD 操作，使用 SQLAlchemy 2.0 async engine.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import and_, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from .models import (
    Base,
    BuildJob,
    OutboxEvent,
    Question,
    QuestionMaterial,
    QuestionResolutionEvent,
    RunRecord,
    SolutionMethod,
    SolutionStep,
)

# ── 默认连接配置 ───────────────────────────────────────────────────────────

DEFAULT_DB_URL = "postgresql+asyncpg://cai:cai@localhost:5432/question_bank"


class QuestionRepository:
    """题库 PostgreSQL 仓库.

    用法::

        repo = QuestionRepository("postgresql+asyncpg://...")
        async with repo.session() as sess:
            q = await repo.create_question(sess, ...)
    """

    def __init__(self, db_url: str = DEFAULT_DB_URL):
        self._engine = create_async_engine(
            db_url,
            echo=False,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
        )
        self._session_factory = async_sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False
        )

    async def create_tables(self) -> None:
        """幂等建表（生产用 Alembic 迁移；此方法仅用于开发/测试）."""
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def ensure_recall_metadata_column(self) -> None:
        """为已有题库补齐召回元数据列，供幂等种子导入使用。"""
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    "ALTER TABLE questions "
                    "ADD COLUMN IF NOT EXISTS recall_metadata JSONB "
                    "NOT NULL DEFAULT '{}'::jsonb"
                )
            )

    def session(self) -> AsyncSession:
        return self._session_factory()

    async def close(self) -> None:
        await self._engine.dispose()

    # ── Question ────────────────────────────────────────────────────────

    async def create_question(
        self,
        sess: AsyncSession,
        *,
        statement_raw: str,
        statement_canonical: str,
        instance_fingerprint: str,
        success_criteria: dict,
        materials_data: list[dict],
        force_new_version: bool = False,
    ) -> Question:
        """创建题目 + 关联资料。相同指纹返回已有实例（幂等）。"""
        # 指纹去重
        existing = await self._by_fingerprint(sess, instance_fingerprint)
        if existing and not force_new_version:
            return existing

        now = datetime.now(timezone.utc)
        q = Question(
            statement_raw=statement_raw,
            statement_canonical=statement_canonical,
            instance_fingerprint=instance_fingerprint,
            success_criteria=success_criteria,
            status="draft",
            version=(existing.version + 1) if existing and force_new_version else 1,
            created_at=now,
            updated_at=now,
        )
        sess.add(q)
        await sess.flush()

        for m in materials_data:
            mat = QuestionMaterial(
                question_id=q.question_id,
                ordinal=m.get("ordinal", 0),
                material_type=m.get("material_type", "writeup"),
                object_key=m.get("object_key", ""),
                mime_type=m.get("mime_type", "application/octet-stream"),
                size_bytes=m.get("size_bytes", 0),
                sha256=m.get("sha256", ""),
                status=m.get("status", "uploaded"),
                created_at=now,
            )
            sess.add(mat)

        return q

    async def _by_fingerprint(self, sess: AsyncSession, fp: str) -> Optional[Question]:
        stmt = select(Question).where(Question.instance_fingerprint == fp)
        result = await sess.execute(stmt)
        return result.scalar_one_or_none()

    async def get_question(self, sess: AsyncSession, question_id: uuid.UUID) -> Optional[Question]:
        stmt = select(Question).where(Question.question_id == question_id)
        result = await sess.execute(stmt)
        return result.scalar_one_or_none()

    async def get_question_for_update(
        self, sess: AsyncSession, question_id: uuid.UUID
    ) -> Optional[Question]:
        """Load a question while serializing build-trigger decisions for it."""
        stmt = (
            select(Question)
            .where(Question.question_id == question_id)
            .with_for_update()
        )
        result = await sess.execute(stmt)
        return result.scalar_one_or_none()

    async def get_question_by_fingerprint(
        self, sess: AsyncSession, instance_fingerprint: str
    ) -> Optional[Question]:
        stmt = select(Question).where(
            Question.instance_fingerprint == instance_fingerprint
        )
        result = await sess.execute(stmt)
        return result.scalar_one_or_none()

    async def list_questions(
        self,
        sess: AsyncSession,
        *,
        status: Optional[str] = None,
        limit: int = 25,
        cursor: Optional[str] = None,  # question_id 字符串，简单游标
    ) -> tuple[list[Question], int]:
        items, total, _has_more = await self.list_questions_page(
            sess, status=status, limit=limit, cursor=cursor
        )
        return items, total

    async def list_questions_page(
        self,
        sess: AsyncSession,
        *,
        status: Optional[str] = None,
        limit: int = 25,
        cursor: Optional[str] = None,
    ) -> tuple[list[Question], int, bool]:
        """Return a deterministic ID-ordered page and whether another page exists."""
        stmt = select(Question)
        if status:
            stmt = stmt.where(Question.status == status)
        if cursor:
            stmt = stmt.where(Question.question_id > uuid.UUID(cursor))
        stmt = stmt.order_by(Question.question_id.asc()).limit(limit + 1)

        result = await sess.execute(stmt)
        rows = result.scalars().all()

        has_more = len(rows) > limit
        items = rows[:limit]

        # 总数
        count_stmt = select(func.count()).select_from(Question)
        if status:
            count_stmt = count_stmt.where(Question.status == status)
        total_result = await sess.execute(count_stmt)
        total = total_result.scalar() or 0

        return items, total, has_more

    async def update_question_status(
        self, sess: AsyncSession, question_id: uuid.UUID, status: str
    ) -> None:
        now = datetime.now(timezone.utc)
        stmt = (
            update(Question)
            .where(Question.question_id == question_id)
            .values(status=status, updated_at=now)
        )
        await sess.execute(stmt)

    async def set_current_solution(
        self, sess: AsyncSession, question_id: uuid.UUID, solution_id: uuid.UUID
    ) -> None:
        """原子切换当前教学方法指针。"""
        now = datetime.now(timezone.utc)
        # 先 supersede 旧方法
        old_stmt = (
            update(SolutionMethod)
            .where(
                and_(
                    SolutionMethod.question_id == question_id,
                    SolutionMethod.status == "usable",
                    SolutionMethod.solution_id != solution_id,
                )
            )
            .values(status="superseded", superseded_at=now)
        )
        await sess.execute(old_stmt)
        # 设置新指针
        q_stmt = (
            update(Question)
            .where(Question.question_id == question_id)
            .values(current_solution_method_id=solution_id, updated_at=now)
        )
        await sess.execute(q_stmt)

    # ── QuestionMaterial ─────────────────────────────────────────────────

    async def get_materials(
        self, sess: AsyncSession, question_id: uuid.UUID
    ) -> list[QuestionMaterial]:
        stmt = (
            select(QuestionMaterial)
            .where(QuestionMaterial.question_id == question_id)
            .order_by(QuestionMaterial.ordinal)
        )
        result = await sess.execute(stmt)
        return list(result.scalars().all())

    async def update_material_status(
        self, sess: AsyncSession, material_id: uuid.UUID, status: str
    ) -> None:
        stmt = (
            update(QuestionMaterial)
            .where(QuestionMaterial.material_id == material_id)
            .values(status=status)
        )
        await sess.execute(stmt)

    # ── RunRecord ────────────────────────────────────────────────────────

    async def create_run_record(self, sess: AsyncSession, **kwargs: Any) -> RunRecord:
        rr = RunRecord(**kwargs)
        sess.add(rr)
        await sess.flush()
        return rr

    async def get_run_record(self, sess: AsyncSession, run_id: uuid.UUID) -> Optional[RunRecord]:
        stmt = select(RunRecord).where(RunRecord.run_id == run_id)
        result = await sess.execute(stmt)
        return result.scalar_one_or_none()

    async def update_run_status(
        self, sess: AsyncSession, run_id: uuid.UUID, **kwargs: Any
    ) -> None:
        stmt = update(RunRecord).where(RunRecord.run_id == run_id).values(**kwargs)
        await sess.execute(stmt)

    # ── SolutionMethod ───────────────────────────────────────────────────

    async def create_solution_method(
        self, sess: AsyncSession, **kwargs: Any
    ) -> SolutionMethod:
        sm = SolutionMethod(**kwargs)
        sess.add(sm)
        await sess.flush()
        return sm

    async def get_solution_method(
        self, sess: AsyncSession, solution_id: uuid.UUID
    ) -> Optional[SolutionMethod]:
        stmt = select(SolutionMethod).where(SolutionMethod.solution_id == solution_id)
        result = await sess.execute(stmt)
        return result.scalar_one_or_none()

    async def get_usable_solution(
        self, sess: AsyncSession, question_id: uuid.UUID, version: Optional[int] = None
    ) -> Optional[SolutionMethod]:
        stmt = select(SolutionMethod).where(
            and_(
                SolutionMethod.question_id == question_id,
                SolutionMethod.status == "usable",
            )
        )
        if version is not None:
            stmt = stmt.where(SolutionMethod.version == version)
        stmt = stmt.order_by(SolutionMethod.version.desc()).limit(1)
        result = await sess.execute(stmt)
        return result.scalar_one_or_none()

    async def has_usable_solution(
        self, sess: AsyncSession, question_id: uuid.UUID
    ) -> bool:
        stmt = select(func.count()).select_from(SolutionMethod).where(
            and_(
                SolutionMethod.question_id == question_id,
                SolutionMethod.status == "usable",
            )
        )
        result = await sess.execute(stmt)
        return (result.scalar() or 0) > 0

    async def update_solution_method(
        self, sess: AsyncSession, solution_id: uuid.UUID, **kwargs: Any
    ) -> None:
        stmt = (
            update(SolutionMethod)
            .where(SolutionMethod.solution_id == solution_id)
            .values(**kwargs)
        )
        await sess.execute(stmt)

    async def create_solution_steps(
        self, sess: AsyncSession, steps_data: list[dict]
    ) -> list[SolutionStep]:
        steps = [SolutionStep(**s) for s in steps_data]
        sess.add_all(steps)
        await sess.flush()
        return steps

    # ── BuildJob ─────────────────────────────────────────────────────────

    async def create_build_job(self, sess: AsyncSession, **kwargs: Any) -> BuildJob:
        bj = BuildJob(**kwargs)
        sess.add(bj)
        await sess.flush()
        return bj

    async def get_build_job(self, sess: AsyncSession, job_id: uuid.UUID) -> Optional[BuildJob]:
        stmt = select(BuildJob).where(BuildJob.job_id == job_id)
        result = await sess.execute(stmt)
        return result.scalar_one_or_none()

    async def get_build_job_for_update(
        self, sess: AsyncSession, job_id: uuid.UUID
    ) -> Optional[BuildJob]:
        stmt = (
            select(BuildJob)
            .where(BuildJob.job_id == job_id)
            .with_for_update()
        )
        result = await sess.execute(stmt)
        return result.scalar_one_or_none()

    async def get_build_job_by_idempotency(
        self, sess: AsyncSession, key: str
    ) -> Optional[BuildJob]:
        stmt = select(BuildJob).where(BuildJob.idempotency_key == key)
        result = await sess.execute(stmt)
        return result.scalar_one_or_none()

    async def update_build_job(
        self, sess: AsyncSession, job_id: uuid.UUID, **kwargs: Any
    ) -> None:
        if "updated_at" not in kwargs:
            kwargs["updated_at"] = datetime.now(timezone.utc)
        stmt = update(BuildJob).where(BuildJob.job_id == job_id).values(**kwargs)
        await sess.execute(stmt)

    async def has_active_build_job(self, sess: AsyncSession, question_id: uuid.UUID) -> bool:
        stmt = select(func.count()).select_from(BuildJob).where(
            and_(
                BuildJob.question_id == question_id,
                BuildJob.status.in_(["queued", "running", "verifying_result", "synthesizing_method", "verifying_method"]),
            )
        )
        result = await sess.execute(stmt)
        return (result.scalar() or 0) > 0

    # ── Outbox ───────────────────────────────────────────────────────────

    async def create_outbox_event(
        self,
        sess: AsyncSession,
        *,
        aggregate_type: str,
        aggregate_id: uuid.UUID,
        event_type: str,
        payload: dict,
    ) -> OutboxEvent:
        evt = OutboxEvent(
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type,
            payload=payload,
        )
        sess.add(evt)
        await sess.flush()
        return evt

    async def get_undispatched_events(
        self, sess: AsyncSession, limit: int = 100
    ) -> list[OutboxEvent]:
        stmt = (
            select(OutboxEvent)
            .where(OutboxEvent.dispatched_at.is_(None))
            .order_by(OutboxEvent.created_at)
            .limit(limit)
        )
        result = await sess.execute(stmt)
        return list(result.scalars().all())

    async def mark_dispatched(
        self, sess: AsyncSession, event_ids: list[uuid.UUID]
    ) -> None:
        now = datetime.now(timezone.utc)
        stmt = (
            update(OutboxEvent)
            .where(OutboxEvent.event_id.in_(event_ids))
            .values(dispatched_at=now)
        )
        await sess.execute(stmt)

    # ── Resolution Events ────────────────────────────────────────────────

    async def save_resolution_event(
        self, sess: AsyncSession, **kwargs: Any
    ) -> QuestionResolutionEvent:
        evt = QuestionResolutionEvent(**kwargs)
        sess.add(evt)
        await sess.flush()
        return evt
