-- Vibration spike alerting API — schema (idempotent)

CREATE TABLE IF NOT EXISTS readings (
    device_id   TEXT        NOT NULL,
    sample_id   TEXT        NOT NULL,
    rev         BIGINT      NOT NULL,
    event_time  TIMESTAMPTZ NOT NULL,
    amplitude   DOUBLE PRECISION NOT NULL,
    baseline    DOUBLE PRECISION,
    verdict     TEXT        NOT NULL DEFAULT 'unknown'
                CHECK (verdict IN ('hit', 'normal', 'unknown')),
    PRIMARY KEY (device_id, sample_id)
);

-- "previous 60 minutes, same device" window lookups (ordered by event time)
CREATE INDEX IF NOT EXISTS readings_device_time_idx
    ON readings (device_id, event_time, sample_id)
    INCLUDE (amplitude);

CREATE TABLE IF NOT EXISTS alerts (
    alert_id          TEXT        NOT NULL,           -- sample_id of first hit
    device_id         TEXT        NOT NULL,
    first_sample_id   TEXT        NOT NULL,
    last_sample_id    TEXT        NOT NULL,
    starts_at         TIMESTAMPTZ NOT NULL,
    ends_at           TIMESTAMPTZ NOT NULL,
    hit_count         INTEGER     NOT NULL,
    max_amplitude     DOUBLE PRECISION NOT NULL,
    updated_at        TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (device_id, alert_id)
);

CREATE INDEX IF NOT EXISTS alerts_device_time_idx
    ON alerts (device_id, starts_at, alert_id);

CREATE TABLE IF NOT EXISTS changes (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    device_id   TEXT        NOT NULL,
    alert_id    TEXT        NOT NULL,
    op          TEXT        NOT NULL CHECK (op IN ('created', 'updated', 'revoked')),
    changed_at  TIMESTAMPTZ NOT NULL,
    data        JSONB       NOT NULL
);

-- cursor-ordered reads
CREATE INDEX IF NOT EXISTS changes_id_idx ON changes (id);
CREATE INDEX IF NOT EXISTS changes_device_idx ON changes (device_id, id);
