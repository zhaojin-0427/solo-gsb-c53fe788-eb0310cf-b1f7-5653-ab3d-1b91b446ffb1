"""Vibration spike alerting HTTP API.

PostgreSQL-backed FastAPI service. All writes for a device happen in a single
serialized transaction (advisory lock) so retries never emit duplicate changes.
"""
from __future__ import annotations

import heapq
import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Json
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field, field_validator

WINDOW = timedelta(minutes=60)
MERGE_GAP = timedelta(minutes=5)
MIN_BASELINE_SAMPLES = 20
BASELINE_MULTIPLIER = 3.0
MAX_BATCH = 1000

SCHEMA_SQL = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")


def _bulk(conn: Connection, query: str, seq: list[tuple]) -> None:
    """Execute a parameterized statement once per tuple via pipeline mode."""
    if not seq:
        return
    cur = conn.cursor()
    with conn.pipeline():
        cur.executemany(query, seq)


pool: ConnectionPool = None  # type: ignore


# ---------------------------------------------------------------- models ----

class ReadingIn(BaseModel):
    sample_id: str = Field(min_length=1)
    event_time: datetime
    rev: int = Field(ge=0)
    amplitude: float

    @field_validator("sample_id")
    @classmethod
    def _nonblank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("sample_id must not be blank")
        return v

    @field_validator("event_time")
    @classmethod
    def _utc_whole_second(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("event_time must be timezone-aware UTC")
        v = v.astimezone(timezone.utc)
        if v.microsecond != 0:
            raise ValueError("event_time must be a whole second")
        return v

    @field_validator("amplitude")
    @classmethod
    def _finite_nonneg(cls, v: float) -> float:
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("amplitude must be finite")
        if v < 0:
            raise ValueError("amplitude must be non-negative")
        return v


class BatchIn(BaseModel):
    device_id: str = Field(min_length=1)
    readings: list[ReadingIn] = Field(min_length=1, max_length=MAX_BATCH)

    @field_validator("device_id")
    @classmethod
    def _nonblank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("device_id must not be blank")
        return v


# ------------------------------------------------------------- sliding median

class SlidingMedian:
    """Exact median over a sliding event-time window.

    Two heaps with lazy removal: entry i is flagged 0=absent, 1=in lower
    (max-heap), 2=in upper (min-heap). Stale heap entries are discarded when
    they reach the top.
    """

    def __init__(self, amps: list[float]):
        self.amps = amps
        n = len(amps)
        self.state = [0] * n
        self.lo: list[tuple[float, int]] = []
        self.hi: list[tuple[float, int]] = []
        self.nlo = 0
        self.nhi = 0

    def _clean(self) -> None:
        lo, hi, st = self.lo, self.hi, self.state
        while lo and st[lo[0][1]] != 1:
            heapq.heappop(lo)
        while hi and st[hi[0][1]] != 2:
            heapq.heappop(hi)

    def _add_idx(self, i: int) -> None:
        a = self.amps[i]
        self._clean()
        if self.lo and a <= -self.lo[0][0]:
            self.state[i] = 1
            heapq.heappush(self.lo, (-a, i))
            self.nlo += 1
        else:
            self.state[i] = 2
            heapq.heappush(self.hi, (a, i))
            self.nhi += 1
        self._rebalance()

    def _remove_idx(self, i: int) -> None:
        s = self.state[i]
        if s == 1:
            self.nlo -= 1
        elif s == 2:
            self.nhi -= 1
        self.state[i] = 0
        self._rebalance()

    def _rebalance(self) -> None:
        total = self.nlo + self.nhi
        want = (total + 1) // 2
        while self.nlo > want:
            self._clean()
            neg_a, i = heapq.heappop(self.lo)
            if self.state[i] != 1:
                continue
            a = -neg_a
            self.state[i] = 2
            self.nlo -= 1
            heapq.heappush(self.hi, (a, i))
            self.nhi += 1
        while self.nlo < want:
            self._clean()
            a, i = heapq.heappop(self.hi)
            if self.state[i] != 2:
                continue
            self.state[i] = 1
            self.nhi -= 1
            heapq.heappush(self.lo, (-a, i))
            self.nlo += 1

    def median(self) -> Optional[float]:
        self._clean()
        total = self.nlo + self.nhi
        if total < MIN_BASELINE_SAMPLES:
            return None
        if self.nlo > self.nhi:
            return -self.lo[0][0]
        return (-self.lo[0][0] + self.hi[0][0]) / 2.0


# --------------------------------------------------------------- serialization

def iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def alert_json(row: dict) -> dict:
    return {
        "alert_id": row["alert_id"],
        "device_id": row["device_id"],
        "first_sample_id": row["first_sample_id"],
        "last_sample_id": row["last_sample_id"],
        "starts_at": iso(row["starts_at"]),
        "ends_at": iso(row["ends_at"]),
        "hit_count": row["hit_count"],
        "max_amplitude": row["max_amplitude"],
        "updated_at": iso(row["updated_at"]),
    }


# --------------------------------------------------------------------- app ----

app = FastAPI(title="Vibration Spike Alert API", version="1.0.0")


class _Conflict(Exception):
    """Same revision, different content — whole batch must roll back."""

    def __init__(self, device_id: str, results: list[dict]):
        self.device_id = device_id
        self.results = results


@app.exception_handler(_Conflict)
def conflict_handler(request: Request, exc: _Conflict) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={"device_id": exc.device_id,
                 "error": "revision conflict", "results": exc.results},
    )


@app.on_event("startup")
def open_pool() -> None:
    global pool
    pool = ConnectionPool(
        os.environ.get("DATABASE_URL", "postgresql://postgres@127.0.0.1:5432/postgres"),
        min_size=2,
        max_size=10,
        kwargs={"row_factory": dict_row},
        open=True,
    )
    with pool.connection() as conn:
        conn.execute(SCHEMA_SQL)
        conn.commit()


@app.on_event("shutdown")
def close_pool() -> None:
    if pool is not None:
        pool.close()


@app.get("/healthz")
def healthz() -> dict:
    with pool.connection() as conn:
        conn.execute("SELECT 1").fetchone()
    return {"status": "ok"}


# --------------------------------------------------------------- POST readings

def _recompute_baselines(conn: Connection, device_id: str,
                         lo: datetime, hi: datetime) -> dict[str, tuple[Optional[float], str]]:
    """Recompute baseline/verdict for every current reading in [lo, hi].

    A reading r is affected by the batch iff its 60-minute baseline window can
    contain an applied reading; that is exactly r.event_time in
    [min applied time, max applied time + 60 min].
    """
    rows = conn.execute(
        """
        SELECT sample_id, event_time, amplitude
          FROM readings
         WHERE device_id = %s
           AND event_time >= %s
           AND event_time <= %s
         ORDER BY event_time, sample_id
        """,
        (device_id, lo - WINDOW, hi),
    ).fetchall()

    times = [r["event_time"] for r in rows]
    amps = [float(r["amplitude"]) for r in rows]
    sm = SlidingMedian(amps)

    out: dict[str, tuple[Optional[float], str]] = {}
    add_p = rm_p = 0
    n = len(rows)
    for i in range(n):
        t = times[i]
        if not (lo <= t <= hi):
            continue
        while add_p < n and times[add_p] < t:  # right end exclusive (same second excluded)
            sm._add_idx(add_p)
            add_p += 1
        cutoff = t - WINDOW
        while rm_p < n and times[rm_p] < cutoff:  # left end inclusive
            if sm.state[rm_p] != 0:
                sm._remove_idx(rm_p)
            rm_p += 1
        baseline = sm.median()
        if baseline is None:
            verdict = "unknown"
        elif amps[i] > BASELINE_MULTIPLIER * baseline:
            verdict = "hit"
        else:
            verdict = "normal"
        out[rows[i]["sample_id"]] = (baseline, verdict)

    if out:
        _bulk(conn,
              "UPDATE readings SET baseline = %s, verdict = %s "
              "WHERE device_id = %s AND sample_id = %s",
              [(b, vd, device_id, sid) for sid, (b, vd) in out.items()])
    return out


def _rebuild_alerts(conn: Connection, device_id: str, now: datetime) -> list[dict]:
    """Rebuild all current alerts for one device from its hit verdicts.

    Hits ordered by event time (sample_id as tie-break) form runs; adjacent hits
    more than 5 minutes apart split runs. An alert id is the first hit's
    sample id. Emits created/updated/revoked change rows for the diff.
    """
    hits = conn.execute(
        """
        SELECT sample_id, event_time, amplitude
          FROM readings
         WHERE device_id = %s AND verdict = 'hit'
         ORDER BY event_time, sample_id
        """,
        (device_id,),
    ).fetchall()

    runs: list[dict] = []
    for h in hits:
        if runs and h["event_time"] - runs[-1]["ends_at"] <= MERGE_GAP:
            run = runs[-1]
            run["last_sample_id"] = h["sample_id"]
            run["ends_at"] = h["event_time"]
            run["hit_count"] += 1
            run["max_amplitude"] = max(run["max_amplitude"], float(h["amplitude"]))
        else:
            runs.append({
                "alert_id": h["sample_id"],
                "first_sample_id": h["sample_id"],
                "last_sample_id": h["sample_id"],
                "starts_at": h["event_time"],
                "ends_at": h["event_time"],
                "hit_count": 1,
                "max_amplitude": float(h["amplitude"]),
            })

    current = {r["alert_id"]: r for r in runs}
    existing_rows = conn.execute(
        "SELECT * FROM alerts WHERE device_id = %s ORDER BY starts_at, alert_id",
        (device_id,),
    ).fetchall()
    existing = {r["alert_id"]: r for r in existing_rows}

    compare_fields = ("first_sample_id", "last_sample_id", "starts_at",
                      "ends_at", "hit_count", "max_amplitude")
    changes: list[tuple] = []

    for alert_id, old in existing.items():
        if alert_id not in current:
            changes.append((device_id, alert_id, "revoked", now, {
                "alert_id": alert_id,
                "device_id": device_id,
                "first_sample_id": old["first_sample_id"],
                "last_sample_id": old["last_sample_id"],
                "starts_at": iso(old["starts_at"]),
                "ends_at": iso(old["ends_at"]),
                "hit_count": old["hit_count"],
                "max_amplitude": old["max_amplitude"],
                "updated_at": iso(old["updated_at"]),
            }))
    if changes:
        conn.execute(
            "DELETE FROM alerts WHERE device_id = %s", (device_id,))

    upserts: list[tuple] = []
    for run in runs:
        old = existing.get(run["alert_id"])
        upserts.append((
            run["alert_id"], device_id, run["first_sample_id"],
            run["last_sample_id"], run["starts_at"], run["ends_at"],
            run["hit_count"], run["max_amplitude"], now,
        ))
        if old is None:
            op = "created"
        elif any(run[f] != old[f] for f in compare_fields):
            op = "updated"
        else:
            op = None
        if op:
            changes.append((device_id, run["alert_id"], op, now, {
                "alert_id": run["alert_id"],
                "device_id": device_id,
                "first_sample_id": run["first_sample_id"],
                "last_sample_id": run["last_sample_id"],
                "starts_at": iso(run["starts_at"]),
                "ends_at": iso(run["ends_at"]),
                "hit_count": run["hit_count"],
                "max_amplitude": run["max_amplitude"],
                "updated_at": iso(now),
            }))

    if upserts:
        _bulk(conn,
            """
            INSERT INTO alerts
                (alert_id, device_id, first_sample_id, last_sample_id,
                 starts_at, ends_at, hit_count, max_amplitude, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (device_id, alert_id) DO UPDATE SET
                first_sample_id = EXCLUDED.first_sample_id,
                last_sample_id  = EXCLUDED.last_sample_id,
                starts_at       = EXCLUDED.starts_at,
                ends_at         = EXCLUDED.ends_at,
                hit_count       = EXCLUDED.hit_count,
                max_amplitude   = EXCLUDED.max_amplitude,
                updated_at      = EXCLUDED.updated_at
            """,
            upserts)

    if changes:
        _bulk(conn,
              "INSERT INTO changes (device_id, alert_id, op, changed_at, data) "
              "VALUES (%s, %s, %s, %s, %s)",
              [(d, a, op, ts, Json(payload)) for d, a, op, ts, payload in changes])
    return changes


@app.post("/readings")
def post_readings(batch: BatchIn):
    device_id = batch.device_id
    items = batch.readings

    dup_ids = [sid for sid, n in Counter(i.sample_id for i in items).items() if n > 1]
    if dup_ids:
        raise HTTPException(422, f"duplicate sample_id within batch: {sorted(dup_ids)}")

    with pool.connection() as conn:
        with conn.transaction():
            # Serialize concurrent writers of the same device.
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (device_id,),
            )
            now = conn.execute("SELECT now()").fetchone()["now"]

            existing = {
                r["sample_id"]: r
                for r in conn.execute(
                    """SELECT sample_id, rev, event_time, amplitude,
                              baseline, verdict
                         FROM readings
                        WHERE device_id = %s
                          AND sample_id = ANY(%s)
                        FOR UPDATE""",
                    (device_id, [i.sample_id for i in items]),
                ).fetchall()
            }

            results: list[dict] = []
            to_insert: list[tuple] = []
            to_update: list[tuple] = []
            applied_times: list[datetime] = []
            conflict = False

            for item in items:
                old = existing.get(item.sample_id)
                t = item.event_time
                a = item.amplitude
                base = {"sample_id": item.sample_id, "rev": item.rev,
                        "event_time": iso(t), "amplitude": a}
                if old is None:
                    to_insert.append((item.sample_id, device_id, item.rev, t, a))
                    applied_times.append(t)
                    results.append(base | {"status": "accepted",
                                           "baseline": None, "verdict": None})
                elif item.rev < old["rev"]:
                    results.append(base | {
                        "status": "stale",
                        "event_time": iso(old["event_time"]),
                        "amplitude": old["amplitude"],
                        "baseline": old["baseline"],
                        "verdict": old["verdict"],
                    })
                elif item.rev == old["rev"]:
                    same = t == old["event_time"] and a == float(old["amplitude"])
                    if same:
                        results.append(base | {
                            "status": "duplicate",
                            "baseline": old["baseline"],
                            "verdict": old["verdict"],
                        })
                    else:
                        conflict = True
                        results.append(base | {
                            "status": "conflict",
                            "stored_event_time": iso(old["event_time"]),
                            "stored_amplitude": old["amplitude"],
                            "stored_rev": old["rev"],
                            "baseline": None, "verdict": None,
                        })
                else:
                    to_update.append((item.rev, t, a, device_id, item.sample_id))
                    applied_times.append(t)
                    results.append(base | {"status": "accepted",
                                           "baseline": None, "verdict": None})

            if conflict:
                # Atomic batch: nothing is written; exception rolls the
                # transaction back and the handler renders the 409 response.
                raise _Conflict(device_id, results)

            if to_insert:
                _bulk(conn,
                    """
                    INSERT INTO readings
                        (sample_id, device_id, rev, event_time, amplitude)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    to_insert)
            if to_update:
                _bulk(conn,
                    """
                    UPDATE readings
                       SET rev = %s, event_time = %s, amplitude = %s,
                           baseline = NULL, verdict = 'unknown'
                     WHERE device_id = %s AND sample_id = %s
                    """,
                    [(rev, t, a, dev, sid) for rev, t, a, dev, sid in to_update])

            recomputed: dict[str, tuple[Optional[float], str]] = {}
            if applied_times:
                lo = min(applied_times)
                hi = max(applied_times) + WINDOW
                recomputed = _recompute_baselines(conn, device_id, lo, hi)

            for r in results:
                if r["status"] in ("accepted", "duplicate"):
                    bv = recomputed.get(r["sample_id"])
                    if bv is not None:
                        r["baseline"], r["verdict"] = bv
                    # duplicates outside the affected window keep stored values

            if applied_times:
                _rebuild_alerts(conn, device_id, now)

        return {"device_id": device_id, "results": results}


# ------------------------------------------------------------------ GET alerts

@app.get("/alerts")
def get_alerts(
    device_id: Optional[str] = None,
    limit: int = Query(500, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    with pool.connection() as conn:
        if device_id is None:
            rows = conn.execute(
                """SELECT * FROM alerts
                   ORDER BY starts_at, device_id, alert_id
                   LIMIT %s OFFSET %s""",
                (limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM alerts
                   WHERE device_id = %s
                   ORDER BY starts_at, alert_id
                   LIMIT %s OFFSET %s""",
                (device_id, limit, offset),
            ).fetchall()
    return {"alerts": [alert_json(r) for r in rows]}


# ----------------------------------------------------------------- GET changes

@app.get("/changes")
def get_changes(
    device_id: Optional[str] = None,
    cursor: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
):
    with pool.connection() as conn:
        if device_id is None:
            rows = conn.execute(
                """SELECT * FROM changes
                   WHERE id > %s
                   ORDER BY id
                   LIMIT %s""",
                (cursor, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM changes
                   WHERE device_id = %s AND id > %s
                   ORDER BY id
                   LIMIT %s""",
                (device_id, cursor, limit),
            ).fetchall()

    changes = [{
        "id": r["id"],
        "device_id": r["device_id"],
        "alert_id": r["alert_id"],
        "op": r["op"],
        "changed_at": iso(r["changed_at"]),
        "data": r["data"],
    } for r in rows]
    next_cursor = rows[-1]["id"] if rows else cursor
    return {"changes": changes, "next_cursor": next_cursor,
            "has_more": len(rows) == limit}
