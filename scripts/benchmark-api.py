from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import time
from typing import Any

import httpx


def percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil(len(ordered) * ratio) - 1)
    return round(ordered[index], 2)


async def benchmark(
    base_url: str,
    username: str,
    password: str,
    requests: int,
    concurrency: int,
) -> dict[str, Any]:
    limits = httpx.Limits(
        max_connections=concurrency,
        max_keepalive_connections=concurrency,
    )
    timeout = httpx.Timeout(15.0, connect=3.0)
    async with httpx.AsyncClient(
        base_url=base_url.rstrip("/"),
        limits=limits,
        timeout=timeout,
    ) as client:
        auth = await client.post(
            "/api/auth/login",
            json={"username": username, "password": password},
        )
        auth.raise_for_status()
        token = auth.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        paths = ["/api/health", "/api/runs", "/api/metrics", "/api/tools", "/api/knowledge"]
        semaphore = asyncio.Semaphore(concurrency)
        latencies: list[float] = []
        status_counts: dict[str, int] = {}

        async def request_once(index: int) -> None:
            path = paths[index % len(paths)]
            try:
                async with semaphore:
                    started = time.perf_counter()
                    response = await client.get(path, headers=headers)
                    latency = (time.perf_counter() - started) * 1000
                key = str(response.status_code)
            except Exception as exc:  # captured in the evidence instead of hiding load failures
                key = f"error:{type(exc).__name__}"
                latency = 15_000.0
            latencies.append(latency)
            status_counts[key] = status_counts.get(key, 0) + 1

        started = time.perf_counter()
        await asyncio.gather(*(request_once(index) for index in range(requests)))
        elapsed = time.perf_counter() - started
    success = status_counts.get("200", 0)
    return {
        "profile": "mixed-read-api",
        "base_url": base_url,
        "requests": requests,
        "concurrency": concurrency,
        "duration_seconds": round(elapsed, 3),
        "throughput_rps": round(requests / elapsed, 2),
        "success_rate_percent": round(success / requests * 100, 2),
        "status_counts": status_counts,
        "latency_ms": {
            "min": round(min(latencies), 2),
            "mean": round(statistics.fmean(latencies), 2),
            "p50": percentile(latencies, 0.50),
            "p95": percentile(latencies, 0.95),
            "p99": percentile(latencies, 0.99),
            "max": round(max(latencies), 2),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Bounded Harbor AgentOps HTTP benchmark")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--username", default="admin@harbor.local")
    parser.add_argument("--password", default="harbor-demo-2026")
    parser.add_argument("--requests", type=int, default=400)
    parser.add_argument("--concurrency", type=int, default=32)
    args = parser.parse_args()
    if args.requests < 1 or args.requests > 10_000:
        raise SystemExit("--requests must be between 1 and 10000")
    if args.concurrency < 1 or args.concurrency > 256:
        raise SystemExit("--concurrency must be between 1 and 256")
    result = asyncio.run(
        benchmark(
            args.base_url,
            args.username,
            args.password,
            args.requests,
            args.concurrency,
        )
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["success_rate_percent"] != 100:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
