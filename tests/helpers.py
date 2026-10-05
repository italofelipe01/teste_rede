"""Utilitários de teste: sonda simulada e espera por condições."""

from __future__ import annotations

import asyncio
import time

from netmon.probes import RouteDiscovery

ROUTE = ["192.168.0.1", "100.64.0.1", "8.8.8.8"]


class FakeProber:
    """Simula a rede: `down_hops` define quais IPs não respondem no momento."""

    instances: list[FakeProber] = []

    def __init__(self, settings) -> None:
        self.timeout_ms = settings.timeout_ms
        self.down_hops: set[str] = set()
        self.route = list(ROUTE)
        FakeProber.instances.append(self)

    async def prepare(self) -> None:
        return None

    async def ping(self, ip: str) -> float | None:
        await asyncio.sleep(0)
        if ip in self.down_hops:
            return None
        return 1.0 + self.route.index(ip) if ip in self.route else 5.0

    async def discover_route(self, dest_ip: str, max_hops: int) -> RouteDiscovery:
        await asyncio.sleep(0)
        hops = [(ttl, ip) for ttl, ip in enumerate(self.route, start=1)]
        if hops and hops[-1][1] != dest_ip:
            hops[-1] = (hops[-1][0], dest_ip)
        return RouteDiscovery(hops=hops, source="fake", reached=True)


def wait_until(predicate, timeout: float = 10.0, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condição não atingida no tempo limite")
