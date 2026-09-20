"""Question bank builder for the CAI API.

Turns a "problem + reference materials" into a reusable question-bank entry:

- **Stage 1 (reproduction)**: run the existing CAI agent on the problem, with the
  reference materials injected into its system prompt. Preserve the real run
  trace (tool calls, outputs, messages, final result).
- **Stage 2 (summarization)**: run a dedicated summarizer agent over the real
  reproduction to produce an overall approach and a small set of large steps
  (goal / reason / principle / action / result).

Nothing here reimplements the agent loop; it reuses ``Runner``, ``Agent`` and the
existing tool set (FR-02).
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from cai.agents import get_agent_by_name
from cai.sdk.agents.agent import Agent
from cai.sdk.agents.run import Runner
from cai.util import get_session_logs_dir, update_agent_models_recursively


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class BankBuildRequest(BaseModel):
    problem: str
    references: list[str] = Field(default_factory=list)
    agent: str | None = None          # internal agent id, e.g. "one_tool_agent"
    model: str | None = None          # model string, e.g. "anthropic/claude-3-5-sonnet-20240620"
    max_turns: int = 20
    base_url: str | None = None       # optional per-build provider override
    api_key: str | None = None


class BankBuildResponse(BaseModel):
    bank_id: str
    status: str                       # "completed" | "incomplete"
    incomplete_reason: str | None = None
    problem: str
    references: list[str]
    reproduction: dict[str, Any]      # summarize_run_result() output (real trace)
    overall_approach: str | None
    steps: list[dict[str, Any]] | None


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
def bank_dir() -> Path:
    """Directory holding one JSON file per built question-bank entry."""
    d = get_session_logs_dir().parent / "question_bank"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _bank_path(bank_id: str) -> Path:
    return bank_dir() / f"{bank_id}.json"


def save_bank(entry: BankBuildResponse) -> None:
    _bank_path(entry.bank_id).write_text(
        entry.model_dump_json(indent=2), encoding="utf-8"
    )


def load_bank(bank_id: str) -> BankBuildResponse | None:
    p = _bank_path(bank_id)
    if not p.exists():
        return None
    return BankBuildResponse.model_validate_json(p.read_text(encoding="utf-8"))


def list_banks() -> list[dict[str, Any]]:
    """Return [{bank_id, status, problem, created_at}] for all saved entries."""
    out = []
    for p in sorted(bank_dir().glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append(
            {
                "bank_id": data.get("bank_id", p.stem),
                "status": data.get("status"),
                "problem": (data.get("problem") or "")[:120],
                "created_at": data.get("created_at"),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Stage 1: reproduction
# ---------------------------------------------------------------------------
def _make_repro_instructions(original_instructions: Any):
    """Return a callable that injects problem + references into the system prompt.

    The callable reads the run context (set by the API layer when calling
    ``Runner.run(context={...})``) and composes a system prompt from the
    original agent instructions plus the injected materials.
    """

    def repro_instructions(run_context, agent) -> str:  # type: ignore[no-untyped-def]
        ctx = getattr(run_context, "context", None) or {}
        problem = ctx.get("problem") or ""
        references = ctx.get("references") or []

        base = ""
        if isinstance(original_instructions, str):
            base = original_instructions
        elif callable(original_instructions):
            try:
                base = original_instructions(run_context, agent)
            except Exception:
                base = ""

        parts = [base]
        if problem:
            parts.append("\n\n## 题目\n" + problem)
        if references:
            blocks = [
                f"\n\n### 参考资料 {i + 1}\n{ref}" for i, ref in enumerate(references)
            ]
            parts.append("".join(blocks))
        if references or problem:
            parts.append(
                "\n\n请先完整阅读题目和参考资料，然后独立完成解题过程。"
                "记录你实际执行的每个动作及其真实输出。"
                "请始终使用中文进行思考和输出。"
            )
        return "\n".join(p for p in parts if p)

    return repro_instructions


async def _run_reproduction(payload: BankBuildRequest) -> tuple[Agent, dict[str, Any]]:
    """Run stage 1: reproduce the solution with the reference materials available.

    Returns (agent, reproduction_dict). Raises on agent/model errors.
    """
    agent_name = payload.agent or "one_tool_agent"
    model = payload.model or os.getenv("CAI_MODEL") or ""

    agent = get_agent_by_name(agent_name)
    if model:
        update_agent_models_recursively(agent, model)
    # Per-build provider override (same mechanism as sessions).
    if payload.base_url is not None or payload.api_key is not None:
        from cai.util import apply_provider_to_agent

        apply_provider_to_agent(agent, payload.base_url, payload.api_key)

    # Wrap instructions to inject problem + references into the system prompt.
    repro_agent = agent.clone(instructions=_make_repro_instructions(agent.instructions))

    context = {
        "problem": payload.problem,
        "references": payload.references,
    }
    # Timeout guards against a slow/flaky provider hanging the whole build.
    # max_turns bounds agent work; the wall-clock cap is a safety net.
    result = await asyncio.wait_for(
        Runner.run(
            repro_agent,
            payload.problem,
            context=context,
            max_turns=payload.max_turns,
        ),
        timeout=max(120, payload.max_turns * 60),
    )
    from cai.api.sessions import summarize_run_result

    return repro_agent, summarize_run_result(result)


def _is_complete(reproduction: dict[str, Any]) -> tuple[bool, str | None]:
    """Decide whether the reproduction actually reached a result (FR-06).

    Returns (complete, reason). A final_output that is empty or explicitly an
    error marks the reproduction incomplete.
    """
    final = reproduction.get("final_output")
    if final is None or final == "" or final == [] or final == {}:
        return False, "复现未产生最终输出（final_output 为空）"
    if isinstance(final, dict) and final.get("type") == "error":
        return False, f"复现以错误结束：{final.get('message', '')}"
    if isinstance(final, str) and final.strip().lower() in (
        "error", "failed", "失败", "错误", "unable to complete",
    ):
        return False, "复现未能完成解题"
    return True, None


# ---------------------------------------------------------------------------
# Stage 2: summarization
# ---------------------------------------------------------------------------
class BigStepModel(BaseModel):
    title: str
    goal: str        # 这一步要解决什么问题
    reason: str      # 为什么此时需要这一步
    principle: str   # 这一步依赖什么机制或知识
    action: str      # CAI 实际进行了什么操作（可追溯到复现记录）
    result: str      # CAI 得到了什么关键输出或结论


class BigStepsModel(BaseModel):
    overall_approach: str = Field(description="整体解题思路（一段话）")
    steps: list[BigStepModel] = Field(
        description="少量有明确目的的大步骤（不要把每次 tool call 当一步）"
    )


_SUMMARIZE_INSTRUCTIONS = """\
你是题库总结器。给定一道题目的题面、参考资料，以及 CAI 独立复现该题的真实过程记录，\
你需要输出结构化的解题说明。

严格要求：
1. 只能基于提供的真实复现记录来写"动作"和"结果"，不得编造未发生的操作或证据。
2. "整体思路"用一段话描述解法的主要路线，让读者不读完整 Trace 也能理解。
3. 把过程划分为少量（3-6 个）有明确目的的大步骤。大步骤表达解题阶段，\
不要把每次 tool call 机械地当成一个步骤。
4. 每个大步骤说明：目标（解决什么问题）、原因（为什么此时需要）、\
原理（依赖什么机制/知识）、动作（CAI 实际操作）、结果（关键输出或结论）。\
其中"动作"和"结果"必须能追溯到复现记录。

输出格式：只输出一个 JSON 对象，不要输出任何多余文字。JSON 结构如下：
{
  "overall_approach": "整体解题思路（一段话）",
  "steps": [
    {
      "title": "步骤标题",
      "goal": "这一步要解决什么问题",
      "reason": "为什么此时需要这一步",
      "principle": "这一步依赖什么机制或知识",
      "action": "CAI 实际进行了什么操作",
      "result": "CAI 得到了什么关键输出或结论"
    }
  ]
}
"""


def _build_summarizer(model: Any) -> Agent:
    """Create the stage-2 summarizer agent.

    ``model`` may be a model string or an existing model object (preferred —
    carrying the per-build provider override attached by the reproduction stage).
    We avoid ``output_type`` here: the summarizer returns plain text JSON, which
    we parse manually (more robust across providers that don't enforce schemas).
    """
    return Agent(
        name="BankSummarizer",
        instructions=_SUMMARIZE_INSTRUCTIONS,
        model=model,
    )


def _rebuild_model(model: Any) -> Any:
    """Rebuild a fresh model instance from an existing one, preserving the
    per-build provider override (base URL / API key) but dropping run state
    (message history, cached client).

    Uses the shared default OpenAI client rather than stage-1's client so we do
    not reuse a connection that stage-1 may have left in a closed/dirty state.
    """
    cls = model.__class__
    try:
        from cai.sdk.agents.models import _openai_shared

        client = _openai_shared.get_default_openai_client()
        fresh = cls(
            model=getattr(model, "model", None) or "",
            openai_client=client,
            agent_name="BankSummarizer",
            agent_id=None,
        )
        for attr in ("_provider_base", "_provider_key"):
            val = getattr(model, attr, None)
            if val is not None:
                setattr(fresh, attr, val)
        return fresh
    except Exception:
        # Fall back to the original instance if rebuild is not possible.
        return model


async def _run_summarization(
    agent: Agent,
    payload: BankBuildRequest,
    reproduction: dict[str, Any],
) -> BigStepsModel | None:
    """Run stage 2: summarize the real reproduction into large steps."""
    # Build a FRESH model instance for stage-2. Reusing the stage-1 model object
    # carries dirty state (message history, connection) from stage-1's run and
    # makes stage-2 hang or return invalid output. Rebuild the same class with a
    # fresh client and re-attach the per-build provider override.
    agent_model = agent.model
    if not isinstance(agent_model, str):
        fresh_model = _rebuild_model(agent_model)
        summarizer = _build_summarizer(fresh_model)
    else:
        summarizer = _build_summarizer(agent_model or os.getenv("CAI_MODEL") or "")
        if payload.base_url is not None or payload.api_key is not None:
            from cai.util import apply_provider_to_agent

            apply_provider_to_agent(summarizer, payload.base_url, payload.api_key)

    # Compact but faithful summary of the real trace as the summarizer's input.
    # The item type is the run-item class name (e.g. "tool_call_item",
    # "message_output_item") as emitted by summarize_run_result.
    text_lines: list[str] = []
    for item in reproduction.get("messages", []):
        item_type = item.get("type", "")
        agent_name = item.get("agent", "")
        item_payload = item.get("payload") or {}
        if not isinstance(item_payload, dict):
            item_payload = {}
        if item_type == "message_output_item":
            content = item_payload.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    str(p.get("text", "")) for p in content if isinstance(p, dict)
                )
            text_lines.append(f"[AGENT {agent_name}] 输出: {content}")
        elif item_type == "tool_call_item":
            text_lines.append(
                f"[AGENT {agent_name}] 调用工具 {item_payload.get('name')}: "
                f"{item_payload.get('arguments', '')}"
            )
        elif item_type == "tool_call_output_item":
            text_lines.append(f"[AGENT {agent_name}] 工具输出: {item.get('output', '')}")
        elif item_type == "handoff_output_item":
            text_lines.append(f"[AGENT {agent_name}] Handoff → {item_payload}")

    user_input = "\n".join(
        [
            f"## 题目\n{payload.problem}",
            f"\n## 参考资料\n" + "\n\n".join(
                f"{i + 1}. {r}" for i, r in enumerate(payload.references)
            ),
            f"\n## 真实复现记录\n" + "\n".join(text_lines) or "(无记录)",
            f"\n## 最终结果\n{reproduction.get('final_output')}",
        ]
    )

    # Retry with a per-attempt timeout so a slow/flaky provider response does
    # not hang the whole build. Some providers (e.g. mnapi via a long-lived
    # process) can take a while on the summarization call, so allow up to 3 min
    # per attempt.
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            result = await asyncio.wait_for(
                Runner.run(summarizer, user_input, max_turns=3), timeout=180
            )
            text = _extract_final_text(result.final_output)
            if not text:
                _debug_stage2(f"attempt {attempt}: empty text")
                return None
            data = _parse_json_block(text)
            if not data:
                _debug_stage2(f"attempt {attempt}: parse failed. text={text[:200]!r}")
                return None
            return BigStepsModel(**data)
        except Exception as exc:  # noqa: PERF203
            last_err = exc
            _debug_stage2(
                f"attempt {attempt}: {type(exc).__name__}: {str(exc)[:200]}"
            )
            await asyncio.sleep(2)
    return None


def _debug_stage2(msg: str) -> None:
    try:
        with open("/tmp/qb_stage2_err.txt", "a", encoding="utf-8") as _f:
            _f.write(msg + "\n")
    except Exception:
        pass


def _extract_final_text(final_output: Any) -> str:
    """Extract a text blob from a run's final_output (str, list, or model)."""
    if isinstance(final_output, str):
        return final_output.strip()
    if isinstance(final_output, list):
        parts = []
        for p in final_output:
            if isinstance(p, dict) and p.get("type") == "output_text":
                parts.append(str(p.get("text", "")))
            elif isinstance(p, str):
                parts.append(p)
        return "\n".join(parts).strip()
    return str(final_output or "").strip()


def _parse_json_block(text: str) -> dict[str, Any] | None:
    """Parse a JSON object, tolerating ```json fences and surrounding prose."""
    t = text.strip()
    # Strip fenced code block if present.
    if t.startswith("```"):
        # remove first and last ``` line
        lines = t.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    # Try direct parse.
    try:
        return json.loads(t)
    except Exception:
        pass
    # Fall back to first { ... } block.
    start = t.find("{")
    end = t.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(t[start : end + 1])
        except Exception:
            pass
    return None


# ---------------------------------------------------------------------------
# PostgreSQL mirror (§7.4 integration)
# ---------------------------------------------------------------------------
_pg_repo = None


def _get_pg_repo():
    """Shared PostgreSQL repository (lazy singleton, like the v2 routes)."""
    global _pg_repo
    if _pg_repo is None:
        from cai.question_bank.repository import QuestionRepository

        _pg_repo = QuestionRepository()
    return _pg_repo


async def _persist_to_db(response: BankBuildResponse) -> None:
    """Mirror a built entry into PostgreSQL so it shows up in 题库管理 (§7.4)
    and is usable by the QuestionResolver (§7.3).

    Best-effort: the file-based result is already saved, so a database failure
    must never fail the build. Completed entries become a ``Question`` with a
    ``usable`` SolutionMethod + steps; incomplete entries are recorded with
    status ``incomplete``.
    """
    if not response.problem:
        return
    try:
        from cai.question_bank.fingerprints import (
            canonicalize_statement,
            compute_instance_fingerprint,
        )
        from cai.question_bank.models import SolutionMethod
        from sqlalchemy import update

        repo = _get_pg_repo()
        fingerprint = compute_instance_fingerprint(response.problem)
        canonical = canonicalize_statement(response.problem)
        completed = response.status == "completed" and bool(response.steps)

        async with repo.session() as sess:
            async with sess.begin():
                q = await repo.create_question(
                    sess,
                    statement_raw=response.problem,
                    statement_canonical=canonical,
                    instance_fingerprint=fingerprint,
                    success_criteria={},
                    materials_data=[],
                )
                if completed:
                    solution = await repo.create_solution_method(
                        sess,
                        question_id=q.question_id,
                        version=q.version,
                        status="draft",
                        overall_approach=response.overall_approach or "",
                    )
                    if response.steps:
                        await repo.create_solution_steps(
                            sess,
                            [
                                {
                                    "solution_id": solution.solution_id,
                                    "ordinal": i,
                                    "goal": s.get("goal", ""),
                                    "why": s.get("reason", ""),
                                    "principle": s.get("principle", ""),
                                    "action": s.get("action", ""),
                                    "result": s.get("result", ""),
                                    "evidence_refs": [],
                                }
                                for i, s in enumerate(response.steps)
                            ],
                        )
                    # Retire any prior usable method, point the question at the
                    # new one, then promote it to usable.
                    await repo.set_current_solution(
                        sess, q.question_id, solution.solution_id
                    )
                    await sess.execute(
                        update(SolutionMethod)
                        .where(SolutionMethod.solution_id == solution.solution_id)
                        .values(status="usable")
                    )
                    await repo.update_question_status(sess, q.question_id, "usable")
                else:
                    # Never downgrade a question that already reached usable.
                    if q.status != "usable":
                        await repo.update_question_status(
                            sess, q.question_id, "incomplete"
                        )
    except Exception as exc:  # noqa: BLE001
        _debug_stage2(f"persist_to_db failed: {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
async def build_question_bank(payload: BankBuildRequest) -> BankBuildResponse:
    """Two-stage pipeline: reproduce, then summarize. Persist the result."""
    bank_id = uuid.uuid4().hex[:12]

    try:
        agent, reproduction = await _run_reproduction(payload)
    except Exception as exc:
        # Stage-1 could not run to completion (e.g. a tool was blocked by the
        # safety layer, or the provider errored). Record it as an explicit
        # "incomplete" entry rather than failing the whole request (FR-06).
        response = BankBuildResponse(
            bank_id=bank_id,
            status="incomplete",
            incomplete_reason=f"复现阶段未能完成: {type(exc).__name__}: {exc}",
            problem=payload.problem,
            references=payload.references,
            reproduction={"messages": [], "history": [], "final_output": None},
            overall_approach=None,
            steps=None,
        )
        save_bank(response)
        await _persist_to_db(response)
        return response

    complete, reason = _is_complete(reproduction)
    response = BankBuildResponse(
        bank_id=bank_id,
        status="completed" if complete else "incomplete",
        incomplete_reason=None if complete else reason,
        problem=payload.problem,
        references=payload.references,
        reproduction=reproduction,
        overall_approach=None,
        steps=None,
    )

    if complete:
        summary = await _run_summarization(agent, payload, reproduction)
        if summary is not None:
            response.overall_approach = summary.overall_approach
            response.steps = [s.model_dump() for s in summary.steps]

    save_bank(response)
    await _persist_to_db(response)
    return response
