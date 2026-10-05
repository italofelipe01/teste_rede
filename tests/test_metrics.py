import unittest
from datetime import datetime, timedelta

from netmon.metrics import (
    SCOPE_EXTERNAL,
    SCOPE_LOCAL,
    SCOPE_UNKNOWN,
    HopStats,
    OutageTracker,
    availability_pct,
    diagnose,
    farthest_responding,
)

T0 = datetime(2026, 1, 1, 12, 0, 0)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


class HopStatsTests(unittest.TestCase):
    def test_latency_aggregates(self):
        hop = HopStats(hop=1, host="10.0.0.1")
        for value in (10.0, 20.0, 30.0):
            hop.register_success(value)
        hop.register_timeout()
        self.assertEqual((hop.sent, hop.recv), (4, 3))
        self.assertAlmostEqual(hop.avg, 20.0)
        self.assertEqual((hop.best, hop.worst), (10.0, 30.0))
        self.assertIsNone(hop.last)
        self.assertAlmostEqual(hop.loss_pct, 25.0)
        self.assertAlmostEqual(hop.stdev, 8.16496580927726)
        self.assertAlmostEqual(hop.jitter, 10.0)

    def test_jitter_resets_after_timeout(self):
        hop = HopStats(hop=1, host="10.0.0.1")
        hop.register_success(10.0)
        hop.register_timeout()
        hop.register_success(50.0)
        self.assertIsNone(hop.jitter)

    def test_recent_window(self):
        hop = HopStats(hop=1, host="10.0.0.1", window=4)
        for _ in range(4):
            hop.register_timeout()
        for value in (5.0, 7.0, 9.0, 11.0):
            hop.register_success(value)
        self.assertEqual(hop.recent_loss_pct, 0.0)
        self.assertAlmostEqual(hop.loss_pct, 50.0)
        self.assertAlmostEqual(hop.recent_avg, 8.0)
        self.assertAlmostEqual(hop.recent_jitter, 2.0)

    def test_no_samples(self):
        hop = HopStats(hop=1, host="10.0.0.1")
        self.assertEqual(hop.loss_pct, 0.0)
        self.assertIsNone(hop.avg)
        self.assertIsNone(hop.stdev)


class OutageTrackerTests(unittest.TestCase):
    def test_single_failure_below_threshold_is_not_outage(self):
        tracker = OutageTracker(min_cycles=3)
        tracker.update(False, at(0))
        tracker.update(False, at(1))
        self.assertEqual(tracker.pending_cycles, 2)
        self.assertIsNone(tracker.update(True, at(2)))
        self.assertEqual(tracker.records, [])
        self.assertFalse(tracker.is_down)
        self.assertEqual(tracker.pending_cycles, 0)

    def test_outage_starts_at_first_failure_and_ends_on_reply(self):
        tracker = OutageTracker(min_cycles=3)
        hop = (2, "10.0.0.2")
        self.assertIsNone(tracker.update(False, at(10), hop, True))
        self.assertIsNone(tracker.update(False, at(11), hop, True))
        self.assertEqual(tracker.update(False, at(12), hop, True), "down")
        self.assertTrue(tracker.is_down)
        self.assertEqual(tracker.down_start, at(10))
        self.assertAlmostEqual(tracker.current_outage_sec(at(15)), 5.0)
        self.assertAlmostEqual(tracker.total_downtime_sec(at(15)), 5.0)
        tracker.update(False, at(13), hop, True)
        self.assertEqual(tracker.update(True, at(20)), "up")
        record = tracker.records[0]
        self.assertEqual((record.start, record.end, record.duration_sec), (at(10), at(20), 10.0))
        self.assertEqual(record.down_cycles, 4)
        self.assertEqual((record.last_ok_hop, record.last_ok_ip, record.scope), (2, "10.0.0.2", SCOPE_EXTERNAL))
        self.assertEqual(tracker.total_outages, 1)

    def test_scope_local_and_unknown(self):
        tracker = OutageTracker(min_cycles=1)
        tracker.update(False, at(0), None, True)
        tracker.update(True, at(5))
        tracker.update(False, at(10), None, False)
        tracker.update(True, at(12))
        self.assertEqual([r.scope for r in tracker.records], [SCOPE_LOCAL, SCOPE_UNKNOWN])

    def test_close_open_outage(self):
        tracker = OutageTracker(min_cycles=1)
        tracker.update(False, at(0))
        tracker.close_open_outage(at(30))
        self.assertFalse(tracker.is_down)
        self.assertEqual(tracker.records[0].duration_sec, 30.0)

    def test_backward_compatible_default_threshold(self):
        tracker = OutageTracker()
        self.assertEqual(tracker.update(False, at(0)), "down")


class HelpersTests(unittest.TestCase):
    def test_availability(self):
        self.assertEqual(availability_pct(0, 0), 100.0)
        self.assertAlmostEqual(availability_pct(100, 1), 99.0)
        self.assertEqual(availability_pct(10, 50), 0.0)

    def test_farthest_responding_ignores_destination(self):
        stats = [HopStats(1, "a"), HopStats(2, "b"), HopStats(3, "c", is_destination=True)]
        self.assertEqual(farthest_responding(stats, [1.0, 2.0, 3.0]), (2, "b"))
        self.assertEqual(farthest_responding(stats, [1.0, None, None]), (1, "a"))
        self.assertIsNone(farthest_responding(stats, [None, None, 3.0]))


def build_route(losses: list[float], samples: int = 100) -> list[HopStats]:
    stats = []
    for idx, loss in enumerate(losses, start=1):
        hop = HopStats(hop=idx, host=f"10.0.0.{idx}", is_destination=idx == len(losses), window=samples)
        lost = round(samples * loss / 100)
        for n in range(samples):
            if n < lost:
                hop.register_timeout()
            else:
                hop.register_success(10.0 + idx)
        stats.append(hop)
    return stats


class DiagnosisTests(unittest.TestCase):
    def test_stable(self):
        result = diagnose(build_route([0, 0, 0]), OutageTracker())
        self.assertEqual((result.level, result.code), ("ok", "stable"))

    def test_intermediate_icmp_limit_is_not_a_problem(self):
        result = diagnose(build_route([0, 60, 0]), OutageTracker())
        self.assertEqual((result.level, result.code), ("ok", "intermediate_icmp_limit"))

    def test_loss_starting_at_first_hop_is_local(self):
        result = diagnose(build_route([20, 20, 20]), OutageTracker())
        self.assertEqual((result.level, result.code, result.suspect_hop), ("bad", "loss_local", 1))

    def test_loss_starting_later_is_external(self):
        result = diagnose(build_route([0, 5, 5, 5]), OutageTracker())
        self.assertEqual((result.level, result.code, result.suspect_hop), ("warn", "loss_external", 2))

    def test_destination_never_responded(self):
        result = diagnose(build_route([0, 100]), OutageTracker())
        self.assertEqual(result.code, "destination_never_responded")

    def test_outage_in_progress(self):
        tracker = OutageTracker(min_cycles=1)
        tracker.update(False, at(0), (2, "10.0.0.2"), True)
        result = diagnose(build_route([0, 0, 0]), tracker)
        self.assertEqual((result.level, result.code, result.suspect_hop), ("bad", "outage", 2))

    def test_single_lost_packet_is_not_flagged(self):
        result = diagnose(build_route([0, 0, 4], samples=25), OutageTracker())
        self.assertEqual(result.code, "stable")

    def test_collecting(self):
        result = diagnose(build_route([0, 0], samples=3), OutageTracker())
        self.assertEqual(result.code, "collecting")


if __name__ == "__main__":
    unittest.main()
