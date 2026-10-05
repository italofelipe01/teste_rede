import tempfile
import unittest
from pathlib import Path

from netmon.config import Settings
from netmon.engine import MonitorEngine
from tests.helpers import FakeProber, wait_until


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        FakeProber.instances.clear()
        self.settings = Settings(
            target="8.8.8.8",
            interval_sec=0.2,
            timeout_ms=200,
            outage_min_cycles=2,
            csv_path=self.dir / "monitoramento_rota.csv",
        )
        self.engine = MonitorEngine(self.settings, prober_factory=FakeProber)
        self.engine.start()

    def tearDown(self):
        self.engine.stop()
        self.tmp.cleanup()

    def snapshot(self):
        return self.engine.snapshot()

    def test_full_flow(self):
        engine = self.engine
        wait_until(lambda: len(self.snapshot()["hops"]) == 3 and self.snapshot()["cycle"] >= 3)
        snap = self.snapshot()
        self.assertEqual(snap["phase"], "monitoring")
        self.assertEqual([h["Host_IP"] for h in snap["hops"]], ["192.168.0.1", "100.64.0.1", "8.8.8.8"])
        self.assertTrue(snap["hops"][-1]["Is_Destination"])
        self.assertEqual(snap["status"]["link"], "up")
        self.assertTrue(snap["route"]["complete"])
        self.assertTrue((self.dir / "monitoramento_rota.csv").exists())

        history = engine.history_since(0)
        self.assertGreaterEqual(len(history["points"]), 3)
        self.assertEqual(history["instance"], engine.instance_id)
        latest = history["latest_seq"]
        self.assertEqual(engine.history_since(latest)["points"], [])
        self.assertTrue(engine.history_since(latest, instance="outro")["reset"])

        prober = FakeProber.instances[-1]
        prober.down_hops = {"100.64.0.1", "8.8.8.8"}
        wait_until(lambda: self.snapshot()["status"]["link"] == "down")
        snap = self.snapshot()
        self.assertEqual(snap["status"]["breakpoint_ip"], "192.168.0.1")
        self.assertEqual(snap["diagnosis"]["code"], "outage")
        self.assertIn("outage_start", [e["event"] for e in snap["events"]])

        prober.down_hops = set()
        wait_until(lambda: self.snapshot()["status"]["link"] == "up")
        snap = self.snapshot()
        self.assertEqual(len(snap["outages"]), 1)
        self.assertEqual(snap["outages"][0]["Scope"], "external")
        self.assertEqual(snap["outages"][0]["Last_OK_Hop"], 1)
        self.assertLess(snap["summary"]["availability_pct"], 100)
        self.assertIn("1;", (self.dir / "monitoramento_rota_quedas.csv").read_text(encoding="utf-8"))

    def test_route_change_is_recorded(self):
        wait_until(lambda: len(self.snapshot()["hops"]) == 3)
        prober = FakeProber.instances[-1]
        prober.route = ["192.168.0.1", "10.9.9.9", "8.8.8.8"]
        self.engine.request_rediscover()
        wait_until(lambda: any(e["event"] == "route_changed" for e in self.snapshot()["events"]))
        snap = self.snapshot()
        self.assertEqual([h["Host_IP"] for h in snap["hops"]], ["192.168.0.1", "10.9.9.9", "8.8.8.8"])
        self.assertEqual(snap["route"]["changes"], 1)

    def test_settings_live_update_and_target_change_archives_session(self):
        wait_until(lambda: self.snapshot()["cycle"] >= 2)
        first_session = self.snapshot()["session_id"]
        settings, restarted = self.engine.update_settings({"outage_min_cycles": 5})
        self.assertFalse(restarted)
        self.assertEqual(settings.outage_min_cycles, 5)
        wait_until(lambda: any(e["event"] == "settings_changed" for e in self.snapshot()["events"]))
        with self.assertRaises(ValueError):
            self.engine.update_settings({"target": "não é host"})

        _, restarted = self.engine.update_settings({"target": "1.1.1.1"})
        self.assertTrue(restarted)
        wait_until(
            lambda: self.snapshot().get("session_id") not in (None, first_session) and self.snapshot()["cycle"] >= 1
        )
        snap = self.snapshot()
        self.assertEqual(snap["destino"], "1.1.1.1")
        self.assertEqual(snap["hops"][-1]["Host_IP"], "1.1.1.1")
        archived = self.engine.list_sessions()
        self.assertEqual(len(archived), 1)
        self.assertIn("monitoramento_rota_relatorio.txt", archived[0]["files"])
        self.assertEqual(archived[0]["summary"]["target"], "8.8.8.8")

    def test_export_zip(self):
        wait_until(lambda: self.snapshot()["cycle"] >= 2)
        name, data = self.engine.export_zip()
        self.assertTrue(name.startswith("evidencias_8.8.8.8_") and name.endswith(".zip"))
        self.assertGreater(len(data), 200)

    def test_stop_finalizes_report(self):
        wait_until(lambda: self.snapshot()["cycle"] >= 2)
        self.engine.stop()
        report = (self.dir / "monitoramento_rota_relatorio.txt").read_text(encoding="utf-8")
        self.assertIn("RELATÓRIO DE ESTABILIDADE", report)
        self.assertIn("session_end", (self.dir / "monitoramento_rota_eventos.csv").read_text(encoding="utf-8"))
        self.assertEqual(self.snapshot()["phase"], "stopped")


if __name__ == "__main__":
    unittest.main()
