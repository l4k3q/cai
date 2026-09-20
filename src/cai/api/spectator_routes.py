# -*- coding: utf-8 -*-
"""旁观 Agent API 集成层（specs/001-spectator-agent/contracts/api.md）。

新增端点（挂 /api/v1, 复用既有鉴权; 不修改任何既有端点与 SSE 协议——宪法 I）:
- GET  /api/v1/sessions/{session_id}/spectator/card          查询活跃卡片
- POST /api/v1/sessions/{session_id}/spectator/card/accept   确认开始学习
- POST /api/v1/sessions/{session_id}/spectator/card/decline  拒绝推荐

本模块属于集成层（运行于 WSL ~/cai 内）, 可 import cai 模块;
核心逻辑在 spectator 包内（无 CAI 依赖, Windows 可测）。
挂载方式见 patch_spectator_app.py。
"""

from __future__ import annotations

import logging
import os
import asyncio
from pathlib import Path
from typing import Any, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from cai.spectator.config import SpectatorConfig
from cai.spectator.evaluator import LLMIntentEvaluator, make_litellm_completer
from cai.spectator.logging_utils import SpectatorLogger
from cai.spectator.matcher import QuestionDoc, QuestionSearcher
from cai.spectator.provider_config import (
    DEFAULT_PROVIDER_FILE,
    ProviderConfig,
    apply_changes,
    is_complete,
    load_provider_config,
    mask_key,
    save_provider_config,
)
from cai.spectator.service import CardStateError, SpectatorService

logger = logging.getLogger("cai.spectator.routes")

router = APIRouter(prefix="/api/v1/sessions", tags=["spectator"])
config_router = APIRouter(prefix="/api/v1/spectator", tags=["spectator"])

ConversationTurn = Tuple[str, str]

# ── 服务单例（进程内）────────────────────────────────────────────────────────

_service: Optional[SpectatorService] = None
_round_counter: dict[str, int] = {}
_observer_tasks: dict[str, asyncio.Task] = {}
_MAX_TRACKED_SESSIONS = 500  # 简单防泄漏: 只保留最近 N 个会话的计数

_PROVIDER_FILE_ENV = "CAI_SPECTATOR_PROVIDER_FILE"


def _provider_file() -> Path:
    import os  # noqa: PLC0415

    override = os.getenv(_PROVIDER_FILE_ENV)
    return Path(override) if override else DEFAULT_PROVIDER_FILE


def get_spectator_service() -> SpectatorService:
    """构造/返回旁观服务单例。

    T033 手动配置模式: 评估 provider 只来自 UI 窗口配置文件
    （~/.cai/spectator_provider.json）——不回退 .env、不借用会话
    provider、不读任何环境变量。三项不全时钩子层拦截（degraded:
    provider_not_configured）。
    """
    global _service
    if _service is None:
        config = SpectatorConfig.from_env()
        stored = load_provider_config(_provider_file())
        _service = SpectatorService(
            config=config,
            evaluator=_build_evaluator(stored, config),
            searcher=PgQuestionSearcher(),
            logger=SpectatorLogger(),
        )
        object.__setattr__(_service, "_stored_provider", stored)
    return _service


def _build_evaluator(
    stored: Optional[ProviderConfig], config: SpectatorConfig
) -> LLMIntentEvaluator:
    """按 UI 存储配置构造评估器。

    stored 为 None（未配置）时字段按 None 处理——此时钩子层不会放行
    评估; 占位模型名仅为构造完整, 不完整时不会被真正调用。
    """
    stored = stored if stored is not None else ProviderConfig()
    return LLMIntentEvaluator(
        completer=make_litellm_completer(
            stored.model or "unconfigured/placeholder",
            api_base=stored.api_base,
            api_key=stored.api_key,
        ),
        timeout_seconds=config.eval_timeout_seconds,
    )


def _rebuild_evaluator(service: SpectatorService) -> None:
    """provider 变更后热重建评估器（无需重启服务）。"""
    stored = load_provider_config(_provider_file())
    service.evaluator = _build_evaluator(stored, service.config)
    object.__setattr__(service, "_stored_provider", stored)


# ── SessionManager 依赖（与 app.py 同模式: request.app.state）───────────────


def session_manager_dep(request: Request):
    """从 app.state 取 SessionManager（与 app.py 内 _session_manager_dependency 一致）。"""
    return request.app.state.session_manager


# ── PG 检索实现方（契约 module.md §5）───────────────────────────────────────


class PgQuestionSearcher:
    """从 PostgreSQL 拉取旁观匹配用的轻量题目投影（只读）。"""

    async def usable_questions(self) -> List[QuestionDoc]:
        return await self._load_questions(recall_test=False)

    async def recall_test_questions(self) -> List[QuestionDoc]:
        """显式召回测试模式：包含 usable 与 incomplete 题目。"""
        return await self._load_questions(recall_test=True)

    async def _load_questions(self, *, recall_test: bool) -> List[QuestionDoc]:
        from cai.question_bank.repository import QuestionRepository  # noqa: PLC0415

        repo = QuestionRepository()
        try:
            docs: List[QuestionDoc] = []
            async with repo.session() as sess:
                async with sess.begin():
                    questions, _total = await repo.list_questions(
                        sess, status=None if recall_test else "usable", limit=1000
                    )
                    for q in questions:
                        if recall_test and q.status not in ("usable", "incomplete"):
                            continue
                        metadata = (
                            q.recall_metadata
                            if isinstance(q.recall_metadata, dict)
                            else {}
                        )
                        method = await repo.get_usable_solution(sess, q.question_id)
                        if method is None and not recall_test:
                            continue  # 防御: 无 usable 方法不推荐（FR-006）
                        title = metadata.get("title") or q.statement_raw or ""
                        recall_text = _merge_recall_metadata(metadata)
                        docs.append(
                            QuestionDoc(
                                question_id=str(q.question_id),
                                statement=str(title),
                                principles_text=_merge_principles(method),
                                recall_text=recall_text,
                                learnable=method is not None,
                                disabled=method is None,
                                disabled_reason=(
                                    "仅召回测试：尚无 usable 解法"
                                    if method is None
                                    else ""
                                ),
                            )
                        )
            return docs
        finally:
            await repo.close()


def _merge_principles(method) -> str:
    """overall_approach + principles(JSONB) 合并为匹配用文本。"""
    if method is None:
        return ""
    parts: List[str] = []
    if method.overall_approach:
        parts.append(str(method.overall_approach))
    principles = method.principles
    if isinstance(principles, dict):
        parts.extend(str(v) for v in principles.values() if v)
    elif isinstance(principles, list):
        parts.extend(str(v) for v in principles)
    elif principles:
        parts.append(str(principles))
    return " ".join(parts)


def _merge_recall_metadata(metadata: dict) -> str:
    """仅合并召回元数据；不读取或生成任何解法内容。"""
    parts: List[str] = []
    for field in ("aliases", "keywords", "recall_queries"):
        values = metadata.get(field, [])
        if isinstance(values, list):
            parts.extend(str(value) for value in values if value)
    return " ".join(parts)


# ── 请求/响应模型 ───────────────────────────────────────────────────────────


class AcceptRequest(BaseModel):
    card_id: str
    question_id: str


class DeclineRequest(BaseModel):
    card_id: str


class FailureCardRequest(BaseModel):
    card_id: str


class AcceptResponse(BaseModel):
    status: str = "accepted"
    question_id: str
    solution_method_id: Optional[str] = None
    teaching_prompt_hint: str = "开始学习这道题"


class DeclineResponse(BaseModel):
    status: str = "rejected"


class AbandonResponse(BaseModel):
    status: str = "abandoned"


# ── 端点 ────────────────────────────────────────────────────────────────────


@router.get("/{session_id}/spectator/card")
async def get_spectator_card(
    session_id: str,
    manager=Depends(session_manager_dep),
):
    """查询活跃推荐卡片。无卡片/功能关闭/内部异常 → 204（轮询方无感降级）。"""
    from cai.api.sessions import SessionNotFoundError  # noqa: PLC0415

    service = get_spectator_service()
    if not service.config.enabled:
        return _no_content()
    try:
        manager.get_session(session_id)
    except SessionNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    card = service.get_card(session_id)
    if card is None:
        return _no_content()
    return _card_response(card)


def _card_response(card):
    return {
        "card_id": card.card_id,
        "kind": card.kind,
        "created_at_round": card.created_at_round,
        "topic_summary": card.topic_summary,
        "keywords": card.keywords,
        "candidates": [c.model_dump() for c in card.candidates],
        "error_message": card.error_message,
        "retryable": card.retryable,
    }


@router.post("/{session_id}/spectator/card/retry")
async def retry_spectator_card(
    session_id: str,
    payload: FailureCardRequest,
    manager=Depends(session_manager_dep),
):
    """使用失败时保存的对话窗口重试旁观模型连接与评估。"""
    from cai.api.sessions import SessionNotFoundError  # noqa: PLC0415

    try:
        manager.get_session(session_id)
    except SessionNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        card = await get_spectator_service().retry_failed(session_id, payload.card_id)
    except CardStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if card is None:
        return _no_content()
    return _card_response(card)


@router.post(
    "/{session_id}/spectator/card/abandon", response_model=AbandonResponse
)
async def abandon_spectator_card(
    session_id: str,
    payload: FailureCardRequest,
    manager=Depends(session_manager_dep),
):
    """放弃失败卡片，不再自动重试本轮评估。"""
    from cai.api.sessions import SessionNotFoundError  # noqa: PLC0415

    try:
        manager.get_session(session_id)
    except SessionNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        await get_spectator_service().abandon_failed(session_id, payload.card_id)
    except CardStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return AbandonResponse()


@router.post("/{session_id}/spectator/card/accept", response_model=AcceptResponse)
async def accept_spectator_card(
    session_id: str,
    payload: AcceptRequest,
    manager=Depends(session_manager_dep),
):
    """用户显式确认「开始学习」: 绑定会话（等价显式题目命中）, 不产生讲解内容。

    讲解由前端随后发送 teaching_prompt_hint 普通消息触发, 走既有教学路径
    （宪法 IV/VII: 复用 explain 路径）。
    """
    from cai.api.sessions import SessionNotFoundError  # noqa: PLC0415

    service = get_spectator_service()
    if not service.config.enabled:
        raise HTTPException(status_code=409, detail="Spectator disabled")

    try:
        manager.get_session(session_id)
    except SessionNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")

    # 先验证 usable 方法，验证失败时绝不改变卡片状态或绑定会话。
    try:
        method = await _lookup_usable_solution(payload.question_id)
    except Exception:  # noqa: BLE001 —— 无法确认时 fail closed
        logger.warning("spectator accept: usable solution lookup failed", exc_info=True)
        raise HTTPException(status_code=503, detail="题库不可用，无法确认学习候选")
    if method is None:
        raise HTTPException(
            status_code=409,
            detail="该候选仅用于召回测试，不可学习：尚无 usable 解法",
        )

    try:
        result = await service.accept(session_id, payload.card_id, payload.question_id)
    except CardStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    solution_method_id = str(method.solution_id)

    # 写 SessionState 绑定字段（与 patch_v2 显式命中路径完全一致）
    session = manager.get_session(session_id)
    session.question_id = result.question_id
    session.solution_method_id = solution_method_id
    session.learning_mode = "explain"
    session.resolution_status = "matched"
    session.resolution_locked = True

    return AcceptResponse(
        question_id=result.question_id,
        solution_method_id=solution_method_id,
    )


async def _lookup_usable_solution(question_id: str):
    """读取可讲解解法；异常向上抛出让 accept 路径 fail closed。"""
    import uuid as _uuid

    from cai.question_bank.repository import QuestionRepository  # noqa: PLC0415

    try:
        parsed_id = _uuid.UUID(question_id)
    except (TypeError, ValueError):
        return None

    repo = QuestionRepository()
    try:
        async with repo.session() as sess:
            async with sess.begin():
                return await repo.get_usable_solution(sess, parsed_id)
    finally:
        await repo.close()


@router.post("/{session_id}/spectator/card/decline", response_model=DeclineResponse)
async def decline_spectator_card(
    session_id: str,
    payload: DeclineRequest,
    manager=Depends(session_manager_dep),
):
    """用户显式拒绝: 卡片 → rejected, 全候选进入会话级冷却。"""
    from cai.api.sessions import SessionNotFoundError  # noqa: PLC0415

    service = get_spectator_service()
    if not service.config.enabled:
        raise HTTPException(status_code=409, detail="Spectator disabled")
    try:
        manager.get_session(session_id)
    except SessionNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        await service.decline(session_id, payload.card_id)
    except CardStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return DeclineResponse()


# ── Provider 配置端点（UI 填写窗口 ↔ 后端, T029/T030）───────────────────────


class ProviderConfigRequest(BaseModel):
    """UI 配置提交体。

    字段语义: 缺省 = 不变; 空串 = 清除（回退 env 链）; 非空 = 设置。
    api_key 留空提交时省略该字段即"保持不变"（前端不会回显明文）。
    """

    api_base: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None


class ProviderConfigResponse(BaseModel):
    """配置回显——绝不包含明文密钥（宪法安全条款）。

    T033: 无 env 回退——configured=false 表示三项未填全, 评估不会运行。
    """

    enabled: bool
    recall_test_mode: bool = False
    interval_rounds: int
    window_messages: int
    configured: bool
    api_base: Optional[str] = None
    model: Optional[str] = None
    api_key_configured: bool = False
    api_key_masked: Optional[str] = None
    missing: List[str] = Field(default_factory=list)


def _config_response(service: SpectatorService) -> ProviderConfigResponse:
    stored = load_provider_config(_provider_file())
    stored = stored if stored is not None else ProviderConfig()
    return ProviderConfigResponse(
        enabled=service.config.enabled,
        recall_test_mode=service.config.recall_test_mode,
        interval_rounds=service.config.interval_rounds,
        window_messages=service.config.window_messages,
        configured=is_complete(stored),
        api_base=stored.api_base,
        model=stored.model,
        api_key_configured=bool(stored.api_key),
        api_key_masked=mask_key(stored.api_key),
        missing=stored.missing_fields(),
    )


@config_router.get("/config", response_model=ProviderConfigResponse)
async def get_provider_config():
    """回显当前旁观 provider 配置（手动模式: 仅 UI 配置, 密钥脱敏）。"""
    service = get_spectator_service()
    return _config_response(service)


@config_router.post("/config", response_model=ProviderConfigResponse)
async def update_provider_config(payload: ProviderConfigRequest):
    """保存 UI 填写的旁观 provider 配置并热重建评估器。

    持久化到 ~/.cai/spectator_provider.json（0600）;
    密钥不回显、不写日志。写入失败 → 500（明确报错, 不静默丢配置）。
    """
    service = get_spectator_service()  # 确保单例存在
    stored = load_provider_config(_provider_file())
    changes = {k: v for k, v in payload.model_dump(exclude_none=True).items()}
    updated = apply_changes(stored, **changes)  # type: ignore[arg-type]
    try:
        save_provider_config(_provider_file(), updated)
    except Exception as exc:  # noqa: BLE001
        logger.error("spectator provider config save failed", exc_info=True)
        raise HTTPException(status_code=500, detail="配置保存失败：" + str(exc)[:120])

    _rebuild_evaluator(service)
    logger.info(
        "spectator provider config updated (api_base=%s, model=%s, key=%s, complete=%s)",
        "set" if updated.api_base else "cleared",
        "set" if updated.model else "cleared",
        "set" if updated.api_key else "cleared",
        is_complete(updated),
    )
    return _config_response(service)


# ── 消息流完成钩子（由 patch_spectator_app.py 注入调用）────────────────────


def spectator_after_round(session, session_id: str, payload_input) -> None:
    """每轮消息流完成后由 app.py 钩子调用（同步签名, 内部起后台 task）。

    - 轮次计数: 本模块维护（对 history 格式零假设, 稳健）
    - 评估窗口: 尽力从 session.history 抽取 (role, content); 失败则退化为
      仅当前用户消息
    - 永不抛出（宪法 VI: 旁观不干扰主对话）
    """
    try:
        service = get_spectator_service()
        if not service.config.enabled:
            return
        # FR-011: 仅当会话真正绑定到题库题目（讲解模式, question_id 非空）时停用。
        # 注意不能用 resolution_locked——它在对题库 resolution 尝试后无论命中与否
        # 都会被置位（含 unmatched/insufficient）；普通 WebUI 首轮始终发送 chat，
        # 只有显式 question_statement 请求才触发定位，用它判断会误停旁观推荐。
        session_bound = bool(getattr(session, "question_id", None))
        _round_counter[session_id] = _round_counter.get(session_id, 0) + 1
        _prune_counter()
        round_count = _round_counter[session_id]

        # T033 手动配置模式: 三项（BASE URL/密钥/模型）不全 → 不评估。
        # 不猜凭据、不回退 .env——明确降级并记录缺失字段。
        stored = load_provider_config(_provider_file())
        if not is_complete(stored):
            service.logger.log(
                "degraded",
                session_id,
                round_count,
                {
                    "reason": "provider_not_configured",
                    "missing": stored.missing_fields()
                    if stored is not None
                    else ["api_base", "api_key", "model"],
                },
            )
            return

        window = _extract_window(session, service.config.window_messages, payload_input)
        _schedule_observation(
            service,
            session_id,
            window,
            session_bound,
            round_count,
        )
    except Exception:  # noqa: BLE001
        logger.debug("spectator_after_round failed silently", exc_info=True)


def _prune_counter() -> None:
    if len(_round_counter) > _MAX_TRACKED_SESSIONS:
        # 保留最近的一半（dict 保序 → 丢最旧的键）
        for key in list(_round_counter)[: len(_round_counter) // 2]:
            _round_counter.pop(key, None)


def _schedule_observation(
    service: SpectatorService,
    session_id: str,
    window: List[ConversationTurn],
    session_bound: bool,
    round_count: int,
) -> None:
    """Run the latest spectator evaluation without blocking the main response."""
    previous = _observer_tasks.get(session_id)
    if previous is not None and not previous.done():
        previous.cancel()

    task = asyncio.get_running_loop().create_task(
        service.observe_round(session_id, window, session_bound, round_count)
    )
    _observer_tasks[session_id] = task

    def _cleanup(done: asyncio.Task) -> None:
        if _observer_tasks.get(session_id) is done:
            _observer_tasks.pop(session_id, None)
        try:
            done.exception()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("spectator observation failed silently", exc_info=True)

    task.add_done_callback(_cleanup)


def _extract_window(
    session, window_messages: int, payload_input
) -> List[ConversationTurn]:
    """Extract one sliding user/assistant message window without duplication."""
    turns: List[ConversationTurn] = []
    history = getattr(session, "history", None)
    if isinstance(history, (list, tuple)):
        for item in history:
            if isinstance(item, dict):
                role = str(item.get("role", ""))
                content = _message_text(item.get("content", ""))
                if role in ("user", "assistant") and content:
                    turns.append((role, content))
            elif isinstance(item, str):
                turns.append(("user", item))
    payload_text = _message_text(payload_input)
    recent_has_payload = any(
        role == "user" and content == payload_text for role, content in turns[-2:]
    )
    if payload_text and not recent_has_payload:
        turns.append(("user", payload_text))
    return turns[-window_messages:]


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(part.strip() for part in parts if part.strip())
    return str(content).strip() if content is not None else ""


# ── 辅助 ────────────────────────────────────────────────────────────────────


def _no_content():
    from fastapi.responses import Response  # noqa: PLC0415

    return Response(status_code=204)
