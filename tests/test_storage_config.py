import json
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path

from netmon.config import Settings, apply_changes, load_settings, save_user_config
from netmon.metrics import HopStats, OutageTracker
from netmon.storage import (
    LATENCY_HEADERS,
    CsvAppender,
    EvidencePaths,
    archive_previous_session,
    build_report,
    build_zip,
    list_archives,
    parse_summary,
    prune_archives,
    resolve_archive,
    write_outages_csv,
    write_snapshot_csv,
    write_summary,
)

T0 = datetime(2026, 1, 1, 12, 0, 0)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.paths = EvidencePaths.from_csv(self.dir / "monitoramento_rota.csv")

    def tearDown(self):
        self.tmp.cleanup()

    def test_paths_follow_contract(self):
        names = [p.name for p in self.paths.all()]
        self.assertEqual(
            names,
            [
                "monitoramento_rota.csv",
                "monitoramento_rota_quedas.csv",
                "monitoramento_rota_resumo.txt",
                "monitoramento_rota_latencia_log.csv",
                "monitoramento_rota_eventos.csv",
                "monitoramento_rota_relatorio.txt",
            ],
        )

    def test_snapshot_csv_keeps_v1_columns_first(self):
        hop = HopStats(1, "10.0.0.1", "router")
        hop.register_success(1.5)
        write_snapshot_csv(self.paths.snapshot, [hop])
        lines = self.paths.snapshot.read_text(encoding="utf-8").splitlines()
        self.assertTrue(lines[0].startswith("Hop;Host_IP;Host_Name;Sent_pkt;Recv_pkt;Loss_Pct;Best_ms;Worst_ms;Avrg_ms;Last_ms"))
        self.assertEqual(lines[1], "1;10.0.0.1;router;1;1;0.00;1.50;1.50;1.50;1.50;0.00;-")

    def test_outages_and_summary(self):
        tracker = OutageTracker(min_cycles=1)
        tracker.update(False, T0, (1, "10.0.0.1"), True)
        tracker.update(True, T0 + timedelta(seconds=4))
        write_outages_csv(self.paths.outages, tracker)
        rows = self.paths.outages.read_text(encoding="utf-8").splitlines()
        self.assertEqual(rows[1], "1;2026-01-01 12:00:00;2026-01-01 12:00:04;4.00;1;1;10.0.0.1;external")
        write_summary(self.paths.summary, tracker, T0, T0 + timedelta(seconds=100), {"availability_pct": "96.000"})
        summary = parse_summary(self.paths.summary.read_text(encoding="utf-8"))
        self.assertEqual(summary["outage_count"], "1")
        self.assertEqual(summary["total_downtime_sec"], "4.00")
        self.assertEqual(summary["availability_pct"], "96.000")

    def test_appender_keeps_rows_when_flush_fails(self):
        appender = CsvAppender(self.paths.latency_log, LATENCY_HEADERS)
        appender.reset()
        appender.append([["t1", 1, "ip", "-", "1.00", "1.00"]])
        blocked = self.dir / "blocked"
        blocked.mkdir()
        appender.path = blocked  # diretório no lugar do arquivo simula arquivo bloqueado
        with self.assertRaises(OSError):
            appender.append([["t2", 1, "ip", "-", "2.00", "2.00"]])
        appender.path = self.paths.latency_log
        appender.flush()
        lines = self.paths.latency_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], ";".join(LATENCY_HEADERS))
        self.assertEqual([line.split(";")[0] for line in lines[1:]], ["t1", "t2"])

    def test_archive_moves_previous_session(self):
        tracker = OutageTracker()
        write_summary(self.paths.summary, tracker, T0, T0)
        self.paths.snapshot.write_text("x", encoding="utf-8")
        archive_root = self.dir / "sessoes"
        target = archive_previous_session(self.paths, archive_root)
        self.assertEqual(target.name, "20260101-120000")
        self.assertFalse(self.paths.snapshot.exists())
        self.assertTrue((target / "monitoramento_rota.csv").exists())
        self.paths.snapshot.write_text("y", encoding="utf-8")
        second = archive_previous_session(self.paths, archive_root)
        self.assertNotEqual(second, target)
        self.assertEqual(len(list_archives(archive_root)), 2)
        self.assertIsNone(archive_previous_session(self.paths, archive_root))
        self.assertEqual(prune_archives(archive_root, 1), [target.name])

    def test_resolve_archive_blocks_traversal(self):
        root = self.dir / "sessoes"
        (root / "abc").mkdir(parents=True)
        self.assertIsNotNone(resolve_archive(root, "abc"))
        for name in ("..", "../x", "a/b", "", "abc\\..\\.."):
            with self.subTest(name=name):
                self.assertIsNone(resolve_archive(root, name))

    def test_report_and_zip(self):
        hop = HopStats(1, "8.8.8.8", "dns.google", is_destination=True)
        hop.register_success(10.0)
        tracker = OutageTracker()
        report = build_report(
            target="8.8.8.8",
            target_ip="8.8.8.8",
            started_at=T0,
            now=T0 + timedelta(seconds=60),
            stats=[hop],
            tracker=tracker,
            diagnosis={"message": "ok"},
            events=[{"ts": "2026-01-01 12:00:00", "event": "session_start", "detail": "x"}],
            environment={"platform": "test", "python": "3"},
            settings={"outage_min_cycles": 3, "interval_sec": 1, "timeout_ms": 1000},
        )
        self.assertIn("Disponibilidade:      100.000%", report)
        self.assertIn("H1 (destino)", report)
        self.paths.snapshot.write_text("abc", encoding="utf-8")
        data = build_zip([self.paths.snapshot, self.paths.outages], {"relatorio.txt": report})
        with zipfile.ZipFile(BytesIO(data)) as archive:
            self.assertEqual(sorted(archive.namelist()), ["monitoramento_rota.csv", "relatorio.txt"])


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_defaults_without_file(self):
        settings, warnings = load_settings(self.path)
        self.assertEqual(settings, Settings())
        self.assertEqual(warnings, [])

    def test_file_values_and_cli_overrides(self):
        data = {"target": "1.1.1.1", "interval_sec": 2, "csv_path": "out/x.csv"}
        self.path.write_text(json.dumps(data), encoding="utf-8")
        settings, warnings = load_settings(self.path, {"interval_sec": 0.5, "target": None})
        self.assertEqual(warnings, [])
        self.assertEqual(settings.target, "1.1.1.1")
        self.assertEqual(settings.interval_sec, 0.5)
        self.assertEqual(settings.csv_path, (Path(self.tmp.name) / "out" / "x.csv").resolve())
        self.assertEqual(settings.archive_dir, settings.csv_path.parent / "sessoes")

    def test_invalid_file_never_blocks_startup(self):
        self.path.write_text("{ invalid", encoding="utf-8")
        settings, warnings = load_settings(self.path)
        self.assertEqual(settings, Settings())
        self.assertEqual(len(warnings), 1)
        self.path.write_text(json.dumps({"interval_sec": 0, "max_hops": "abc", "unknown": 1}), encoding="utf-8")
        settings, warnings = load_settings(self.path)
        self.assertEqual(settings.interval_sec, Settings().interval_sec)
        self.assertEqual(len(warnings), 3)

    def test_apply_changes_validates(self):
        settings = Settings()
        updated = apply_changes(settings, {"target": "example.com", "timeout_ms": "500"})
        self.assertEqual((updated.target, updated.timeout_ms), ("example.com", 500))
        for changes in ({"target": "x y"}, {"timeout_ms": 5}, {"port": 1}, {"max_hops": 2.5}, {"interval_sec": True}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    apply_changes(settings, changes)

    def test_example_file_matches_defaults(self):
        example = Path(__file__).resolve().parent.parent / "config.example.json"
        settings, warnings = load_settings(example)
        self.assertEqual(warnings, [])
        self.assertEqual(settings, Settings())

    def test_save_preserves_other_keys(self):
        self.path.write_text(json.dumps({"host": "0.0.0.0", "target": "8.8.8.8"}), encoding="utf-8")
        save_user_config(self.path, apply_changes(Settings(), {"target": "1.1.1.1"}))
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["host"], "0.0.0.0")
        self.assertEqual(data["target"], "1.1.1.1")
        settings, _ = load_settings(self.path)
        self.assertEqual(settings.host, "0.0.0.0")


if __name__ == "__main__":
    unittest.main()
