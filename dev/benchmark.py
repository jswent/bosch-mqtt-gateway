"""Measure every simulator TLS emission through real HA state_changed events.

Run only against the development compose stack; do not run alongside e2e.py.
Credentials are read from ignored generated files and never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import websockets

ROOT = Path(__file__).resolve().parent / "generated"


def distribution(values: list[float]) -> dict[str, float | int]:
    ordered = sorted(values)
    return {
        "samples": len(ordered),
        **{
            name: round(ordered[max(0, math.ceil(len(ordered) * percentile) - 1)], 3)
            for name, percentile in (("p50", 0.50), ("p95", 0.95), ("p99", 0.99))
        },
        "max": round(max(ordered), 3),
        "min": round(min(ordered), 3),
    }


async def benchmark(rounds: int = 10, concurrency: int = 16, max_p99_ms: float = 250) -> dict:
    ha_token = (ROOT / "ha_access_token").read_text().strip()
    debug_token = (ROOT / "debug_token").read_text().strip()
    samples: list[dict] = []
    round_results = []
    pending: dict[str, tuple[str, asyncio.Future]] = {}
    semaphore = asyncio.Semaphore(concurrency)
    async with (
        httpx.AsyncClient(
            base_url="http://127.0.0.1:18123",
            headers={"Authorization": "Bearer " + ha_token},
            timeout=30,
        ) as ha,
        httpx.AsyncClient(
            base_url="http://127.0.0.1:18080",
            headers={"Authorization": "Bearer " + debug_token},
            timeout=30,
        ) as gateway,
        httpx.AsyncClient(
            base_url="http://127.0.0.1:18081",
            timeout=30,
            limits=httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency),
        ) as simulator,
    ):
        status = await gateway.get("/status")
        status.raise_for_status()
        before = status.json()
        if not before["ready"]:
            raise RuntimeError("Development gateway must be ready before benchmarking")
        states_response = await ha.get("/api/states")
        states_response.raise_for_status()
        states = {item["entity_id"]: item["state"] for item in states_response.json()}
        names = [f"binary_sensor.point{number}" for number in range(1, 601)]
        if any(states.get(name) not in ("on", "off") for name in names):
            raise RuntimeError("Benchmark requires all 600 real HA point entities available")
        async with websockets.connect(
            "ws://127.0.0.1:18123/api/websocket",
            max_queue=8192,
        ) as websocket:
            if json.loads(await websocket.recv())["type"] != "auth_required":
                raise RuntimeError("Unexpected HA WebSocket authentication response")
            await websocket.send(json.dumps({"type": "auth", "access_token": ha_token}))
            if json.loads(await websocket.recv())["type"] != "auth_ok":
                raise RuntimeError("HA WebSocket authentication failed")
            await websocket.send(
                json.dumps(
                    {
                        "id": 1,
                        "type": "subscribe_events",
                        "event_type": "state_changed",
                    }
                )
            )
            if not json.loads(await websocket.recv()).get("success"):
                raise RuntimeError("HA state event subscription failed")

            async def receive() -> None:
                async for raw in websocket:
                    event = json.loads(raw).get("event", {})
                    data = event.get("data", {})
                    entity = data.get("entity_id")
                    if entity not in pending:
                        continue
                    expected, future = pending[entity]
                    new = data.get("new_state") or {}
                    old = data.get("old_state") or {}
                    if (
                        not future.done()
                        and new.get("state") == expected
                        and old.get("state") != expected
                    ):
                        # Both timestamps come from the same Docker VM wall clock.
                        timestamp = int(
                            datetime.fromisoformat(new["last_updated"]).timestamp() * 1e9
                        )
                        future.set_result(timestamp)

            reader = asyncio.create_task(receive(), name="ha-benchmark-observer")
            started = time.monotonic()
            try:
                # First round is a standalone 600-point burst; subsequent rounds
                # constitute a sustained sequence, with no intentional quiet time.
                for round_number in range(rounds + 1):
                    pending.clear()
                    expected = {name: "off" if states[name] == "on" else "on" for name in names}
                    for name in names:
                        pending[name] = (expected[name], asyncio.get_running_loop().create_future())
                    emitted: dict[str, int] = {}

                    async def emit(
                        number: int, name: str, targets=expected, emissions=emitted
                    ) -> None:
                        async with semaphore:
                            response = await simulator.post(
                                "/event",
                                json={
                                    "point": number,
                                    "status": 2 if targets[name] == "on" else 3,
                                },
                            )
                            response.raise_for_status()
                            emissions[name] = response.json()["emitted_ns"]

                    round_started = time.monotonic()
                    async with asyncio.timeout(60):
                        await asyncio.gather(*(emit(i, name) for i, name in enumerate(names, 1)))
                        injection_elapsed = time.monotonic() - round_started
                        await asyncio.gather(*(future for _, future in pending.values()))
                    round_elapsed = time.monotonic() - round_started
                    current_samples = []
                    for name in names:
                        ha_timestamp = pending[name][1].result()
                        latency = (ha_timestamp - emitted[name]) / 1e6
                        if latency < 0:
                            raise RuntimeError(
                                "Clock skew invalidates pipeline latency measurements"
                            )
                        sample = {
                            "round": round_number,
                            "entity": name,
                            "state": expected[name],
                            "emitted_ns": emitted[name],
                            "ha_updated_ns": ha_timestamp,
                            "latency_ms": latency,
                        }
                        samples.append(sample)
                        current_samples.append(latency)
                    states.update(expected)
                    round_results.append(
                        {
                            "round": round_number,
                            "kind": "burst" if round_number == 0 else "sustained",
                            "requested": 600,
                            "delivered": 600,
                            "injection_seconds": round(injection_elapsed, 3),
                            "completion_seconds": round(round_elapsed, 3),
                            "latency_ms": distribution(current_samples),
                        }
                    )
                    print(json.dumps(round_results[-1]), flush=True)
                elapsed = time.monotonic() - started
            finally:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
        # HA can process a publication before the gateway receives its PUBACK.
        # Drain those final acknowledgements before comparing completion counters.
        async with asyncio.timeout(15):
            while True:
                status = await gateway.get("/status")
                status.raise_for_status()
                after = status.json()
                completed = (
                    after["metrics"]["events_published"] - before["metrics"]["events_published"]
                )
                if completed >= len(samples) and after["queued_events"] == 0:
                    break
                await asyncio.sleep(0.02)
        counters = (
            "events_received",
            "events_published",
            "queue_overflows",
            "reconnections",
            "failures",
        )
        delta = {key: after["metrics"][key] - before["metrics"][key] for key in counters}
        if (
            not after["ready"]
            or delta["queue_overflows"]
            or delta["reconnections"]
            or delta["failures"]
        ):
            raise RuntimeError("Gateway lost healthy operation during benchmark")
        result = {
            "measured_at": datetime.now(UTC).isoformat(),
            "ha_version": (await ha.get("/api/config")).json().get("version"),
            "points": 600,
            "http_concurrency": concurrency,
            "sustained_rounds": rounds,
            "requested": 600 * (rounds + 1),
            "delivered": len(samples),
            "elapsed_seconds": round(elapsed, 3),
            "delivered_events_per_second": round(len(samples) / elapsed, 2),
            "pipeline_latency_ms": distribution([s["latency_ms"] for s in samples]),
            "burst_latency_ms": round_results[0]["latency_ms"],
            "sustained_latency_ms": distribution([s["latency_ms"] for s in samples if s["round"]]),
            "gateway_counter_deltas": delta,
            "gateway_final_ready": after["ready"],
            "gateway_final_metrics": after["metrics"],
            "p99_regression_threshold_ms": max_p99_ms,
            "p99_regression_passed": distribution([s["latency_ms"] for s in samples])["p99"]
            <= max_p99_ms,
            "rounds": round_results,
        }
        os.umask(0o077)
        (ROOT / "benchmark-results.json").write_text(json.dumps(result, indent=2))
        (ROOT / "benchmark-samples.json").write_text(json.dumps(samples, separators=(",", ":")))
        if not result["p99_regression_passed"]:
            raise RuntimeError(
                "Pipeline p99 exceeded configured regression threshold; see saved results"
            )
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--max-p99-ms", type=float, default=250)
    args = parser.parse_args()
    if not 1 <= args.rounds <= 100 or not 1 <= args.concurrency <= 64 or args.max_p99_ms <= 0:
        parser.error("rounds must be 1-100 and concurrency 1-64")
    print(
        json.dumps(asyncio.run(benchmark(args.rounds, args.concurrency, args.max_p99_ms)), indent=2)
    )
