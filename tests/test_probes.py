import unittest

from netmon.probes import (
    build_ping_command,
    ordered_route,
    parse_ping_rtt,
    parse_probe_responder,
    parse_trace_output,
    validate_target,
)

WIN_EN_OK = """
Pinging 8.8.8.8 with 32 bytes of data:
Reply from 8.8.8.8: bytes=32 time=12ms TTL=117

Ping statistics for 8.8.8.8:
    Packets: Sent = 1, Received = 1, Lost = 0 (0% loss),
Approximate round trip times in milli-seconds:
    Minimum = 12ms, Maximum = 12ms, Average = 12ms
"""

WIN_PT_OK = """
Disparando 8.8.8.8 com 32 bytes de dados:
Resposta de 8.8.8.8: bytes=32 tempo=14ms TTL=117

Estatísticas do Ping para 8.8.8.8:
    Pacotes: Enviados = 1, Recebidos = 1, Perdidos = 0 (0% de
             perda),
Aproximar um número redondo de vezes em milissegundos:
    Mínimo = 14ms, Máximo = 14ms, Média = 14ms
"""

WIN_PT_SUB_MS = """
Disparando 192.168.0.1 com 32 bytes de dados:
Resposta de 192.168.0.1: bytes=32 tempo<1ms TTL=64
"""

WIN_PT_TIMEOUT = """
Disparando 8.8.8.8 com 32 bytes de dados:
Esgotado o tempo limite do pedido.

Estatísticas do Ping para 8.8.8.8:
    Pacotes: Enviados = 1, Recebidos = 0, Perdidos = 1 (100% de
             perda),
"""

WIN_EN_UNREACHABLE = """
Pinging 10.0.0.9 with 32 bytes of data:
Reply from 192.168.0.10: Destination host unreachable.

Ping statistics for 10.0.0.9:
    Packets: Sent = 1, Received = 1, Lost = 0 (0% loss),
"""

WIN_EN_TTL = """
Pinging 8.8.8.8 with 32 bytes of data:
Reply from 192.168.0.1: TTL expired in transit.

Ping statistics for 8.8.8.8:
    Packets: Sent = 1, Received = 1, Lost = 0 (0% loss),
"""

WIN_PT_TTL = """
Disparando 8.8.8.8 com 32 bytes de dados:
Resposta de 100.64.0.1: TTL expirou durante passagem.

Estatísticas do Ping para 8.8.8.8:
    Pacotes: Enviados = 1, Recebidos = 1, Perdidos = 0 (0% de
             perda),
"""

LINUX_OK = """PING 127.0.0.1 (127.0.0.1) 56(84) bytes of data.
64 bytes from 127.0.0.1: icmp_seq=1 ttl=64 time=0.623 ms

--- 127.0.0.1 ping statistics ---
1 packets transmitted, 1 received, 0% packet loss, time 0ms
rtt min/avg/max/mdev = 0.623/0.623/0.623/0.000 ms
"""

LINUX_TIMEOUT = """PING 8.8.8.8 (8.8.8.8) 56(84) bytes of data.

--- 8.8.8.8 ping statistics ---
1 packets transmitted, 0 received, 100% packet loss, time 0ms
"""

LINUX_TTL = """PING 8.8.8.8 (8.8.8.8) 56(84) bytes of data.
From 192.0.2.1 icmp_seq=1 Time to live exceeded

--- 8.8.8.8 ping statistics ---
1 packets transmitted, 0 received, +1 errors, 100% packet loss, time 0ms
"""

LINUX_COMMA_LOCALE = """PING 8.8.8.8 (8.8.8.8) 56(84) bytes of data.
64 bytes from 8.8.8.8: icmp_seq=1 ttl=117 time=10,5 ms
"""

MAC_OK = """PING 8.8.8.8 (8.8.8.8): 56 data bytes
64 bytes from 8.8.8.8: icmp_seq=0 ttl=117 time=13.214 ms

--- 8.8.8.8 ping statistics ---
1 packets transmitted, 1 packets received, 0.0% packet loss
round-trip min/avg/max/stddev = 13.214/13.214/13.214/0.000 ms
"""

MAC_TTL = """PING 8.8.8.8 (8.8.8.8): 56 data bytes
92 bytes from 192.168.1.1: Time to live exceeded
Vr HL TOS  Len   ID Flg  off TTL Pro  cks      Src      Dst
 4  5  00 5400 b3a1   0 0000  01  01 4a4c 192.168.1.20  8.8.8.8

--- 8.8.8.8 ping statistics ---
1 packets transmitted, 0 packets received, 100.0% packet loss
"""

MAC_TIMEOUT = """PING 8.8.8.8 (8.8.8.8): 56 data bytes

--- 8.8.8.8 ping statistics ---
1 packets transmitted, 0 packets received, 100.0% packet loss
"""

TRACERT_PT = """
Rastreando a rota para dns.google [8.8.8.8]
com no máximo 30 saltos:

  1    <1 ms    <1 ms    <1 ms  192.168.0.1
  2     8 ms     7 ms     9 ms  100.64.0.1
  3     *        *        *     Esgotado o tempo limite do pedido.
  4    12 ms    11 ms    12 ms  8.8.8.8

Rastreamento concluído.
"""

TRACEROUTE_LINUX = """traceroute to 8.8.8.8 (8.8.8.8), 5 hops max, 60 byte packets
 1  192.0.2.1  0.240 ms
 2  21.4.2.7  0.201 ms
 3  *
 4  8.8.8.8  10.1 ms
"""

TRACEPATH = """ 1?: [LOCALHOST]                      pmtu 1400
 1:  192.0.2.1                                             0.312ms
 1:  192.0.2.1                                             0.129ms
 2:  21.4.2.7                                              0.131ms
 3:  no reply
     Too many hops: pmtu 1400
     Resume: pmtu 1400
"""


class PingParsingTests(unittest.TestCase):
    def test_success_outputs_all_systems_and_languages(self):
        self.assertEqual(parse_ping_rtt(WIN_EN_OK), 12.0)
        self.assertEqual(parse_ping_rtt(WIN_PT_OK), 14.0)
        self.assertEqual(parse_ping_rtt(LINUX_OK), 0.623)
        self.assertEqual(parse_ping_rtt(MAC_OK), 13.214)
        self.assertEqual(parse_ping_rtt(LINUX_COMMA_LOCALE), 10.5)

    def test_sub_millisecond_windows_is_half_ms(self):
        self.assertEqual(parse_ping_rtt(WIN_PT_SUB_MS), 0.5)

    def test_failures_return_none(self):
        for output in (WIN_PT_TIMEOUT, WIN_EN_UNREACHABLE, WIN_EN_TTL, WIN_PT_TTL, LINUX_TIMEOUT, LINUX_TTL,
                       MAC_TTL, MAC_TIMEOUT, ""):
            with self.subTest(output=output[:40]):
                self.assertIsNone(parse_ping_rtt(output))


class ProbeResponderTests(unittest.TestCase):
    def test_ttl_exceeded_returns_router_ip(self):
        self.assertEqual(parse_probe_responder(WIN_EN_TTL), "192.168.0.1")
        self.assertEqual(parse_probe_responder(WIN_PT_TTL), "100.64.0.1")
        self.assertEqual(parse_probe_responder(LINUX_TTL), "192.0.2.1")
        self.assertEqual(parse_probe_responder(MAC_TTL), "192.168.1.1")

    def test_destination_reply_returns_destination(self):
        self.assertEqual(parse_probe_responder(WIN_PT_OK), "8.8.8.8")
        self.assertEqual(parse_probe_responder(LINUX_OK), "127.0.0.1")
        self.assertEqual(parse_probe_responder(MAC_OK), "8.8.8.8")

    def test_timeouts_have_no_responder(self):
        for output in (WIN_PT_TIMEOUT, LINUX_TIMEOUT, MAC_TIMEOUT, ""):
            with self.subTest(output=output[:40]):
                self.assertIsNone(parse_probe_responder(output))


class TraceParsingTests(unittest.TestCase):
    def test_tracert_windows(self):
        self.assertEqual(parse_trace_output(TRACERT_PT), [(1, "192.168.0.1"), (2, "100.64.0.1"), (4, "8.8.8.8")])

    def test_traceroute_linux(self):
        self.assertEqual(parse_trace_output(TRACEROUTE_LINUX), [(1, "192.0.2.1"), (2, "21.4.2.7"), (4, "8.8.8.8")])

    def test_tracepath(self):
        self.assertEqual(parse_trace_output(TRACEPATH), [(1, "192.0.2.1"), (2, "21.4.2.7")])

    def test_ordered_route_dedupes_and_stops_at_destination(self):
        responders = {3: "8.8.8.8", 1: "192.168.0.1", 2: "192.168.0.1", 4: "8.8.8.8", 5: "8.8.8.8"}
        self.assertEqual(ordered_route(responders, "8.8.8.8"), [(1, "192.168.0.1"), (3, "8.8.8.8")])


class CommandTests(unittest.TestCase):
    def test_windows_command(self):
        self.assertEqual(
            build_ping_command("8.8.8.8", 1000, ttl=3, style="windows"),
            ["ping", "-n", "1", "-w", "1000", "-i", "3", "8.8.8.8"],
        )

    def test_bsd_command_uses_milliseconds(self):
        self.assertEqual(
            build_ping_command("8.8.8.8", 800, ttl=2, style="bsd"),
            ["ping", "-n", "-c", "1", "-W", "800", "-m", "2", "8.8.8.8"],
        )

    def test_linux_command_seconds(self):
        base = ["ping", "-n", "-c", "1", "-W"]
        self.assertEqual(build_ping_command("8.8.8.8", 400, style="linux"), base + ["0.4", "8.8.8.8"])
        self.assertEqual(build_ping_command("8.8.8.8", 1000, style="linux"), base + ["1", "8.8.8.8"])
        self.assertEqual(
            build_ping_command("8.8.8.8", 400, ttl=5, style="linux", fractional_wait=False),
            ["ping", "-n", "-c", "1", "-W", "1", "-t", "5", "8.8.8.8"],
        )


class TargetValidationTests(unittest.TestCase):
    def test_valid_targets(self):
        self.assertEqual(validate_target(" 8.8.8.8 "), "8.8.8.8")
        self.assertEqual(validate_target("google.com"), "google.com")
        self.assertEqual(validate_target("my-router.local"), "my-router.local")

    def test_invalid_targets(self):
        for value in ("", "-f", "8.8.8", "999.1.1.1", "a b", "::1", "2001:db8::1", "0.0.0.0", "bad_host!", "224.0.0.1"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_target(value)


if __name__ == "__main__":
    unittest.main()
