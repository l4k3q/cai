"""题库 PostgreSQL API 路由 (§7.4).

新增端点（与现有文件版 question_bank 端点共存）:
- POST   /api/v1/question-bank/questions              创建题目（multipart）
- GET    /api/v1/question-bank/questions              题目列表（分页）
- GET    /api/v1/question-bank/questions/{id}         题目详情
- POST   /api/v1/question-bank/questions/{id}/build   触发构建
- GET    /api/v1/question-bank/build-runs/{job_id}    构建状态
- GET    /api/v1/question-bank/questions/{id}/solution 当前解法

依赖: 需要 PostgreSQL + MinIO 基础设施可用。
"""

from __future__ import annotations

import uuid
import logging
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy.exc import IntegrityError

from cai.question_bank.fingerprints import (
    canonicalize_statement,
    compute_content_fingerprint,
    compute_instance_fingerprint,
)
from cai.question_bank.materials import MAX_FILE_SIZE, MaterialStore
from cai.question_bank.repository import QuestionRepository
from cai.question_bank.security import sanitize_runtime_config
from cai.question_bank.schemas import (
    BuildRunResponse,
    BuildTriggerResponse,
    MaterialResponse,
    MaterialUploadResponse,
    QuestionBrief,
    QuestionCreate,
    QuestionResponse,
    SolutionResponse,
    SolutionStepResponse,
)

router = APIRouter(prefix="/api/v1/question-bank", tags=["question-bank-v2"])
logger = logging.getLogger(__name__)

# ── 依赖注入 ──────────────────────────────────────────────────────────────

_repo: Optional[QuestionRepository] = None
_store: Optional[MaterialStore] = None


def _get_repo() -> QuestionRepository:
    global _repo
    if _repo is None:
        _repo = QuestionRepository()
    return _repo


def _get_store() -> MaterialStore:
    global _store
    if _store is None:
        _store = MaterialStore()
    return _store


def _delete_uploaded_objects(store: MaterialStore, object_keys: list[str]) -> None:
    """Best-effort cleanup for objects uploaded before a DB commit."""
    for object_key in object_keys:
        try:
            store.delete(object_key)
        except Exception:
            logger.exception("Failed to clean up uploaded material object")


# ── 辅助 ──────────────────────────────────────────────────────────────────

def _question_to_response(q, build_job_id=None) -> QuestionResponse:
    """ORM → API response."""
    return QuestionResponse(
        question_id=q.question_id,
        version=q.version,
        status=q.status,
        instance_fingerprint=q.instance_fingerprint,
        statement_raw=q.statement_raw,
        statement_canonical=q.statement_canonical,
        success_criteria=q.success_criteria,
        recall_metadata=getattr(q, "recall_metadata", None) or {},
        current_solution_method_id=q.current_solution_method_id,
        build_job_id=build_job_id,
        materials=[
            MaterialResponse(
                material_id=m.material_id,
                ordinal=m.ordinal,
                material_type=m.material_type,
                sha256=m.sha256,
                mime_type=m.mime_type,
                size_bytes=m.size_bytes,
                status=m.status,
                created_at=m.created_at,
            )
            for m in (q.materials or [])
        ],
        created_at=q.created_at,
        updated_at=q.updated_at,
    )


def _question_to_brief(q) -> QuestionBrief:
    """ORM → 列表摘要."""
    has_usable = (
        q.current_solution_method_id is not None and q.status == "usable"
    )
    return QuestionBrief(
        question_id=q.question_id,
        version=q.version,
        status=q.status,
        instance_fingerprint=q.instance_fingerprint,
        statement_preview=(q.statement_raw or "")[:120],
        material_count=len(q.materials) if q.materials else 0,
        has_usable_method=has_usable,
        recall_metadata=getattr(q, "recall_metadata", None) or {},
        created_at=q.created_at,
        updated_at=q.updated_at,
    )


# ═══════════════════════════════════════════════════════════════════════════
# POST /questions — 创建题目（支持 multipart 资料上传）
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/questions", response_model=QuestionResponse, status_code=201)
async def create_question(
    request: Request,
    statement: str = Form(..., description="原始题面"),
    success_criteria: str = Form("{}", description="JSON 成功判据"),
    force_new_version: bool = Form(False),
    auto_build: bool = Form(True, description="创建后自动触发构建（默认开启）"),
    agent: Optional[str] = Form(None, description="复现用的 agent（默认 one_tool_agent）"),
    model: Optional[str] = Form(None, description="复现/总结用的模型（默认取环境变量 CAI_MODEL）"),
    base_url: Optional[str] = Form(None, description="可选自定义 provider 端点"),
    api_key: Optional[str] = Form(None, description="可选自定义 provider 密钥"),
    materials: list[UploadFile] = File(default_factory=list),
):
    """创建题目 + 上传资料 + (可选) 自动触发构建. 按 instance_fingerprint 去重."""
    repo = _get_repo()
    store = _get_store()

    # 构建运行时配置（仅保留非空字段）
    runtime_config = {
        k: v for k, v in {
            "agent": agent, "model": model, "base_url": base_url, "api_key": api_key,
        }.items() if v
    }

    # 解析 success_criteria
    import json as _json
    try:
        criteria = _json.loads(success_criteria)
    except _json.JSONDecodeError:
        raise HTTPException(status_code=422, detail="success_criteria 必须是合法 JSON")

    # 规范化题面 + 上传资料
    canonical = canonicalize_statement(statement)
    prepared_materials: list[dict] = []
    material_fingerprints: list[str] = []

    for idx, file in enumerate(materials):
        content = await file.read()
        if len(content) > MAX_FILE_SIZE:
            raise HTTPException(
                status_code=413,
                detail=f"文件 {file.filename} 超过 {MAX_FILE_SIZE // 1024 // 1024} MiB 限制",
            )
        material_fingerprints.append(compute_content_fingerprint(content))
        mime = file.content_type or "application/octet-stream"
        prepared_materials.append(
            {
                "ordinal": idx,
                "filename": file.filename or f"material_{idx}",
                "content": content,
                "mime_type": mime,
                "material_type": _infer_material_type(file.filename or "", mime),
            }
        )

    fingerprint = compute_instance_fingerprint(statement, material_fingerprints)

    async with repo.session() as sess:
        async with sess.begin():
            existing = await repo.get_question_by_fingerprint(sess, fingerprint)
            if existing is not None and not force_new_version:
                await sess.refresh(existing, ["materials"])
    if existing is not None and not force_new_version:
        return _question_to_response(existing)

    material_data: list[dict] = []
    uploaded_keys: list[str] = []
    try:
        for prepared in prepared_materials:
            object_key = MaterialStore.make_object_key(
                uuid.uuid4(), prepared["material_type"], prepared["filename"]
            )
            uploaded_keys.append(object_key)
            result = store.upload(
                object_key,
                prepared["content"],
                prepared["mime_type"],
            )
            material_data.append(
                {
                    "ordinal": prepared["ordinal"],
                    "material_type": prepared["material_type"],
                    "object_key": object_key,
                    "mime_type": prepared["mime_type"],
                    "size_bytes": result["size_bytes"],
                    "sha256": result["sha256"],
                    "status": "available",
                }
            )
    except Exception:
        _delete_uploaded_objects(store, uploaded_keys)
        raise

    build_job_id = None
    duplicate = False
    try:
        async with repo.session() as sess:
            async with sess.begin():
                existing_after_upload = await repo.get_question_by_fingerprint(
                    sess, fingerprint
                )
                if existing_after_upload is not None and not force_new_version:
                    q = existing_after_upload
                    duplicate = True
                else:
                    q = await repo.create_question(
                        sess,
                        statement_raw=statement,
                        statement_canonical=canonical,
                        instance_fingerprint=fingerprint,
                        success_criteria=criteria,
                        materials_data=material_data,
                        force_new_version=force_new_version,
                    )
                await sess.refresh(q, ["materials"])

                if not duplicate and auto_build and q.status in ("draft", "incomplete"):
                    if not await repo.has_active_build_job(sess, q.question_id):
                        job = await repo.create_build_job(
                            sess,
                            question_id=q.question_id,
                            status="queued",
                            runtime_config=runtime_config,
                        )
                        build_job_id = job.job_id
                        await repo.update_question_status(sess, q.question_id, "building")
                        await repo.create_outbox_event(
                            sess,
                            aggregate_type="BuildJob",
                            aggregate_id=job.job_id,
                            event_type="build_job_queued",
                            payload={
                                "job_id": str(job.job_id),
                                "question_id": str(q.question_id),
                            },
                        )

        if duplicate:
            _delete_uploaded_objects(store, uploaded_keys)
        return _question_to_response(q, build_job_id)
    except IntegrityError:
        _delete_uploaded_objects(store, uploaded_keys)
        if not force_new_version:
            async with repo.session() as sess:
                async with sess.begin():
                    existing = await repo.get_question_by_fingerprint(sess, fingerprint)
                    if existing is not None:
                        await sess.refresh(existing, ["materials"])
            if existing is not None:
                return _question_to_response(existing)
        raise HTTPException(status_code=409, detail="题目指纹已存在")
    except Exception:
        _delete_uploaded_objects(store, uploaded_keys)
        raise


def _infer_material_type(filename: str, mime: str) -> str:
    """根据文件名/MIME 推断 material_type."""
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


# ═══════════════════════════════════════════════════════════════════════════
# GET /questions — 题目列表
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/questions")
async def list_questions(
    status: Optional[str] = None,
    limit: int = 25,
    cursor: Optional[str] = None,
):
    """分页列出题目摘要."""
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=422, detail="limit 需在 1-100 之间")

    repo = _get_repo()
    async with repo.session() as sess:
        items, total, has_more = await repo.list_questions_page(
            sess, status=status, limit=limit, cursor=cursor
        )

    briefs = [_question_to_brief(q) for q in items]
    next_cursor = str(items[-1].question_id) if has_more and items else None

    return {
        "items": [b.model_dump() for b in briefs],
        "total": total,
        "limit": limit,
        "cursor": cursor,
        "next_cursor": next_cursor,
    }


# ═══════════════════════════════════════════════════════════════════════════
# GET /questions/{question_id} — 题目详情
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/questions/{question_id}", response_model=QuestionResponse)
async def get_question(question_id: uuid.UUID):
    """获取题目完整详情，含资料列表."""
    repo = _get_repo()
    async with repo.session() as sess:
        q = await repo.get_question(sess, question_id)
        if q is None:
            raise HTTPException(status_code=404, detail="题库条目不存在")
        return _question_to_response(q)


# ═══════════════════════════════════════════════════════════════════════════
# POST /questions/{question_id}/build — 触发构建
# ═══════════════════════════════════════════════════════════════════════════

@router.post(
    "/questions/{question_id}/build",
    response_model=BuildTriggerResponse,
    status_code=202,
)
async def trigger_build(
    question_id: uuid.UUID,
    request: Request,
    agent: Optional[str] = Form(None, description="复现用的 agent（默认 one_tool_agent）"),
    model: Optional[str] = Form(None, description="复现/总结用的模型（默认取环境变量 CAI_MODEL）"),
    base_url: Optional[str] = Form(None, description="可选自定义 provider 端点"),
    api_key: Optional[str] = Form(None, description="可选自定义 provider 密钥"),
):
    """将题目投递到 Redis 构建队列 (§7.2)."""
    repo = _get_repo()

    # 构建运行时配置（仅保留非空字段）
    runtime_config = {
        k: v for k, v in {
            "agent": agent, "model": model, "base_url": base_url, "api_key": api_key,
        }.items() if v
    }

    # 幂等 key
    idempotency_key = request.headers.get("Idempotency-Key")
    import hashlib
    request_sha256 = None

    try:
        async with repo.session() as sess:
            async with sess.begin():
                q = await repo.get_question_for_update(sess, question_id)
                if q is None:
                    raise HTTPException(status_code=404, detail="题目不存在")

                if idempotency_key:
                    existing = await repo.get_build_job_by_idempotency(
                        sess, idempotency_key
                    )
                    if existing is not None:
                        if existing.question_id != question_id:
                            raise HTTPException(
                                status_code=409,
                                detail="Idempotency-Key 已用于其他题目",
                            )
                        response = BuildTriggerResponse(
                            job_id=existing.job_id,
                            question_id=existing.question_id,
                            status=existing.status,
                            idempotency_key=idempotency_key,
                        )
                    else:
                        response = None
                else:
                    response = None

                if response is None:
                    if await repo.has_active_build_job(sess, question_id):
                        raise HTTPException(status_code=409, detail="已有活动构建任务")

                    materials = await repo.get_materials(sess, question_id)
                    available_mats = [m for m in materials if m.status == "available"]
                    if not available_mats and q.success_criteria == {}:
                        raise HTTPException(
                            status_code=422,
                            detail="资料或成功判据不完整，无法构建",
                        )

                    job = await repo.create_build_job(
                        sess,
                        question_id=question_id,
                        status="queued",
                        idempotency_key=idempotency_key,
                        runtime_config=runtime_config,
                    )
                    await repo.update_question_status(sess, question_id, "building")
                    await repo.create_outbox_event(
                        sess,
                        aggregate_type="BuildJob",
                        aggregate_id=job.job_id,
                        event_type="build_job_queued",
                        payload={
                            "job_id": str(job.job_id),
                            "question_id": str(question_id),
                        },
                    )
                    response = BuildTriggerResponse(
                        job_id=job.job_id,
                        question_id=question_id,
                        status="queued",
                        idempotency_key=idempotency_key,
                    )
        return response
    except IntegrityError:
        if idempotency_key:
            async with repo.session() as sess:
                async with sess.begin():
                    existing = await repo.get_build_job_by_idempotency(
                        sess, idempotency_key
                    )
            if existing is not None and existing.question_id == question_id:
                return BuildTriggerResponse(
                    job_id=existing.job_id,
                    question_id=existing.question_id,
                    status=existing.status,
                    idempotency_key=idempotency_key,
                )
        raise HTTPException(status_code=409, detail="构建请求冲突，请重试")


# ═══════════════════════════════════════════════════════════════════════════
# GET /build-runs/{job_id} — 构建状态
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/build-runs/{job_id}", response_model=BuildRunResponse)
async def get_build_run(job_id: uuid.UUID):
    """查询构建任务状态、进度和错误."""
    repo = _get_repo()
    async with repo.session() as sess:
        job = await repo.get_build_job(sess, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="构建任务不存在")
        return BuildRunResponse(
            job_id=job.job_id,
            question_id=job.question_id,
            run_id=job.run_id,
            status=job.status,
            progress=job.progress,
            retryable=job.retryable,
            blocker=job.blocker,
            error_code=job.error_code,
            runtime_config=sanitize_runtime_config(job.runtime_config or {}),
            started_at=job.started_at,
            finished_at=job.finished_at,
            created_at=job.created_at,
            updated_at=job.updated_at,
        )


# ═══════════════════════════════════════════════════════════════════════════
# GET /questions/{question_id}/solution — 当前解法
# ═══════════════════════════════════════════════════════════════════════════

@router.get(
    "/questions/{question_id}/solution",
    response_model=SolutionResponse,
)
async def get_solution(
    question_id: uuid.UUID,
    version: Optional[int] = None,
):
    """获取题目的当前 usable SolutionMethod."""
    repo = _get_repo()
    async with repo.session() as sess:
        # 先确认题目存在
        q = await repo.get_question(sess, question_id)
        if q is None:
            raise HTTPException(status_code=404, detail="题目不存在")

        solution = await repo.get_usable_solution(sess, question_id, version)
        if solution is None:
            raise HTTPException(status_code=409, detail="尚无可用解法 (usable)")

        return SolutionResponse(
            solution_id=solution.solution_id,
            question_id=solution.question_id,
            version=solution.version,
            status=solution.status,
            overall_approach=solution.overall_approach,
            principles=solution.principles,
            verified_run_id=solution.verified_run_id,
            steps=[
                SolutionStepResponse(
                    step_id=s.step_id,
                    ordinal=s.ordinal,
                    goal=s.goal,
                    why=s.why,
                    principle=s.principle,
                    action=s.action,
                    result=s.result,
                    evidence_refs=s.evidence_refs,
                )
                for s in (solution.steps or [])
            ],
            created_at=solution.created_at,
        )
