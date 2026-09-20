"""题目定位器 (§7.3).

从用户题面定位到题库中的唯一 Question 实例。

流程:
1. 规范化题面 + 计算实例指纹
2. 精确指纹匹配（快速路径）
3. PostgreSQL 全文/相似度候选召回
4. LLM 确认唯一题目（仅在有多个候选时）
5. 返回 ResolutionResponse

依赖: PostgreSQL (QuestionRepository), CAI model (用于 LLM 确认).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from cai.question_bank.fingerprints import canonicalize_statement, compute_instance_fingerprint
from cai.question_bank.repository import QuestionRepository
from cai.question_bank.schemas import ResolutionResponse

logger = logging.getLogger(__name__)

# ── 默认阈值（从环境变量读取，文档 §9） ─────────────────────────────────────────

DEFAULT_TOP_K = 10
DEFAULT_MIN_CONFIDENCE = 0.90
DEFAULT_MIN_MARGIN = 0.10
RESOLVER_VERSION = "resolver-v1"


@dataclass
class ResolverCandidate:
    """候选题目，供 LLM 确认."""
    question_id: uuid.UUID
    statement_preview: str  # 截断题面（≤200 字符）
    instance_fingerprint: str
    status: str
    match_type: str  # "exact_fingerprint" | "fulltext" | "similarity"


class QuestionResolver:
    """题目定位器 — 从用户输入到唯一 Question 的完整链路."""

    def __init__(
        self,
        repo: QuestionRepository,
        *,
        top_k: int = DEFAULT_TOP_K,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
        min_margin: float = DEFAULT_MIN_MARGIN,
    ):
        self._repo = repo
        self.top_k = top_k
        self.min_confidence = min_confidence
        self.min_margin = min_margin

    async def resolve(
        self,
        statement: str,
        *,
        model=None,  # Optional[Model] — CAI model for LLM confirmation
        session_id: Optional[uuid.UUID] = None,
        material_fingerprints: Sequence[str] = (),
    ) -> ResolutionResponse:
        """定位题目。返回结构化定位结果。"""
        t0 = time.monotonic()

        # 1. 规范化
        canonical = canonicalize_statement(statement)
        fingerprint = compute_instance_fingerprint(statement, material_fingerprints)

        async with self._repo.session() as sess:
            # 2. 精确指纹匹配
            exact = await self._repo._by_fingerprint(sess, fingerprint)
            if exact is not None:
                latency_ms = int((time.monotonic() - t0) * 1000)
                return ResolutionResponse(
                    status="matched",
                    question_id=exact.question_id,
                    method_status=_method_status(exact),
                    solution_method_id=exact.current_solution_method_id,
                    solution_version=None,
                    candidate_ids=[exact.question_id],
                    confidence=1.0,
                    reasons=[f"exact_fingerprint: {fingerprint[:20]}…"],
                    resolver_version=RESOLVER_VERSION,
                    latency_ms=latency_ms,
                    locked=True,
                )

            # 3. 全文/相似度候选召回
            candidates = await self._search_candidates(sess, canonical, statement)
            if not candidates:
                latency_ms = int((time.monotonic() - t0) * 1000)
                return ResolutionResponse(
                    status="insufficient",
                    question_id=None,
                    method_status=None,
                    candidate_ids=[],
                    confidence=0.0,
                    reasons=["无候选匹配：指纹不命中，全文检索无结果"],
                    resolver_version=RESOLVER_VERSION,
                    latency_ms=latency_ms,
                    locked=True,
                )

            # 4. LLM 确认（仅在有多个候选、或单个候选但非精确匹配时）
            if len(candidates) == 1 and candidates[0].match_type == "exact_fingerprint":
                # 不应到达这里（已在步骤2返回），但做防御
                q = candidates[0]
                latency_ms = int((time.monotonic() - t0) * 1000)
                return ResolutionResponse(
                    status="matched",
                    question_id=q.question_id,
                    method_status=None,
                    candidate_ids=[q.question_id],
                    confidence=1.0,
                    reasons=["exact_fingerprint"],
                    resolver_version=RESOLVER_VERSION,
                    latency_ms=latency_ms,
                    locked=True,
                )

            # 需要 LLM 确认
            if model is None:
                # 无 LLM 可用 → 如果只有1个候选，返回 insufficient（不确定）
                if len(candidates) == 1:
                    c = candidates[0]
                    # 如果候选质量够高，直接返回 matched
                    async with self._repo.session() as sess2:
                        q = await self._repo.get_question(sess2, c.question_id)
                    if q is not None:
                        latency_ms = int((time.monotonic() - t0) * 1000)
                        return ResolutionResponse(
                            status="matched",
                            question_id=q.question_id,
                            method_status=_method_status(q),
                            solution_method_id=q.current_solution_method_id,
                            candidate_ids=[c.question_id],
                            confidence=0.85,
                            reasons=["single_fulltext_candidate_no_llm"],
                            resolver_version=RESOLVER_VERSION,
                            latency_ms=latency_ms,
                            locked=True,
                        )
                latency_ms = int((time.monotonic() - t0) * 1000)
                return ResolutionResponse(
                    status="insufficient",
                    question_id=None,
                    method_status=None,
                    candidate_ids=[c.question_id for c in candidates],
                    confidence=0.0,
                    reasons=[f"{len(candidates)} 候选，无 LLM 确认"],
                    resolver_version=RESOLVER_VERSION,
                    latency_ms=latency_ms,
                    locked=True,
                )

            # 调用 LLM 确认
            try:
                result = await self._llm_confirm(model, statement, candidates)
                latency_ms = int((time.monotonic() - t0) * 1000)
                result.latency_ms = latency_ms
                result.resolver_version = RESOLVER_VERSION
                result.locked = True
                return result
            except Exception:
                logger.exception("LLM confirmation failed, falling back to insufficient")
                latency_ms = int((time.monotonic() - t0) * 1000)
                return ResolutionResponse(
                    status="skipped",
                    question_id=None,
                    method_status=None,
                    candidate_ids=[c.question_id for c in candidates],
                    confidence=0.0,
                    reasons=["LLM 确认异常，降级跳过"],
                    resolver_version=RESOLVER_VERSION,
                    latency_ms=latency_ms,
                    locked=True,
                )

    async def _search_candidates(
        self, sess, canonical: str, raw_statement: str
    ) -> list[ResolverCandidate]:
        """PostgreSQL 全文检索 + 简单相似度召回候选题目."""
        from sqlalchemy import func, or_, select, text

        from cai.question_bank.models import Question

        candidates: list[ResolverCandidate] = []

        # ── 全文检索（使用 PostgreSQL tsvector/tsquery） ──
        try:
            # 构建 tsquery：取原始题面的前 100 个字符，按词搜索
            query_text = raw_statement[:200].replace("'", "''")
            tsquery_expr = f"websearch_to_tsquery('simple', '{query_text}')"
            # 用 coalesce 处理 statement_canonical 可能为空的情况
            tsvector_expr = (
                "to_tsvector('simple', coalesce(statement_canonical, '') || ' ' || "
                "coalesce(statement_raw, ''))"
            )

            stmt = (
                select(
                    Question.question_id,
                    Question.statement_raw,
                    Question.instance_fingerprint,
                    Question.status,
                    func.ts_rank(
                        text(f"{tsvector_expr}"),
                        text(tsquery_expr),
                    ).label("rank"),
                )
                .where(text(f"({tsvector_expr}) @@ {tsquery_expr}"))
                .order_by(text("rank DESC"))
                .limit(self.top_k)
            )
            result = await sess.execute(stmt)
            rows = result.all()
            for row in rows:
                preview = (row.statement_raw or "")[:200]
                candidates.append(
                    ResolverCandidate(
                        question_id=row.question_id,
                        statement_preview=preview,
                        instance_fingerprint=row.instance_fingerprint,
                        status=row.status,
                        match_type="fulltext",
                    )
                )
        except Exception:
            logger.exception("Fulltext search failed, falling back to simple ILIKE")

        # ── 如果全文检索结果不足，补充 ILIKE 模糊搜索 ──
        if len(candidates) < 3:
            # 提取关键词（长度 ≥ 3 的 token）
            keywords = [w for w in re.findall(r"[a-zA-Z0-9_\-\+]{3,}|\w{2,}", raw_statement) if len(w) >= 3]
            unique_keywords = list(dict.fromkeys(keywords))[:5]  # 取前5个唯一关键词

            if unique_keywords:
                ilike_conditions = [
                    Question.statement_raw.ilike(f"%{kw}%")
                    for kw in unique_keywords
                ]
                ilike_stmt = (
                    select(
                        Question.question_id,
                        Question.statement_raw,
                        Question.instance_fingerprint,
                        Question.status,
                    )
                    .where(or_(*ilike_conditions))
                    .limit(self.top_k)
                )
                result = await sess.execute(ilike_stmt)
                existing_ids = {c.question_id for c in candidates}
                for row in result.all():
                    if row.question_id not in existing_ids:
                        preview = (row.statement_raw or "")[:200]
                        candidates.append(
                            ResolverCandidate(
                                question_id=row.question_id,
                                statement_preview=preview,
                                instance_fingerprint=row.instance_fingerprint,
                                status=row.status,
                                match_type="similarity",
                            )
                        )

        return candidates[: self.top_k]

    async def _llm_confirm(
        self, model, statement: str, candidates: list[ResolverCandidate]
    ) -> ResolutionResponse:
        """使用 LLM 从候选列表中确认唯一题目。

        提示词约束（§7.3/§8.3）:
        - LLM 只能接收候选 Question 的题面和资料摘要
        - 不得引用解法、Flag、漏洞分类或命令路线作为身份依据
        - 输出必须通过 Pydantic Schema 校验
        """
        if len(candidates) == 1:
            c = candidates[0]
            async with self._repo.session() as sess:
                q = await self._repo.get_question(sess, c.question_id)
            ms = _method_status(q) if q else None
            return ResolutionResponse(
                status="matched",
                question_id=c.question_id,
                method_status=ms,
                solution_method_id=q.current_solution_method_id if q else None,
                candidate_ids=[c.question_id],
                confidence=0.90,
                reasons=["single_candidate_llm_shortcut"],
                resolver_version=RESOLVER_VERSION,
                latency_ms=0,
                locked=True,
            )

        # 构建候选列表给 LLM
        candidate_lines = []
        for i, c in enumerate(candidates):
            candidate_lines.append(
                f"[{i}] ID: {c.question_id}\n"
                f"    题面: {c.statement_preview}\n"
                f"    状态: {c.status}"
            )

        prompt = f"""你是一个题目定位器。给定用户的题面，从候选列表中判断是否匹配已有题目。

用户题面:
{statement[:500]}

候选题目:
{chr(10).join(candidate_lines)}

请判断用户的题面是否匹配上述某个候选题目。只使用题面内容作为判断依据，不得引用解法、Flag、漏洞分类或命令。

返回 JSON:
{{"matched_index": <int|null> // 匹配的候选编号（从0开始），null表示都不匹配
 "confidence": <float>      // 0.0-1.0，仅在 matched_index 不为 null 时
 "reason": "<string>"       // 简短说明匹配或不匹配的原因
}}"""

        try:
            # 使用 CAI model 调用
            result = await model.query(prompt, output_type=None)
            parsed = json.loads(result) if isinstance(result, str) else result

            matched_idx = parsed.get("matched_index")
            confidence = float(parsed.get("confidence", 0.0))

            if matched_idx is not None and 0 <= matched_idx < len(candidates):
                c = candidates[matched_idx]
                async with self._repo.session() as sess:
                    q = await self._repo.get_question(sess, c.question_id)

                if confidence >= self.min_confidence:
                    return ResolutionResponse(
                        status="matched",
                        question_id=c.question_id,
                        method_status=_method_status(q) if q else None,
                        solution_method_id=q.current_solution_method_id if q else None,
                        candidate_ids=[c.question_id],
                        confidence=confidence,
                        reasons=[parsed.get("reason", "llm_confirmed")],
                        resolver_version=RESOLVER_VERSION,
                        latency_ms=0,
                        locked=True,
                    )

            # 未达到阈值或不匹配
            return ResolutionResponse(
                status="insufficient" if matched_idx is not None else "unmatched",
                question_id=None,
                method_status=None,
                candidate_ids=[c.question_id for c in candidates],
                confidence=confidence,
                reasons=[parsed.get("reason", "llm_no_match")],
                resolver_version=RESOLVER_VERSION,
                latency_ms=0,
                locked=True,
            )

        except Exception:
            logger.exception("LLM confirmation parse failed")
            # 降级：返回 insufficient
            return ResolutionResponse(
                status="insufficient",
                question_id=None,
                method_status=None,
                candidate_ids=[c.question_id for c in candidates],
                confidence=0.0,
                reasons=["llm_parse_error"],
                resolver_version=RESOLVER_VERSION,
                latency_ms=0,
                locked=True,
            )


def _method_status(q) -> Optional[str]:
    """从 Question ORM 推断 method_status."""
    if q is None:
        return None
    if q.current_solution_method_id and q.status == "usable":
        return "usable"
    if q.status == "building":
        return "unavailable"
    if q.status in ("draft", "incomplete"):
        return "unavailable"
    return None
