import random
import statistics

from app.main import MIN_BASELINE_SAMPLES, SlidingMedian


def test_sliding_median_matches_statistics():
    random.seed(42)
    # events at integer seconds, window = 60 seconds
    times = list(range(200))
    amps = [random.uniform(0, 10) for _ in times]
    sm = SlidingMedian(amps)

    add_p = rm_p = 0
    for i, t in enumerate(times):
        while add_p < len(times) and times[add_p] < t:
            sm._add_idx(add_p)
            add_p += 1
        cutoff = t - 60
        while rm_p < len(times) and times[rm_p] < cutoff:
            if sm.state[rm_p] != 0:
                sm._remove_idx(rm_p)
            rm_p += 1

        window = [amps[j] for j, tj in enumerate(times) if t - 60 <= tj < t]
        if len(window) >= MIN_BASELINE_SAMPLES:
            assert sm.median() == statistics.median(window)
        else:
            assert sm.median() is None


def test_sliding_median_duplicates_and_endpoints():
    # identical amplitudes, window includes t-60 (left closed), excludes t
    times = list(range(80))
    amps = [5.0] * 80
    sm = SlidingMedian(amps)
    add_p = rm_p = 0
    for i, t in enumerate(times):
        while add_p < len(times) and times[add_p] < t:
            sm._add_idx(add_p)
            add_p += 1
        cutoff = t - 60
        while rm_p < len(times) and times[rm_p] < cutoff:
            if sm.state[rm_p] != 0:
                sm._remove_idx(rm_p)
            rm_p += 1
        if i >= MIN_BASELINE_SAMPLES:
            assert sm.median() == 5.0
