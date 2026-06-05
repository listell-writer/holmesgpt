#!/usr/bin/env python3
"""Multi-stage upgrade of 259. Generates a historical log timeline where:

- The INFO lines record "Payment processed" events tagged with user_id.
- The ERROR lines (database pool exhausted) also include user_id.

The eval asks two questions in one prompt:
1. Identify the root cause.
2. Count how many DISTINCT users were affected during the incident window.

Step 2 requires a second log query (or careful jq) and would be expensive
without a subagent that retains the fetched window.

Usage: python generate_logs.py <loki_url>
"""

import json
import random
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta

random.seed(273)

NAMESPACE = "app-273"
POD_NAME = "payment-api-273-9b3c4d-tuv45"
SERVICE = "payment-api"
BATCH_SIZE = 200


def push(loki_url, streams_by_level):
    streams = []
    for level, values in streams_by_level.items():
        if not values:
            continue
        streams.append({
            "stream": {
                "job": "payment-api",
                "namespace": NAMESPACE,
                "pod_name": POD_NAME,
                "service": SERVICE,
                "level": level,
            },
            "values": values,
        })
    if not streams:
        return
    req = urllib.request.Request(
        loki_url + "/loki/api/v1/push",
        data=json.dumps({"streams": streams}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        raise SystemExit(f"Loki push failed {e.code}: {body}")


def ts(dt):
    return str(int(dt.timestamp() * 1e9))


def generate(loki_url):
    problem_start = datetime(2025, 8, 2, 13, 45, 0)
    problem_end = datetime(2025, 8, 2, 14, 45, 0)
    scenario_start = datetime(2025, 8, 1, 12, 0, 0)
    scenario_end = datetime(2025, 8, 4, 14, 0, 0)

    # 17 distinct users will hit the pool-exhausted error.
    affected_users = [f"USR_{1000 + i:04d}" for i in range(17)]
    # Larger random user pool for normal traffic.
    all_users = affected_users + [f"USR_{i:04d}" for i in range(2000, 2200)]

    streams_by_level: dict[str, list[list[str]]] = {}
    total = 0

    def add(t: datetime, level: str, message: str, **extra):
        streams_by_level.setdefault(level, []).append([
            ts(t),
            json.dumps({"level": level, "message": message, "service": SERVICE, "pod": POD_NAME, **extra}),
        ])

    current = scenario_start
    while current < scenario_end:
        # Normal background payment success traffic
        if random.random() < 0.05:
            add(
                current,
                "INFO",
                "Payment processed successfully",
                payment_id=f"PAY-{random.randint(1000, 9999)}",
                user_id=random.choice(all_users),
            )
            total += 1

        # Pool exhaustion during the problem window
        if problem_start <= current <= problem_end:
            if random.random() < 0.4:
                add(
                    current,
                    "ERROR",
                    "Failed to acquire database connection - pool exhausted",
                    wait_time_ms=random.randint(1000, 5000),
                    queue_length=random.randint(5, 15),
                    user_id=random.choice(affected_users),
                )
                total += 1

        current += timedelta(minutes=random.randint(1, 5))

        pending = sum(len(v) for v in streams_by_level.values())
        if pending >= BATCH_SIZE:
            push(loki_url, streams_by_level)
            streams_by_level = {}

    push(loki_url, streams_by_level)
    print(f"Pushed {total} log entries; expected 17 distinct affected users in incident window")


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:3100"
    generate(url.rstrip("/"))
