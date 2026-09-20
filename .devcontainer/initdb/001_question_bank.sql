-- CAI//OPS 题库学习 - 数据库表创建
-- 对应文档 §5 领域模型 + §7.1 PostgreSQL

-- 启用 UUID 扩展
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ── 题目实例 ── §5.1
CREATE TABLE IF NOT EXISTS questions (
    question_id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    version             INT NOT NULL DEFAULT 1,
    statement_raw       TEXT NOT NULL,
    statement_canonical TEXT NOT NULL,
    instance_fingerprint TEXT NOT NULL UNIQUE,
    success_criteria    JSONB NOT NULL DEFAULT '{}',
    recall_metadata     JSONB NOT NULL DEFAULT '{}',
    current_solution_method_id UUID,
    status              TEXT NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft', 'building', 'usable', 'incomplete')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── 题目资料 ── §5.2
CREATE TABLE IF NOT EXISTS question_materials (
    material_id   UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    question_id   UUID NOT NULL REFERENCES questions(question_id) ON DELETE CASCADE,
    ordinal       INT NOT NULL DEFAULT 0,
    material_type TEXT NOT NULL CHECK (material_type IN (
                      'writeup', 'answer', 'source', 'script',
                      'pcap', 'image', 'archive', 'environment'
                  )),
    object_key    TEXT NOT NULL,
    mime_type     TEXT NOT NULL DEFAULT 'application/octet-stream',
    size_bytes    BIGINT NOT NULL DEFAULT 0,
    sha256        TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'uploaded'
                  CHECK (status IN ('uploaded', 'available', 'failed')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── 运行记录 ── §5.3
CREATE TABLE IF NOT EXISTS run_records (
    run_id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    question_id         UUID NOT NULL REFERENCES questions(question_id) ON DELETE CASCADE,
    session_id          UUID,
    source_solution_id  UUID,
    temporary           BOOLEAN NOT NULL DEFAULT false,
    trigger             TEXT NOT NULL DEFAULT 'autonomous'
                        CHECK (trigger IN ('reference_validation', 'autonomous', 'replay')),
    status              TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued', 'running', 'verified',
                                          'incomplete', 'failed', 'timeout', 'stopped')),
    trace_object_key    TEXT,
    final_output_object_key TEXT,
    verification_result JSONB,
    blocker             TEXT,
    worker_error        TEXT,
    started_at          TIMESTAMPTZ,
    finished_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── 解题方法 ── §5.4
CREATE TABLE IF NOT EXISTS solution_methods (
    solution_id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    question_id         UUID NOT NULL REFERENCES questions(question_id) ON DELETE CASCADE,
    version             INT NOT NULL DEFAULT 1,
    status              TEXT NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft', 'verifying', 'usable', 'superseded', 'failed')),
    overall_approach    TEXT,
    principles          JSONB,
    verified_run_id     UUID REFERENCES run_records(run_id),
    verification_result JSONB,
    replay_plan         JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    superseded_at       TIMESTAMPTZ
);

-- ── 解题步骤 ── §5.4
CREATE TABLE IF NOT EXISTS solution_steps (
    step_id       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    solution_id   UUID NOT NULL REFERENCES solution_methods(solution_id) ON DELETE CASCADE,
    ordinal       INT NOT NULL DEFAULT 0,
    goal          TEXT NOT NULL DEFAULT '',
    why           TEXT NOT NULL DEFAULT '',
    principle     TEXT NOT NULL DEFAULT '',
    action        TEXT NOT NULL DEFAULT '',
    result        TEXT NOT NULL DEFAULT '',
    evidence_refs JSONB NOT NULL DEFAULT '[]',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── 构建任务 ── §7.2
CREATE TABLE IF NOT EXISTS build_jobs (
    job_id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    question_id     UUID NOT NULL REFERENCES questions(question_id) ON DELETE CASCADE,
    run_id          UUID REFERENCES run_records(run_id),
    status          TEXT NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued', 'running', 'verifying_result',
                                      'synthesizing_method', 'verifying_method',
                                      'usable', 'incomplete', 'failed')),
    progress        REAL NOT NULL DEFAULT 0.0,
    retryable       BOOLEAN NOT NULL DEFAULT false,
    blocker         TEXT,
    error_code      TEXT,
    idempotency_key TEXT UNIQUE,
    request_sha256  TEXT,
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── 定位事件（审计）── §7.3
CREATE TABLE IF NOT EXISTS question_resolution_events (
    event_id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id        UUID,
    question_id       UUID REFERENCES questions(question_id),
    status            TEXT NOT NULL,
    confidence        REAL,
    candidate_ids     JSONB,
    reasons           JSONB,
    resolver_version  TEXT NOT NULL DEFAULT 'resolver-v1',
    latency_ms        INT,
    locked            BOOLEAN NOT NULL DEFAULT false,
    message_sha256    TEXT,
    model_version     TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Outbox（投递 Redis）── §7.2
CREATE TABLE IF NOT EXISTS outbox_events (
    event_id      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    aggregate_type TEXT NOT NULL,
    aggregate_id  UUID NOT NULL,
    event_type    TEXT NOT NULL,
    payload       JSONB NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    dispatched_at TIMESTAMPTZ
);

-- ── 索引 ──
CREATE INDEX IF NOT EXISTS idx_questions_status ON questions(status);
CREATE INDEX IF NOT EXISTS idx_questions_fingerprint ON questions(instance_fingerprint);
CREATE INDEX IF NOT EXISTS idx_materials_question ON question_materials(question_id);
CREATE INDEX IF NOT EXISTS idx_run_records_question ON run_records(question_id);
CREATE INDEX IF NOT EXISTS idx_run_records_status ON run_records(status);
CREATE INDEX IF NOT EXISTS idx_solution_methods_question ON solution_methods(question_id);
CREATE INDEX IF NOT EXISTS idx_solution_methods_status ON solution_methods(status);
CREATE INDEX IF NOT EXISTS idx_solution_steps_solution ON solution_steps(solution_id);
CREATE INDEX IF NOT EXISTS idx_build_jobs_question ON build_jobs(question_id);
CREATE INDEX IF NOT EXISTS idx_build_jobs_status ON build_jobs(status);
CREATE INDEX IF NOT EXISTS idx_outbox_dispatched ON outbox_events(dispatched_at)
    WHERE dispatched_at IS NULL;
