"""Teste de integração com o `ping` real do sistema (Windows, Linux e macOS)."""

import asyncio
import shutil
import unittest

from netmon.probes import Prober


@unittest.skipIf(shutil.which("ping") is None, "comando ping indisponível neste ambiente")
class SystemPingTests(unittest.TestCase):
    def test_localhost_ping_and_route(self):
        async def scenario():
            prober = Prober(timeout_ms=1000)
            await prober.prepare()
            rtt = await prober.ping("127.0.0.1")
            route = await prober.discover_route("127.0.0.1", 3)
            return rtt, route

        rtt, route = asyncio.run(scenario())
        self.assertIsNotNone(rtt, "o ping para 127.0.0.1 deveria responder")
        self.assertGreaterEqual(rtt, 0.0)
        self.assertEqual(route.hops, [(1, "127.0.0.1")])
        self.assertTrue(route.reached)


if __name__ == "__main__":
    unittest.main()
