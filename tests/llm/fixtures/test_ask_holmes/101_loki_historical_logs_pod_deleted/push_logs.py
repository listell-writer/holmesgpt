#!/usr/bin/env python3
"""Push historical logs directly to Loki for test 101.

Replicates the same log data that app.py generated (same random seed),
but pushes directly to Loki's HTTP API instead of writing to files
for Promtail collection. Uses only stdlib — no external dependencies.
"""
import json
import random
import sys
import urllib.request
import urllib.error
from datetime import datetime, timedelta

random.seed(100)

LOKI_URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:3101"
POD_NAME = "payment-api-101-7f8d9b4c5a-x2k4m"
NAMESPACE = "app-101"


def datetime_to_ns(dt):
    """Convert datetime to nanosecond timestamp string for Loki."""
    epoch = datetime(1970, 1, 1)
    return str(int((dt - epoch).total_seconds() * 1_000_000_000))


def generate_logs():
    """Generate historical logs — same logic and seed as original app.py."""
    logs = []

    problem_start = datetime(2025, 8, 2, 13, 45, 0)
    problem_end = datetime(2025, 8, 2, 14, 45, 0)
    current_time = datetime(2025, 8, 1, 12, 0, 0)
    scenario_end = datetime(2025, 8, 4, 14, 0, 0)

    while current_time < scenario_end:
        if random.random() < 0.05:
            log_entry = {
                "timestamp": current_time.isoformat() + "Z",
                "level": "INFO",
                "message": "Payment processed successfully",
                "service": "payment-api",
                "pod": POD_NAME,
                "payment_id": f"PAY-{random.randint(1000, 9999)}",
            }
            logs.append(("INFO", current_time, json.dumps(log_entry)))

        if problem_start <= current_time <= problem_end:
            if random.random() < 0.4:
                log_entry = {
                    "timestamp": current_time.isoformat() + "Z",
                    "level": "ERROR",
                    "message": "Failed to acquire database connection - pool exhausted",
                    "service": "payment-api",
                    "pod": POD_NAME,
                    "wait_time_ms": random.randint(1000, 5000),
                    "queue_length": random.randint(5, 15),
                }
                logs.append(("ERROR", current_time, json.dumps(log_entry)))

        current_time += timedelta(minutes=random.randint(1, 5))

    return logs


def push_to_loki(logs, batch_size=100):
    """Push logs to Loki in batches."""
    by_level = {}
    for level, ts, line in logs:
        by_level.setdefault(level, []).append((ts, line))

    total = 0
    for level, entries in by_level.items():
        for i in range(0, len(entries), batch_size):
            batch = entries[i : i + batch_size]
            values = [[datetime_to_ns(ts), line] for ts, line in batch]

            payload = {
                "streams": [
                    {
                        "stream": {
                            "job": "payment-api",
                            "namespace": NAMESPACE,
                            "pod": POD_NAME,
                            "level": level,
                            "service": "payment-api",
                        },
                        "values": values,
                    }
                ]
            }

            data = json.dumps(payload).encode()
            req = urllib.request.Request(
                f"{LOKI_URL}/loki/api/v1/push",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    if resp.status not in (200, 204):
                        print(f"ERROR: Push returned {resp.status}", file=sys.stderr)
                        sys.exit(1)
            except urllib.error.HTTPError as e:
                print(
                    f"ERROR: Push failed: {e.code} {e.read().decode()}", file=sys.stderr
                )
                sys.exit(1)

            total += len(batch)
            print(f"  Pushed {len(batch)} {level} logs (batch {i // batch_size + 1})")

    print(f"Total logs pushed: {total}")


if __name__ == "__main__":
    logs = generate_logs()
    print(f"Generated {len(logs)} log entries")
    push_to_loki(logs)
