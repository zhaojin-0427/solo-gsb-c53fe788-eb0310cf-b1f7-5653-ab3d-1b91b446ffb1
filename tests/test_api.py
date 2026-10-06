"""End-to-end tests against a running API (BASE_URL env, default localhost).

Run: pytest tests/test_api.py
Requires the API + Postgres; uses unique device ids so runs are independent.
"""
import os
import time
import uuid

import httpx
import pytest

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8000")
T0 = "2026-10-01T00:00:00Z"


def ts(seconds: int) -> str:
    import datetime as dt
    t = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc) + dt.timedelta(seconds=seconds)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(scope="module")
def client():
    with httpx.Client(base_url=BASE, timeout=30) as c:
        yield c


def new_device():
    return "dev-" + uuid.uuid4().hex[:12]


def post(client, device, readings):
    return client.post("/readings", json={"device_id": device, "readings": readings})


def by_id(resp):
    return {r["sample_id"]: r for r in resp.json()["results"]}


def test_unknown_then_baseline_then_hit(client):
    dev = new_device()
    # first 19 normal readings: < 20 samples -> unknown
    batch = [{"sample_id": f"s{i:03d}", "rev": 0, "event_time": ts(i),
              "amplitude": 1.0} for i in range(19)]
    r = post(client, dev, batch)
    assert r.status_code == 200
    assert all(x["verdict"] == "unknown" for x in r.json()["results"])

    # 20th reading: exactly 19 prior samples -> still unknown
    r = post(client, dev, [{"sample_id": "s019", "rev": 0,
                            "event_time": ts(19), "amplitude": 1.0}])
    assert by_id(r)["s019"]["verdict"] == "unknown"

    # 21st reading: median of 20 ones == 1.0, amp 1.0 not strictly > 3 -> normal
    r = post(client, dev, [{"sample_id": "s020", "rev": 0,
                            "event_time": ts(20), "amplitude": 1.0}])
    assert by_id(r)["s020"]["baseline"] == 1.0
    assert by_id(r)["s020"]["verdict"] == "normal"

    # spike: amp 3.0 is NOT > 3 * 1.0; amp 3.01 is
    r = post(client, dev, [{"sample_id": "s021", "rev": 0,
                            "event_time": ts(21), "amplitude": 3.0}])
    assert by_id(r)["s021"]["verdict"] == "normal"
    r = post(client, dev, [{"sample_id": "s022", "rev": 0,
                            "event_time": ts(22), "amplitude": 3.01}])
    assert by_id(r)["s022"]["verdict"] == "hit"

    alerts = client.get("/alerts", params={"device_id": dev}).json()["alerts"]
    assert [(a["alert_id"], a["hit_count"]) for a in alerts] == [("s022", 1)]
    assert alerts[0]["max_amplitude"] == 3.01


def test_merging_only_adjacent_hits_within_5min(client):
    dev = new_device()
    readings = [{"sample_id": f"b{i:02d}", "rev": 0, "event_time": ts(100 + i),
                 "amplitude": 1.0} for i in range(25)]
    post(client, dev, readings)
    # two hits 60s apart -> merged into one alert, id = first hit
    post(client, dev, [
        {"sample_id": "h1", "rev": 0, "event_time": ts(200), "amplitude": 10.0},
        {"sample_id": "h2", "rev": 0, "event_time": ts(260), "amplitude": 10.0},
    ])
    alerts = client.get("/alerts", params={"device_id": dev}).json()["alerts"]
    assert len(alerts) == 1
    a = alerts[0]
    assert a["alert_id"] == "h1"
    assert a["first_sample_id"] == "h1" and a["last_sample_id"] == "h2"
    assert a["hit_count"] == 2

    # third hit > 5 minutes later -> new alert
    post(client, dev, [{"sample_id": "h3", "rev": 0,
                        "event_time": ts(561), "amplitude": 10.0}])
    alerts = client.get("/alerts", params={"device_id": dev}).json()["alerts"]
    assert [x["alert_id"] for x in alerts] == ["h1", "h3"]

    # exactly 300 s apart (gap from h2 at 260 to a new hit at 560) -> merges
    post(client, dev, [{"sample_id": "h4", "rev": 0,
                        "event_time": ts(560), "amplitude": 10.0}])
    alerts = client.get("/alerts", params={"device_id": dev}).json()["alerts"]
    # h4 sorts before h3 (560 < 561) and is 300s after h2: merges with h1 run;
    # h3 is 1s after h4 -> same run too
    assert [x["alert_id"] for x in alerts] == ["h1"]
    assert alerts[0]["last_sample_id"] == "h3"
    assert alerts[0]["hit_count"] == 4


def test_stale_revision_ignored(client):
    dev = new_device()
    post(client, dev, [{"sample_id": "x", "rev": 2,
                        "event_time": ts(1000), "amplitude": 5.0}])
    r = post(client, dev, [{"sample_id": "x", "rev": 1,
                            "event_time": ts(1000), "amplitude": 9.0}])
    assert by_id(r)["x"]["status"] == "stale"
    assert by_id(r)["x"]["amplitude"] == 5.0


def test_conflict_rollback_and_retry_idempotent(client):
    dev = new_device()
    base = [{"sample_id": f"c{i:02d}", "rev": 0, "event_time": ts(2000 + i),
             "amplitude": 1.0} for i in range(25)]
    post(client, dev, base)
    # hit + an unrelated new reading in the same conflicting batch: both roll back
    body = [
        {"sample_id": "c00", "rev": 0, "event_time": ts(2000), "amplitude": 2.0},
        {"sample_id": "new1", "rev": 0, "event_time": ts(2010), "amplitude": 9.0},
    ]
    r = post(client, dev, body)
    assert r.status_code == 409
    statuses = {x["sample_id"]: x["status"] for x in r.json()["results"]}
    assert statuses == {"c00": "conflict", "new1": "accepted"}
    # nothing written: alerts empty, new1 absent on retry of just new1
    assert client.get("/alerts", params={"device_id": dev}).json()["alerts"] == []

    # higher revision succeeds
    r = post(client, dev, [{"sample_id": "c00", "rev": 1,
                            "event_time": ts(2000), "amplitude": 2.0}])
    assert by_id(r)["c00"]["status"] == "accepted"

    # identical re-POST: duplicate, no new change rows
    c1 = client.get("/changes", params={"device_id": dev}).json()
    r = post(client, dev, [{"sample_id": "c00", "rev": 1,
                            "event_time": ts(2000), "amplitude": 2.0}])
    assert by_id(r)["c00"]["status"] == "duplicate"
    c2 = client.get("/changes", params={"device_id": dev}).json()
    assert [x["id"] for x in c1["changes"]] == [x["id"] for x in c2["changes"]]


def test_correction_recomputes_and_revokes_alerts(client):
    dev = new_device()
    # 25 calm readings + a spike at t=600
    calm = [{"sample_id": f"d{i:02d}", "rev": 0, "event_time": ts(600 - 25 + i),
             "amplitude": 1.0} for i in range(25)]
    post(client, dev, calm)
    post(client, dev, [{"sample_id": "spike", "rev": 0,
                        "event_time": ts(600), "amplitude": 10.0}])
    alerts = client.get("/alerts", params={"device_id": dev}).json()["alerts"]
    assert [a["alert_id"] for a in alerts] == ["spike"]

    # correct the spike down to 1.0 with a higher revision -> verdict normal,
    # alert revoked
    r = post(client, dev, [{"sample_id": "spike", "rev": 1,
                            "event_time": ts(600), "amplitude": 1.0}])
    assert by_id(r)["spike"]["verdict"] == "normal"
    assert client.get("/alerts", params={"device_id": dev}).json()["alerts"] == []

    changes = client.get("/changes", params={"device_id": dev}).json()["changes"]
    ops = [(c["alert_id"], c["op"]) for c in changes]
    assert ("spike", "created") in ops
    assert ("spike", "revoked") in ops
    # revoke comes after create (monotonic cursor)
    assert [o for _, o in ops].index("created") < [o for _, o in ops].index("revoked")


def test_changes_cursor_continuation(client):
    dev = new_device()
    calm = [{"sample_id": f"e{i:02d}", "rev": 0, "event_time": ts(3000 + i),
             "amplitude": 1.0} for i in range(25)]
    post(client, dev, calm)
    post(client, dev, [{"sample_id": "z1", "rev": 0,
                        "event_time": ts(3100), "amplitude": 9.0}])
    first = client.get("/changes", params={"device_id": dev,
                                           "limit": 1}).json()
    assert first["has_more"] is True
    cur = first["next_cursor"]
    second = client.get("/changes", params={"device_id": dev,
                                            "cursor": cur, "limit": 100}).json()
    assert all(c["id"] > cur for c in second["changes"])
    seen = {c["id"] for c in first["changes"]} | {c["id"]
                                                  for c in second["changes"]}
    allc = client.get("/changes", params={"device_id": dev,
                                          "limit": 1000}).json()["changes"]
    assert seen == {c["id"] for c in allc}


def test_validation_errors(client):
    dev = new_device()
    r = post(client, dev, [{"sample_id": "a", "rev": -1,
                            "event_time": T0, "amplitude": 1.0}])
    assert r.status_code == 422
    r = post(client, dev, [{"sample_id": "a", "rev": 0,
                            "event_time": T0, "amplitude": -0.1}])
    assert r.status_code == 422
    r = post(client, dev, [{"sample_id": "a", "rev": 0,
                            "event_time": "2026-10-01T00:00:00.500Z",
                            "amplitude": 1.0}])
    assert r.status_code == 422
    r = client.post("/readings", json={
        "device_id": dev,
        "readings": [{"sample_id": "a", "rev": 0,
                      "event_time": T0, "amplitude": "Infinity"}],
    })
    assert r.status_code == 422
    r = post(client, dev, [
        {"sample_id": "a", "rev": 0, "event_time": T0, "amplitude": 1.0},
        {"sample_id": "a", "rev": 0, "event_time": T0, "amplitude": 2.0},
    ])
    assert r.status_code == 422


def test_perf_100k_then_append_p95(client):
    dev = "perf-" + uuid.uuid4().hex[:8]
    # 100k readings over one day (same second allowed), amplitude 1
    N = 100_000
    BATCH = 1000

    import datetime as dt
    day0 = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)

    def et(i):
        sec = int(i * 86_400 / N)
        return (day0 + dt.timedelta(seconds=sec)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")

    t0 = time.perf_counter()
    for b in range(N // BATCH):
        lo = b * BATCH
        body = [{"sample_id": f"p{lo + i:06d}", "rev": 0, "event_time": et(lo + i),
                 "amplitude": 1.0} for i in range(BATCH)]
        r = post(client, dev, body)
        assert r.status_code == 200, r.text[:300]
    load_s = time.perf_counter() - t0

    # 20 timed pure-append batches of 100 after the 100k
    day_end = day0 + dt.timedelta(seconds=86_400)
    lat = []
    for k in range(20):
        base_idx = N + k * 100
        body = [{"sample_id": f"p{base_idx + i:06d}", "rev": 0,
                 "event_time": (day_end + dt.timedelta(seconds=k * 100 + i)
                                ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "amplitude": 1.0} for i in range(100)]
        t1 = time.perf_counter()
        r = post(client, dev, body)
        lat.append((time.perf_counter() - t1) * 1000)
        assert r.status_code == 200
    lat.sort()
    p95 = lat[int(0.95 * (len(lat) - 1))]
    print(f"\nload 100k in {load_s:.1f}s; append-100 latencies ms: "
          f"min={lat[0]:.0f} med={lat[len(lat)//2]:.0f} p95={p95:.0f} max={lat[-1]:.0f}")
    assert p95 <= 1000, f"P95 {p95:.0f}ms exceeds 1000ms"
