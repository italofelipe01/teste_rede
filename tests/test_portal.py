import http.client
import json
import tempfile
import threading
import unittest
import zipfile
from io import BytesIO
from pathlib import Path

import portal_rede
from netmon.config import Settings
from netmon.engine import MonitorEngine
from tests.helpers import FakeProber, wait_until


class PortalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        settings = Settings(interval_sec=0.2, timeout_ms=200, csv_path=cls.dir / "monitoramento_rota.csv", port=0)
        cls.engine = MonitorEngine(settings, prober_factory=FakeProber)
        cls.stopping = threading.Event()
        cls.config_path = cls.dir / "config.json"
        handler = portal_rede.make_handler(cls.engine, cls.config_path, ["http://127.0.0.1"], cls.stopping)
        cls.server = portal_rede.bind_server("127.0.0.1", 0, handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        cls.thread.start()
        cls.engine.start()
        wait_until(lambda: cls.engine.snapshot()["cycle"] >= 2)

    @classmethod
    def tearDownClass(cls):
        cls.stopping.set()
        cls.engine.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            payload = json.dumps(body).encode() if isinstance(body, dict) else body
            connection.request(method, path, body=payload, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def get_json(self, path):
        status, _, data = self.request("GET", path)
        self.assertEqual(status, 200, data)
        return json.loads(data)

    def post_json(self, path, body, extra_headers=None):
        headers = {"Content-Type": "application/json", **(extra_headers or {})}
        status, _, data = self.request("POST", path, body, headers)
        return status, json.loads(data)

    def test_static_files_and_security(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b"dashboard.js", body)
        for path in ("/dashboard.js", "/dashboard.css", "/favicon.svg"):
            self.assertEqual(self.request("GET", path)[0], 200, path)
        for path in ("/.git/config", "/portal_rede.py", "/data/../portal_rede.py", "/web/../README.md", "/data/x.csv"):
            self.assertEqual(self.request("GET", path)[0], 404, path)
        self.assertEqual(self.request("GET", "/data/monitoramento_rota.csv")[0], 200)

    def test_snapshot_keeps_v1_contract(self):
        snap = self.get_json("/api/snapshot")
        for key in ("destino", "cycle", "last_update", "error", "hops", "outages", "summary"):
            self.assertIn(key, snap)
        for key in ("Hop", "Host_IP", "Host_Name", "Sent", "Recv", "Loss_Pct", "Best", "Worst", "Avrg", "Last"):
            self.assertIn(key, snap["hops"][0])
        for key in ("monitoring_start", "monitoring_end", "outage_count", "total_downtime_sec"):
            self.assertIn(key, snap["summary"])
        for key in ("status", "diagnosis", "route", "events", "settings", "thresholds"):
            self.assertIn(key, snap)

    def test_history_and_health(self):
        history = self.get_json("/api/history?since=0")
        self.assertTrue(history["points"])
        cursor = f"{history['instance']}:{history['latest_seq']}"
        later = self.get_json(f"/api/history?since={cursor}")
        self.assertFalse(later["reset"])
        health = self.get_json("/api/health")
        self.assertEqual(health["app"], "netmon")
        self.assertTrue(health["control_allowed"])
        self.assertEqual(portal_rede.find_running_instance(self.port)["instance"], self.engine.instance_id)

    def test_gzip_when_accepted(self):
        status, headers, _ = self.request("GET", "/api/snapshot", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Encoding"), "gzip")

    def test_stream_sends_first_event(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request("GET", "/api/stream?since=0")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn("text/event-stream", response.getheader("Content-Type"))
            lines = []
            while not any(line.startswith(b"data: ") for line in lines):
                lines.append(response.fp.readline())
            event_id = next(line for line in lines if line.startswith(b"id: "))
            self.assertIn(self.engine.instance_id.encode(), event_id)
            data = json.loads(next(line for line in lines if line.startswith(b"data: "))[6:])
            self.assertIn("snapshot", data)
            self.assertTrue(data["points"])
        finally:
            connection.close()

    def test_export_zip(self):
        status, headers, body = self.request("GET", "/api/export")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        with zipfile.ZipFile(BytesIO(body)) as archive:
            names = archive.namelist()
        self.assertIn("monitoramento_rota.csv", names)
        self.assertIn("monitoramento_rota_relatorio.txt", names)
        self.assertIn("snapshot.json", names)

    def test_control_endpoints_validation(self):
        status, _, _ = self.request("POST", "/api/config", b'{"interval_sec": 0.3}', {"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        status, body = self.post_json("/api/config", {"target": "1.1.1.1"}, {"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        status, body = self.post_json("/api/config", {"target": "1.1.1.1"}, {"Host": f"rebind.example:{self.port}"})
        self.assertEqual(status, 403)
        status, body = self.post_json("/api/config", {"target": "nope nope"})
        self.assertEqual(status, 400)
        self.assertIn("Destino inválido", body["error"])
        status, body = self.post_json("/api/config", {"port": 9999})
        self.assertEqual(status, 400)
        status, body = self.post_json("/api/config", {"interval_sec": 0.3}, {"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["persisted"])
        self.assertEqual(json.loads(self.config_path.read_text(encoding="utf-8"))["interval_sec"], 0.3)
        self.assertEqual(self.post_json("/api/actions/rediscover", {})[0], 200)
        self.assertEqual(self.post_json("/api/actions/unknown", {})[0], 404)

    def test_remote_clients_cannot_control_by_default(self):
        handler = portal_rede.PortalHandler.__new__(portal_rede.PortalHandler)
        handler.engine = self.engine
        handler.client_address = ("192.168.0.50", 50000)
        self.assertFalse(handler._control_allowed())
        handler.client_address = ("::ffff:127.0.0.1", 50000)
        self.assertTrue(handler._control_allowed())

    def test_sessions_listing(self):
        sessions = self.get_json("/api/sessions")["sessions"]
        self.assertIsInstance(sessions, list)
        self.assertEqual(self.request("GET", "/api/sessions/../../etc/export")[0], 404)


class CursorTests(unittest.TestCase):
    def test_parse_cursor(self):
        self.assertEqual(portal_rede._parse_cursor("abc:12"), (12, "abc"))
        self.assertEqual(portal_rede._parse_cursor("7"), (7, None))
        self.assertEqual(portal_rede._parse_cursor(""), (0, None))
        self.assertEqual(portal_rede._parse_cursor("x:y"), (0, None))


if __name__ == "__main__":
    unittest.main()
