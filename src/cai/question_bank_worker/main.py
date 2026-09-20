"""题库构建 Redis Worker 入口 (§7.2).

职责:
1. Outbox Dispatcher: 轮询 PostgreSQL outbox_events → 投递到 Redis
2. Build Worker: 从 Redis 队列领取 build_job → 调用 BuildOrchestrator

启动:
    python -m cai.question_bank_worker.main
    # 或
    cai question-bank-worker
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import uuid
from datetime import datetime, timezone
from typing import Optional

import redis.asyncio as aioredis

from cai.question_bank.builder import BuildOrchestrator
from cai.question_bank.materials import MaterialStore
from cai.question_bank.repository import QuestionRepository

logger = logging.getLogger(__name__)

# ── 默认配置 ───────────────────────────────────────────────────────────────

REDIS_URL = os.getenv("CAI_REDIS_URL", "redis://localhost:6379/0")
DB_URL = os.getenv("CAI_DB_URL", "postgresql+asyncpg://cai:cai@localhost:5432/question_bank")
QUEUE_KEY = "build_jobs:queue"
PROCESSING_QUEUE_KEY = "build_jobs:processing"
OUTBOX_POLL_SECONDS = 5.0
WORKER_POLL_SECONDS = 2.0


class BuildWorker:
    """Redis-backed 题库构建 Worker."""

    def __init__(self):
        self._redis: Optional[aioredis.Redis] = None
        self._repo: Optional[QuestionRepository] = None
        self._store: Optional[MaterialStore] = None
        self._orchestrator: Optional[BuildOrchestrator] = None
        self._running = False

    async def start(self) -> None:
        """启动 Worker."""
        logger.info("Starting BuildWorker...")
        self._running = True

        self._redis = aioredis.from_url(REDIS_URL)
        self._repo = QuestionRepository(DB_URL)
        self._store = MaterialStore()
        self._orchestrator = BuildOrchestrator(self._repo, self._store)

        # 验证连接
        await self._redis.ping()
        logger.info("Redis connected: %s", REDIS_URL)
        await self._recover_processing()

        # 并行运行 dispatcher + worker
        await asyncio.gather(
            self._outbox_dispatcher(),
            self._build_worker(),
        )

    async def stop(self) -> None:
        """优雅关闭."""
        logger.info("Stopping BuildWorker...")
        self._running = False
        if self._redis:
            await self._redis.close()
        if self._repo:
            await self._repo.close()

    # ── Outbox Dispatcher ────────────────────────────────────────────

    async def _outbox_dispatcher(self) -> None:
        """轮询 PostgreSQL outbox → 投递 Redis（事务外，at-least-once）."""
        logger.info("Outbox dispatcher started (poll=%ss)", OUTBOX_POLL_SECONDS)
        while self._running:
            try:
                async with self._repo.session() as sess:  # type: ignore[union-attr]
                    events = await self._repo.get_undispatched_events(sess, limit=50)  # type: ignore[union-attr]
                    if events:
                        dispatched_ids = []
                        for evt in events:
                            try:
                                # 投递到 Redis list
                                await self._redis.rpush(  # type: ignore[union-attr]
                                    QUEUE_KEY,
                                    str(evt.aggregate_id),
                                )
                                dispatched_ids.append(evt.event_id)
                            except Exception:
                                logger.exception("Failed to dispatch event %s", evt.event_id)

                        if dispatched_ids:
                            await self._repo.mark_dispatched(sess, dispatched_ids)  # type: ignore[union-attr]
                            await sess.commit()
                            logger.debug("Dispatched %d outbox events", len(dispatched_ids))
            except Exception:
                logger.exception("Outbox dispatcher error")

            await asyncio.sleep(OUTBOX_POLL_SECONDS)

    # ── Build Worker ─────────────────────────────────────────────────

    async def _recover_processing(self) -> None:
        """Return jobs left in processing after a worker crash."""
        recovered = 0
        while True:
            payload = await self._redis.rpoplpush(  # type: ignore[union-attr]
                PROCESSING_QUEUE_KEY, QUEUE_KEY
            )
            if payload is None:
                break
            recovered += 1
        if recovered:
            logger.info("Recovered %d build jobs", recovered)

    @staticmethod
    def _queue_text(payload: bytes | str) -> str:
        return payload.decode("utf-8") if isinstance(payload, bytes) else payload

    async def _ack_processing(self, payload: bytes | str) -> None:
        """Remove exactly one successfully completed delivery."""
        await self._redis.lrem(  # type: ignore[union-attr]
            PROCESSING_QUEUE_KEY, 1, payload
        )

    async def _build_worker(self) -> None:
        """Reliably claim and process build jobs."""
        logger.info("Build worker started, listening on %s", QUEUE_KEY)
        while self._running:
            try:
                payload = await self._redis.brpoplpush(  # type: ignore[union-attr]
                    QUEUE_KEY, PROCESSING_QUEUE_KEY, timeout=5
                )
                if payload is None:
                    continue

                should_ack = False
                job_id_text = self._queue_text(payload)
                try:
                    job_id = uuid.UUID(job_id_text)
                except (ValueError, AttributeError):
                    logger.error("Discarding malformed build job payload")
                    should_ack = True
                else:
                    logger.info("Received build job: %s", job_id)
                    try:
                        await self._process_job(job_id)
                    except Exception:
                        logger.exception(
                            "Build job %s remains in processing for retry", job_id
                        )
                    else:
                        should_ack = True

                if should_ack:
                    await self._ack_processing(payload)

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Build worker error")
                await asyncio.sleep(1)

    async def _process_job(self, job_id: uuid.UUID) -> None:
        """处理单个构建任务."""
        try:
            await self._orchestrator.run(job_id)  # type: ignore[union-attr]
        except Exception:
            logger.exception("Build job %s failed", job_id)
            # 记录失败状态
            try:
                async with self._repo.session() as sess:  # type: ignore[union-attr]
                    async with sess.begin():
                        await self._repo.update_build_job(  # type: ignore[union-attr]
                            sess, job_id,
                            status="failed",
                            error_code="worker_exception",
                            finished_at=datetime.now(timezone.utc),
                            updated_at=datetime.now(timezone.utc),
                        )
            except Exception:
                logger.exception("Failed to record failure for job %s", job_id)
                raise


# ── Entry Point ───────────────────────────────────────────────────────────

async def main() -> None:
    """Worker 主入口."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    worker = BuildWorker()

    # 优雅关闭
    loop = asyncio.get_running_loop()

    def _shutdown() -> None:
        logger.info("Received shutdown signal")
        asyncio.create_task(worker.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            # Windows 不支持 add_signal_handler
            signal.signal(sig, lambda s, f: asyncio.create_task(worker.stop()))

    try:
        await worker.start()
    except asyncio.CancelledError:
        pass
    finally:
        await worker.stop()


if __name__ == "__main__":
    asyncio.run(main())
