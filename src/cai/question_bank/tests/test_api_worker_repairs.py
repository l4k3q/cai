from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from cai.question_bank import routes
from cai.question_bank_worker import main as worker_main


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _ListRepository:
    def __init__(self, items, has_more):
        self.items = items
        self.has_more = has_more

    def session(self):
        return _Session()

    async def list_questions_page(self, sess, **kwargs):
        return self.items, len(self.items), self.has_more


@pytest.mark.asyncio
@pytest.mark.parametrize("has_more, expected_cursor", [(False, None), (True, "last")])
async def test_list_questions_uses_page_metadata(monkeypatch, has_more, expected_cursor):
    question_id = uuid.uuid4()
    question = SimpleNamespace(
        question_id=question_id,
        version=1,
        status="draft",
        instance_fingerprint="fp",
        statement_raw="question",
        materials=[],
        current_solution_method_id=None,
        created_at=None,
        updated_at=None,
    )
    repo = _ListRepository([question], has_more)
    monkeypatch.setattr(routes, "_get_repo", lambda: repo)

    result = await routes.list_questions(limit=1)

    assert result["next_cursor"] == (str(question_id) if has_more else expected_cursor)


@pytest.mark.asyncio
async def test_build_run_response_redacts_runtime_config(monkeypatch):
    job = SimpleNamespace(
        job_id=uuid.uuid4(),
        question_id=uuid.uuid4(),
        run_id=None,
        status="queued",
        progress=0.0,
        retryable=False,
        blocker=None,
        error_code=None,
        runtime_config={"agent": "one_tool_agent", "api_key": "secret"},
        started_at=None,
        finished_at=None,
        created_at=None,
        updated_at=None,
    )

    class _Repository:
        def session(self):
            return _Session()

        async def get_build_job(self, sess, job_id):
            return job

    monkeypatch.setattr(routes, "_get_repo", lambda: _Repository())

    response = await routes.get_build_run(job.job_id)

    assert response.runtime_config == {"agent": "one_tool_agent"}
    assert "api_key" not in response.model_dump_json()
    assert "secret" not in response.model_dump_json()


class _Redis:
    def __init__(self, pending=None, processing=None):
        self.lists = {
            worker_main.QUEUE_KEY: list(pending or []),
            worker_main.PROCESSING_QUEUE_KEY: list(processing or []),
        }

    async def rpoplpush(self, source, destination):
        if not self.lists[source]:
            return None
        payload = self.lists[source].pop()
        self.lists[destination].insert(0, payload)
        return payload

    async def lrem(self, key, count, value):
        removed = 0
        remaining = []
        for item in self.lists[key]:
            if item == value and removed < count:
                removed += 1
            else:
                remaining.append(item)
        self.lists[key] = remaining
        return removed


@pytest.mark.asyncio
async def test_worker_recovers_processing_and_acks_one_delivery():
    first = b"11111111-1111-1111-1111-111111111111"
    second = b"22222222-2222-2222-2222-222222222222"
    redis = _Redis(pending=[b"pending"], processing=[first, second])
    worker = worker_main.BuildWorker()
    worker._redis = redis

    await worker._recover_processing()
    assert redis.lists[worker_main.PROCESSING_QUEUE_KEY] == []
    assert set(redis.lists[worker_main.QUEUE_KEY]) == {b"pending", first, second}

    redis.lists[worker_main.PROCESSING_QUEUE_KEY] = [first, first]
    await worker._ack_processing(first)
    assert redis.lists[worker_main.PROCESSING_QUEUE_KEY] == [first]
