CREATE TABLE IF NOT EXISTS job_queue (
    job_id        TEXT PRIMARY KEY,
    run_key       TEXT NOT NULL UNIQUE,
    status        TEXT NOT NULL DEFAULT 'PENDING',
    attempts      INTEGER NOT NULL DEFAULT 0,
    max_attempts  INTEGER NOT NULL DEFAULT 3,
    lease_until   TEXT,
    created_at    TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT,
    last_error    TEXT,
    payload_json  TEXT
);

CREATE TABLE IF NOT EXISTS job_steps (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        TEXT NOT NULL REFERENCES job_queue(job_id),
    step_name     TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'PENDING',
    attempts      INTEGER NOT NULL DEFAULT 0,
    started_at    TEXT,
    finished_at   TEXT,
    error         TEXT,
    artifact_path TEXT,
    UNIQUE(job_id, step_name)
);

CREATE INDEX IF NOT EXISTS idx_job_queue_status ON job_queue(status);
CREATE INDEX IF NOT EXISTS idx_job_steps_job ON job_steps(job_id);
