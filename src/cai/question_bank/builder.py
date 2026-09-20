"""Question-bank build orchestration and two-stage verification."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Optional

from cai.question_bank.materials import MaterialStore
from cai.question_bank.models import Question, RunRecord, SolutionMethod
from cai.question_bank.repository import QuestionRepository
from cai.question_bank.security import (
    collect_secret_values,
    redact_text,
    redact_value,
    runtime_config_for_execution,
)

logger = logging.getLogger(__name__)


def _trace_items(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, Mapping):
        for key in ("events", "messages", "trace"):
            nested = value.get(key)
            if isinstance(nested, (list, tuple)):
                return list(nested)
        return [value]
    return []


def _normalize_trace_events(
    value: Any, run_id: Optional[uuid.UUID] = None
) -> list[dict]:
    prefix = str(run_id) if run_id is not None else "trace"
    normalized: list[dict] = []
    for index, item in enumerate(_trace_items(value)):
        event = dict(item) if isinstance(item, Mapping) else {"content": item}
        if not event.get("index"):
            event["index"] = index
        if not event.get("event_index"):
            event["event_index"] = index
        if not event.get("event_id"):
            event["event_id"] = f"{prefix}:{index:06d}"
        normalized.append(event)
    return normalized


def serialize_trace_events(
    events: Any,
    *,
    run_id: Optional[uuid.UUID] = None,
    secrets: tuple[str, ...] = (),
) -> bytes:
    """Write a trace as canonical JSONL with stable event identifiers."""
    lines = [
        json.dumps(
            redact_value(event, secrets),
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        for event in _normalize_trace_events(events, run_id)
    ]
    return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")


def parse_trace_events(
    data: bytes | str,
    *,
    run_id: Optional[uuid.UUID] = None,
) -> list[dict]:
    """Read both the current JSONL format and legacy JSON arrays."""
    text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
    text = text.strip()
    if not text:
        return []

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        items: list[Any] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid trace JSON at line {line_number}") from exc
            if isinstance(item, list):
                items.extend(item)
            else:
                items.append(item)
        payload = items

    return _normalize_trace_events(payload, run_id)


def _event_index(event: Mapping[str, Any], fallback: int) -> int:
    for key in ("event_index", "index", "sequence", "ordinal"):
        value = event.get(key)
        if isinstance(value, bool) or value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return fallback


def _json_equal(left: Any, right: Any) -> bool:
    if left == right:
        return True
    try:
        return json.dumps(left, sort_keys=True, default=str) == json.dumps(
            right, sort_keys=True, default=str
        )
    except (TypeError, ValueError):
        return str(left) == str(right)


def _reference_matches(
    reference: Any, event: Mapping[str, Any], fallback_index: int
) -> bool:
    if isinstance(reference, Mapping):
        expected_id = next(
            (
                reference.get(key)
                for key in ("event_id", "trace_event_id", "id")
                if reference.get(key) not in (None, "")
            ),
            None,
        )
        expected_indexes = [
            reference.get(key)
            for key in ("event_index", "index", "sequence", "ordinal")
            if reference.get(key) is not None
        ]
    elif isinstance(reference, (str, int)) and not isinstance(reference, bool):
        expected_id = reference
        expected_indexes = []
    else:
        return False

    current_index = _event_index(event, fallback_index)
    event_ids = {
        str(event.get(key))
        for key in ("event_id", "trace_event_id", "id")
        if event.get(key) not in (None, "")
    }
    matched = False
    if expected_id is not None:
        expected_id_text = str(expected_id)
        matched = expected_id_text in event_ids
        if not matched and expected_id_text.isdecimal():
            matched = int(expected_id_text) == current_index
        if not matched and ":" in expected_id_text:
            prefix, suffix = expected_id_text.rsplit(":", 1)
            if prefix in {"trace", "event", "message"} and suffix.isdecimal():
                matched = int(suffix) == current_index
    if expected_indexes:
        for expected_index in expected_indexes:
            try:
                if int(expected_index) == current_index:
                    matched = True
                    break
            except (TypeError, ValueError):
                continue
    if not matched:
        return False

    aliases = {
        "kind": ("kind", "type", "event_type"),
        "type": ("type", "kind", "event_type"),
        "event_type": ("event_type", "kind", "type"),
        "tool": ("tool",),
        "input_digest": ("input_digest",),
        "output_digest": ("output_digest",),
    }
    if isinstance(reference, Mapping):
        for reference_key, event_keys in aliases.items():
            if reference_key not in reference:
                continue
            actual = next(
                (event[key] for key in event_keys if key in event),
                None,
            )
            if actual is None or not _json_equal(reference[reference_key], actual):
                return False
    return True


class BuildOrchestrator:
    """Run one build job; duplicate deliveries are ignored by job status."""

    def __init__(self, repo: QuestionRepository, store: MaterialStore):
        self._repo = repo
        self._store = store

    async def run(self, job_id: uuid.UUID) -> None:
        claimed = await self._claim_build(job_id)
        if claimed is None:
            return
        question, runtime_config = claimed
        secrets = collect_secret_values(runtime_config)
        try:
            await self._run_claimed_build(job_id, question, runtime_config, secrets)
        except Exception as exc:
            safe_error = redact_text(exc, secrets)
            logger.error("BuildJob %s failed: %s", job_id, safe_error)
            await self._fail_build(
                job_id,
                question.question_id,
                f"构建失败: {safe_error}",
                "orchestrator_error",
                secrets=secrets,
            )

    async def _claim_build(
        self, job_id: uuid.UUID
    ) -> Optional[tuple[Question, dict[str, Any]]]:
        async with self._repo.session() as sess:
            async with sess.begin():
                job_loader = getattr(self._repo, "get_build_job_for_update", None)
                if job_loader is None:
                    job_loader = self._repo.get_build_job
                job = await job_loader(sess, job_id)
                if job is None:
                    logger.error("BuildJob %s not found", job_id)
                    return None
                if job.status != "queued":
                    logger.warning(
                        "BuildJob %s already started: %s", job_id, job.status
                    )
                    return None

                question_loader = getattr(self._repo, "get_question_for_update", None)
                if question_loader is None:
                    question_loader = self._repo.get_question
                question = await question_loader(sess, job.question_id)
                if question is None:
                    await self._repo.update_build_job(
                        sess,
                        job_id,
                        status="failed",
                        blocker="题目不存在",
                        error_code="question_not_found",
                        retryable=False,
                        finished_at=datetime.now(timezone.utc),
                    )
                    return None

                runtime_config = runtime_config_for_execution(job.runtime_config or {})
                now = datetime.now(timezone.utc)
                await self._repo.update_build_job(
                    sess,
                    job_id,
                    status="running",
                    started_at=now,
                    updated_at=now,
                )
                await self._repo.update_question_status(
                    sess, question.question_id, "building"
                )
                return question, runtime_config

    async def _run_claimed_build(
        self,
        job_id: uuid.UUID,
        question: Question,
        runtime_config: dict[str, Any],
        secrets: tuple[str, ...],
    ) -> None:
        run_record = await self._run_cai_reproduction(
            question, runtime_config, job_id=job_id
        )

        async with self._repo.session() as sess:
            async with sess.begin():
                await self._repo.update_build_job(
                    sess,
                    job_id,
                    status="verifying_result",
                    progress=0.4,
                    run_id=run_record.run_id,
                )

        gate1_passed, gate1_result = await self._verify_gate1(question, run_record)
        if not gate1_passed:
            async with self._repo.session() as sess:
                async with sess.begin():
                    await self._repo.update_run_status(
                        sess,
                        run_record.run_id,
                        status="incomplete",
                        verification_result=redact_value(gate1_result, secrets),
                        blocker=redact_text(
                            gate1_result.get("blocker", "终态验证未通过"), secrets
                        ),
                        finished_at=datetime.now(timezone.utc),
                    )
                    await self._mark_build_failure_in_transaction(
                        sess,
                        job_id,
                        question.question_id,
                        gate1_result.get("blocker", "终态验证未通过"),
                        "gate1_failed",
                        secrets=secrets,
                    )
            return

        async with self._repo.session() as sess:
            async with sess.begin():
                await self._repo.update_run_status(
                    sess,
                    run_record.run_id,
                    status="verified",
                    verification_result=redact_value(gate1_result, secrets),
                )
                await self._repo.update_build_job(
                    sess,
                    job_id,
                    status="synthesizing_method",
                    progress=0.6,
                    run_id=run_record.run_id,
                )

        try:
            solution_data = await self._synthesize_method(
                question, run_record, runtime_config
            )
        except Exception as exc:
            safe_error = redact_text(exc, secrets)
            await self._fail_build(
                job_id,
                question.question_id,
                f"方法合成失败: {safe_error}",
                "synthesis_error",
                secrets=secrets,
            )
            return

        solution_data = redact_value(solution_data, secrets)
        if not isinstance(solution_data, Mapping):
            solution_data = {
                "overall_approach": None,
                "principles": {},
                "steps": [],
            }
        steps = solution_data.get("steps", [])
        if not isinstance(steps, list):
            steps = []

        async with self._repo.session() as sess:
            async with sess.begin():
                solution = await self._repo.create_solution_method(
                    sess,
                    question_id=question.question_id,
                    status="verifying",
                    overall_approach=solution_data.get("overall_approach"),
                    principles=solution_data.get("principles"),
                    verified_run_id=run_record.run_id,
                )
                step_rows = [
                    {
                        "solution_id": solution.solution_id,
                        "ordinal": step.get("ordinal", index),
                        "goal": step.get("goal", ""),
                        "why": step.get("why", ""),
                        "principle": step.get("principle", ""),
                        "action": step.get("action", ""),
                        "result": step.get("result", ""),
                        "evidence_refs": step.get("evidence_refs", []),
                    }
                    for index, step in enumerate(steps)
                    if isinstance(step, Mapping)
                ]
                if step_rows:
                    await self._repo.create_solution_steps(sess, step_rows)
                await self._repo.update_build_job(
                    sess,
                    job_id,
                    status="verifying_method",
                    progress=0.85,
                    run_id=run_record.run_id,
                )

        gate2_passed, gate2_result = await self._verify_gate2(
            question, run_record, solution, steps
        )
        verification_result = redact_value(
            {"gate1": gate1_result, "gate2": gate2_result}, secrets
        )

        async with self._repo.session() as sess:
            async with sess.begin():
                if gate2_passed:
                    await self._repo.update_solution_method(
                        sess,
                        solution.solution_id,
                        status="usable",
                        verification_result=verification_result,
                    )
                    await self._repo.update_run_status(
                        sess,
                        run_record.run_id,
                        status="verified",
                        verification_result=verification_result,
                        finished_at=datetime.now(timezone.utc),
                    )
                    await self._repo.update_build_job(
                        sess,
                        job_id,
                        status="usable",
                        progress=1.0,
                        run_id=run_record.run_id,
                        finished_at=datetime.now(timezone.utc),
                    )
                    await self._repo.set_current_solution(
                        sess, question.question_id, solution.solution_id
                    )
                    await self._repo.update_question_status(
                        sess, question.question_id, "usable"
                    )
                else:
                    await self._repo.update_solution_method(
                        sess,
                        solution.solution_id,
                        status="failed",
                        verification_result=verification_result,
                    )
                    await self._repo.update_run_status(
                        sess,
                        run_record.run_id,
                        status="verified",
                        verification_result=verification_result,
                        finished_at=datetime.now(timezone.utc),
                    )
                    await self._mark_build_failure_in_transaction(
                        sess,
                        job_id,
                        question.question_id,
                        gate2_result.get("blocker", "证据验证未通过"),
                        "gate2_failed",
                        secrets=secrets,
                    )

    async def _run_cai_reproduction(
        self,
        question: Question,
        runtime_config: dict,
        *,
        job_id: Optional[uuid.UUID] = None,
    ) -> RunRecord:
        """Run the existing CAI pipeline and persist a redacted trace."""
        materials = []
        for material in question.materials or []:
            if material.status != "available":
                continue
            try:
                content = self._store.download(material.object_key)
                materials.append(content.decode("utf-8", errors="replace"))
            except Exception:
                materials.append(f"[无法读取: {material.object_key}]")

        from cai.api.question_bank import BankBuildRequest, build_question_bank

        execution_config = runtime_config_for_execution(runtime_config)
        payload = BankBuildRequest(
            problem=question.statement_raw,
            references=materials,
            max_turns=20,
            agent=execution_config.get("agent"),
            model=execution_config.get("model"),
            base_url=execution_config.get("base_url"),
            api_key=execution_config.get("api_key"),
        )
        result = await build_question_bank(payload)
        reproduction = getattr(result, "reproduction", {}) or {}
        if not isinstance(reproduction, Mapping):
            reproduction = {}
        secrets = collect_secret_values(execution_config)

        run: Optional[RunRecord] = None
        uploaded_keys: list[str] = []
        try:
            async with self._repo.session() as sess:
                async with sess.begin():
                    run = await self._repo.create_run_record(
                        sess,
                        question_id=question.question_id,
                        trigger="autonomous",
                        status="running",
                        started_at=datetime.now(timezone.utc),
                    )
                    if job_id is not None:
                        await self._repo.update_build_job(
                            sess, job_id, run_id=run.run_id
                        )

            trace_key = MaterialStore.make_trace_key(run.run_id)
            trace_data = serialize_trace_events(
                reproduction.get("messages", []),
                run_id=run.run_id,
                secrets=secrets,
            )
            uploaded_keys.append(trace_key)
            self._store.upload(trace_key, trace_data, "application/jsonl")

            final_key: Optional[str] = None
            if "final_output" in reproduction and reproduction.get("final_output") is not None:
                final_key = f"runs/{run.run_id.hex}/final_output.json"
                final_data = json.dumps(
                    redact_value(reproduction["final_output"], secrets),
                    ensure_ascii=False,
                    default=str,
                ).encode("utf-8")
                uploaded_keys.append(final_key)
                self._store.upload(final_key, final_data, "application/json")

            async with self._repo.session() as sess:
                async with sess.begin():
                    await self._repo.update_run_status(
                        sess,
                        run.run_id,
                        trace_object_key=trace_key,
                        final_output_object_key=final_key,
                        status="running",
                    )

            run.trace_object_key = trace_key
            run.final_output_object_key = final_key
            return run
        except Exception as exc:
            for object_key in uploaded_keys:
                try:
                    self._store.delete(object_key)
                except Exception:
                    logger.warning("Failed to clean build object %s", object_key)
            if run is not None:
                try:
                    async with self._repo.session() as sess:
                        async with sess.begin():
                            await self._repo.update_run_status(
                                sess,
                                run.run_id,
                                status="failed",
                                worker_error=redact_text(exc, secrets),
                                finished_at=datetime.now(timezone.utc),
                            )
                except Exception:
                    logger.error("Failed to record run failure %s", run.run_id)
            raise

    async def _verify_gate1(
        self, question: Question, run_record: RunRecord
    ) -> tuple[bool, dict]:
        criteria = question.success_criteria or {}
        if not isinstance(criteria, Mapping):
            return False, {"blocker": "success_criteria 格式无效", "results": []}
        criteria_list = criteria.get("criteria", [])
        match_mode = criteria.get("match", "all")
        if not isinstance(criteria_list, list) or not criteria_list:
            return False, {"blocker": "题目未配置 success_criteria", "results": []}

        if not run_record.final_output_object_key:
            return False, {"blocker": "终态输出不存在", "results": []}
        try:
            final_output = json.loads(
                self._store.download(run_record.final_output_object_key)
            )
        except Exception:
            return False, {"blocker": "终态输出无法读取", "results": []}

        results = []
        for criterion in criteria_list:
            if not isinstance(criterion, Mapping):
                results.append(
                    {"type": None, "passed": False, "observation": "判据格式无效"}
                )
                continue
            criterion_type = criterion.get("type")
            expected = criterion.get("value", "")
            if criterion_type == "flag":
                passed = self._check_flag(final_output, expected)
                observation = "flag matched" if passed else "flag not found"
            elif criterion_type == "output_contains":
                passed = self._check_output_contains(final_output, expected)
                observation = (
                    "output contains marker"
                    if passed
                    else "output does not contain marker"
                )
            elif criterion_type == "state_assertion":
                passed = False
                observation = "state_assertion not implemented in v1"
            elif criterion_type == "checker":
                passed = False
                observation = "checker not implemented in v1"
            else:
                passed = False
                observation = f"unknown criteria type: {criterion_type}"
            results.append(
                {
                    "type": criterion_type,
                    "passed": passed,
                    "observation": observation,
                }
            )

        if match_mode == "all":
            passed = all(result["passed"] for result in results)
        elif match_mode == "any":
            passed = any(result["passed"] for result in results)
        else:
            passed = False
        return passed, {
            "results": results,
            "blocker": None if passed else "部分判据未通过",
            "match_mode": match_mode,
        }

    def _check_flag(self, final_output: Any, expected: Any) -> bool:
        if not isinstance(expected, str) or not expected:
            return False
        text = (
            final_output
            if isinstance(final_output, str)
            else json.dumps(final_output, default=str)
        )
        return expected in text

    def _check_output_contains(self, final_output: Any, expected: Any) -> bool:
        if not isinstance(expected, str) or not expected:
            return False
        text = (
            final_output
            if isinstance(final_output, str)
            else json.dumps(final_output, default=str)
        )
        return expected.lower() in text.lower()

    async def _verify_gate2(
        self,
        question: Question,
        run_record: RunRecord,
        solution: SolutionMethod,
        steps: list[dict],
    ) -> tuple[bool, dict]:
        del question
        if not steps:
            return False, {"blocker": "无步骤，无法验证", "results": []}
        verified_run_id = getattr(solution, "verified_run_id", None)
        if verified_run_id is not None and str(verified_run_id) != str(run_record.run_id):
            return False, {"blocker": "方法引用的运行记录不一致", "results": []}
        if not run_record.trace_object_key:
            return False, {"blocker": "Trace 不存在", "results": []}

        try:
            trace_events = parse_trace_events(
                self._store.download(run_record.trace_object_key),
                run_id=run_record.run_id,
            )
        except Exception:
            return False, {"blocker": "Trace 无法读取或格式无效", "results": []}
        if not trace_events:
            return False, {"blocker": "Trace 中没有实际事件", "results": []}

        results = []
        last_event_index = -1
        for step in steps:
            if not isinstance(step, Mapping):
                results.append(
                    {
                        "step_ordinal": None,
                        "passed": False,
                        "reason": "步骤格式无效",
                    }
                )
                continue
            evidence_refs = step.get("evidence_refs", [])
            if isinstance(evidence_refs, Mapping):
                evidence_refs = [evidence_refs]
            if not isinstance(evidence_refs, (list, tuple)) or not evidence_refs:
                results.append(
                    {
                        "step_ordinal": step.get("ordinal"),
                        "passed": False,
                        "reason": "无证据引用",
                    }
                )
                continue

            found_indices: list[int] = []
            reason = "evidence verified"
            all_found = True
            for reference in evidence_refs:
                found = None
                for position, event in enumerate(trace_events):
                    if _reference_matches(reference, event, position):
                        found = _event_index(event, position)
                        break
                if found is None:
                    all_found = False
                    reason = "部分证据未在 Trace 中找到"
                    break
                found_indices.append(found)
            if all_found and found_indices and min(found_indices) < last_event_index:
                all_found = False
                reason = "步骤证据顺序早于前一步"
            if found_indices:
                last_event_index = max(last_event_index, max(found_indices))
            results.append(
                {
                    "step_ordinal": step.get("ordinal"),
                    "passed": all_found,
                    "reason": reason,
                }
            )

        passed = bool(results) and all(result["passed"] for result in results)
        return passed, {
            "results": results,
            "trace_event_count": len(trace_events),
            "blocker": None if passed else "部分步骤证据不可追溯",
        }

    async def _synthesize_method(
        self, question: Question, run_record: RunRecord, runtime_config: dict
    ) -> dict[str, Any]:
        from cai.api.question_bank import BankBuildRequest, build_question_bank

        materials = []
        for material in question.materials or []:
            if material.status != "available":
                continue
            try:
                content = self._store.download(material.object_key)
                materials.append(content.decode("utf-8", errors="replace"))
            except Exception:
                continue

        execution_config = runtime_config_for_execution(runtime_config)
        payload = BankBuildRequest(
            problem=question.statement_raw,
            references=materials,
            max_turns=20,
            agent=execution_config.get("agent"),
            model=execution_config.get("model"),
            base_url=execution_config.get("base_url"),
            api_key=execution_config.get("api_key"),
        )
        result = await build_question_bank(payload)
        if result.overall_approach and result.steps:
            enriched_steps = []
            for index, step in enumerate(result.steps):
                if not isinstance(step, Mapping):
                    continue
                enriched = dict(step)
                enriched["ordinal"] = index
                enriched.setdefault("evidence_refs", [])
                enriched_steps.append(enriched)
            return {
                "overall_approach": result.overall_approach,
                "principles": {},
                "steps": enriched_steps,
            }
        return {"overall_approach": None, "principles": {}, "steps": []}

    async def _mark_build_failure_in_transaction(
        self,
        sess,
        job_id: uuid.UUID,
        question_id: uuid.UUID,
        blocker: str,
        error_code: str,
        *,
        secrets: tuple[str, ...] = (),
    ) -> None:
        await self._repo.update_build_job(
            sess,
            job_id,
            status="incomplete",
            progress=0.9 if error_code in ("gate1_failed", "gate2_failed") else 0.0,
            blocker=redact_text(blocker, secrets),
            error_code=error_code,
            retryable=(error_code not in ("gate1_failed", "gate2_failed")),
            finished_at=datetime.now(timezone.utc),
        )
        question = await self._repo.get_question(sess, question_id)
        if question is None:
            return
        checker = getattr(self._repo, "has_usable_solution", None)
        if checker is not None:
            has_usable_method = await checker(sess, question_id)
        else:
            has_usable_method = question.status == "usable" or bool(
                question.current_solution_method_id
            )
        await self._repo.update_question_status(
            sess,
            question_id,
            "usable" if has_usable_method else "incomplete",
        )

    async def _fail_build(
        self,
        job_id: uuid.UUID,
        question_id: uuid.UUID,
        blocker: str,
        error_code: str,
        *,
        secrets: tuple[str, ...] = (),
    ) -> None:
        """Record failure in a fresh transaction."""
        async with self._repo.session() as sess:
            async with sess.begin():
                await self._mark_build_failure_in_transaction(
                    sess,
                    job_id,
                    question_id,
                    blocker,
                    error_code,
                    secrets=secrets,
                )
