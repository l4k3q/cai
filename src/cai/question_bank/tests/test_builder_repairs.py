from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from cai.question_bank.builder import (
    BuildOrchestrator,
    parse_trace_events,
    serialize_trace_events,
)
from cai.question_bank.security import (
    collect_secret_values,
    normalize_model_id,
    redact_text,
    redact_value,
    runtime_config_for_execution,
    sanitize_runtime_config,
)


class MemoryStore:
    def __init__(self, objects=None):
        self.objects = objects or {}

    def download(self, object_key):
        return self.objects[object_key]


def _question(question_id, *, usable=False):
    return SimpleNamespace(
        question_id=question_id,
        status="usable" if usable else "draft",
        current_solution_method_id=uuid.uuid4() if usable else None,
        success_criteria={"criteria": [{"type": "output_contains", "value": "ok"}]},
        statement_raw="test question",
        materials=[],
    )


def _job(question_id):
    return SimpleNamespace(
        job_id=uuid.uuid4(),
        question_id=question_id,
        status="queued",
        runtime_config={
            "agent": "one_tool_agent",
            "model": "test/model",
            "base_url": "https://provider.example/v1",
            "api_key": "secret-provider-key",
        },
        run_id=None,
    )


class FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def begin(self):
        return self


class FakeRepository:
    def __init__(self, question, job):
        self.questions = {question.question_id: question}
        self.jobs = {job.job_id: job}
        self.runs = {}
        self.solutions = {}
        self.steps = []

    @asynccontextmanager
    async def _session(self):
        yield FakeSession()

    def session(self):
        return self._session()

    async def get_build_job_for_update(self, sess, job_id):
        return self.jobs.get(job_id)

    async def get_question_for_update(self, sess, question_id):
        return self.questions.get(question_id)

    async def get_question(self, sess, question_id):
        return self.questions.get(question_id)

    async def update_build_job(self, sess, job_id, **values):
        job = self.jobs[job_id]
        for key, value in values.items():
            setattr(job, key, value)

    async def update_question_status(self, sess, question_id, status):
        self.questions[question_id].status = status

    async def create_run_record(self, sess, **values):
        run = SimpleNamespace(run_id=uuid.uuid4(), **values)
        self.runs[run.run_id] = run
        return run

    async def update_run_status(self, sess, run_id, **values):
        for key, value in values.items():
            setattr(self.runs[run_id], key, value)

    async def create_solution_method(self, sess, **values):
        solution = SimpleNamespace(solution_id=uuid.uuid4(), **values)
        self.solutions[solution.solution_id] = solution
        return solution

    async def create_solution_steps(self, sess, values):
        self.steps.extend(values)
        return [SimpleNamespace(**value) for value in values]

    async def update_solution_method(self, sess, solution_id, **values):
        for key, value in values.items():
            setattr(self.solutions[solution_id], key, value)

    async def has_usable_solution(self, sess, question_id):
        return any(
            solution.question_id == question_id and solution.status == "usable"
            for solution in self.solutions.values()
        )

    async def set_current_solution(self, sess, question_id, solution_id):
        for solution in self.solutions.values():
            if (
                solution.question_id == question_id
                and solution.solution_id != solution_id
                and solution.status == "usable"
            ):
                solution.status = "superseded"
        self.questions[question_id].current_solution_method_id = solution_id


class FakeOrchestrator(BuildOrchestrator):
    def __init__(self, repo, store, gate2_passed):
        super().__init__(repo, store)
        self.gate2_passed = gate2_passed

    async def _run_cai_reproduction(self, question, runtime_config, *, job_id=None):
        async with self._repo.session() as sess:
            run = await self._repo.create_run_record(
                sess,
                question_id=question.question_id,
                trigger="autonomous",
                status="running",
            )
            await self._repo.update_build_job(sess, job_id, run_id=run.run_id)
        run.trace_object_key = "trace"
        run.final_output_object_key = "final"
        return run

    async def _verify_gate1(self, question, run_record):
        return True, {"results": [{"passed": True}], "blocker": None}

    async def _synthesize_method(self, question, run_record, runtime_config):
        return {
            "overall_approach": "approach",
            "principles": {},
            "steps": [
                {
                    "ordinal": 0,
                    "goal": "goal",
                    "why": "why",
                    "principle": "principle",
                    "action": "action",
                    "result": "result",
                    "evidence_refs": [{"event_id": "event-1"}],
                }
            ],
        }

    async def _verify_gate2(self, question, run_record, solution, steps):
        return self.gate2_passed, {
            "results": [{"step_ordinal": 0, "passed": self.gate2_passed}],
            "blocker": None if self.gate2_passed else "bad evidence",
        }


def test_trace_roundtrip_uses_jsonl_and_redacts_secret():
    run_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
    data = serialize_trace_events(
        [
            {"event_id": "event-1", "tool": "shell"},
            {"tool": "shell", "api_key": "secret-provider-key"},
        ],
        run_id=run_id,
        secrets=("secret-provider-key",),
    )

    lines = data.decode("utf-8").splitlines()
    assert len(lines) == 2
    assert all(isinstance(json.loads(line), dict) for line in lines)
    assert "secret-provider-key" not in data.decode("utf-8")
    events = parse_trace_events(data, run_id=run_id)
    assert [event["event_id"] for event in events] == ["event-1", f"{run_id}:000001"]
    assert events[1]["index"] == 1


def test_parse_trace_events_accepts_legacy_json_array():
    run_id = uuid.UUID("22222222-2222-2222-2222-222222222222")
    legacy = json.dumps([{"event_id": "legacy-1"}, {"index": 1}], indent=2)
    events = parse_trace_events(legacy, run_id=run_id)
    assert len(events) == 2
    assert events[0]["event_id"] == "legacy-1"
    assert events[1]["index"] == 1


@pytest.mark.asyncio
async def test_gate2_reads_jsonl_and_requires_real_event():
    run_id = uuid.UUID("33333333-3333-3333-3333-333333333333")
    store = MemoryStore(
        {"trace": serialize_trace_events([{"event_id": "real-event"}], run_id=run_id)}
    )
    orchestrator = BuildOrchestrator(None, store)
    question = SimpleNamespace()
    run = SimpleNamespace(run_id=run_id, trace_object_key="trace")
    solution = SimpleNamespace(verified_run_id=run_id)

    passed, result = await orchestrator._verify_gate2(
        question,
        run,
        solution,
        [{"ordinal": 0, "evidence_refs": [{"event_id": "real-event"}]}],
    )
    assert passed is True
    assert result["trace_event_count"] == 1

    passed, result = await orchestrator._verify_gate2(
        question,
        run,
        solution,
        [{"ordinal": 0, "evidence_refs": [{"event_id": "fabricated"}]}],
    )
    assert passed is False
    assert result["results"][0]["passed"] is False


def test_runtime_config_execution_preserves_key_but_public_cleaner_drops_it():
    config = {
        "agent": "agent",
        "model": "model",
        "base_url": "https://provider/v1",
        "api_key": "secret-provider-key",
        "nested": {"password": "secret-password"},
    }
    execution = runtime_config_for_execution(config)
    public = sanitize_runtime_config(config)
    assert execution["api_key"] == "secret-provider-key"
    assert "api_key" not in public
    assert redact_value(config)["api_key"] == "[REDACTED]"
    assert "secret-provider-key" not in redact_text(config, collect_secret_values(config))


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("deepseek-flash", "deepseek/deepseek-flash"),
        ("claude-3-5-sonnet", "anthropic/claude-3-5-sonnet"),
        ("gpt-5", "openai/gpt-5"),
        ("gemini-2.5-pro", "gemini/gemini-2.5-pro"),
        ("mistral-large", "mistral/mistral-large"),
        ("deepseek/deepseek-chat", "deepseek/deepseek-chat"),
        ("custom-model", "custom-model"),
    ],
)
def test_normalize_model_id(model, expected):
    assert normalize_model_id(model) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("gate2_passed", [True, False])
async def test_build_state_closes_success_and_gate2_failure(gate2_passed):
    question_id = uuid.uuid4()
    question = _question(question_id)
    job = _job(question_id)
    repo = FakeRepository(question, job)
    orchestrator = FakeOrchestrator(repo, MemoryStore(), gate2_passed)

    await orchestrator.run(job.job_id)

    assert job.run_id is not None
    run = repo.runs[job.run_id]
    assert run.run_id == job.run_id
    solution = next(iter(repo.solutions.values()))
    assert solution.verification_result["gate1"]
    assert solution.verification_result["gate2"]
    if gate2_passed:
        assert job.status == "usable"
        assert run.status == "verified"
        assert solution.status == "usable"
        assert question.status == "usable"
        assert question.current_solution_method_id == solution.solution_id
    else:
        assert job.status == "incomplete"
        assert solution.status == "failed"
        assert question.status == "incomplete"


@pytest.mark.asyncio
async def test_gate2_failure_preserves_existing_usable_method():
    question_id = uuid.uuid4()
    question = _question(question_id, usable=True)
    job = _job(question_id)
    repo = FakeRepository(question, job)
    old_solution = SimpleNamespace(
        solution_id=question.current_solution_method_id,
        question_id=question_id,
        status="usable",
    )
    repo.solutions[old_solution.solution_id] = old_solution
    orchestrator = FakeOrchestrator(repo, MemoryStore(), False)

    await orchestrator.run(job.job_id)

    assert job.status == "incomplete"
    assert question.status == "usable"
    assert old_solution.status == "usable"
