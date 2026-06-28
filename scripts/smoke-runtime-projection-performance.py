#!/usr/bin/env python3
"""Generate redacted RuntimeProjection performance evidence for R23-16P."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class CountingLedger:
    def __init__(self, base):
        self.base = base
        self.reset_counts()

    def reset_counts(self):
        self.list_events_calls = 0
        self.events_for_requests_calls = 0
        self.events_for_request_calls = 0
        self.latest_event_id_for_request_calls = 0

    def __getattr__(self, name):
        return getattr(self.base, name)

    def list_events(self, **kwargs):
        self.list_events_calls += 1
        return self.base.list_events(**kwargs)

    def events_for_requests(self, request_ids, *, limit=0):
        self.events_for_requests_calls += 1
        return self.base.events_for_requests(request_ids, limit=limit)

    def events_for_request(self, request_id, *, limit=5000):
        self.events_for_request_calls += 1
        return self.base.events_for_request(request_id, limit=limit)

    def latest_event_id_for_request(self, request_id):
        self.latest_event_id_for_request_calls += 1
        return self.base.latest_event_id_for_request(request_id)


def percentile(values, pct):
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((pct / 100.0) * (len(ordered) - 1))))
    return float(ordered[index])


def run(output: Path, request_count: int, iterations: int) -> dict:
    from agent.protocol import RuntimeProjectionService, reset_run_event_ledger_for_tests

    with tempfile.TemporaryDirectory() as root:
        ledger = reset_run_event_ledger_for_tests(Path(root) / "runtime-projection-perf.db")
        session_id = "perf-session"
        for index in range(request_count):
            request_id = f"perf-request-{index:04d}"
            turn_id = f"perf-turn-{index:04d}"
            ledger.append_event(
                request_id=request_id,
                session_id=session_id,
                event_type="run.accepted",
                payload={"turn_id": turn_id},
            )
            ledger.append_event(
                request_id=request_id,
                session_id=session_id,
                event_type="message.user.accepted",
                payload={"content": f"synthetic user body {index}", "turn_id": turn_id},
            )
            ledger.append_event(
                request_id=request_id,
                session_id=session_id,
                event_type="message.assistant.created",
                payload={"turn_id": turn_id},
            )
            ledger.append_event(
                request_id=request_id,
                session_id=session_id,
                event_type="message.assistant.finalized",
                payload={"content": f"synthetic assistant body {index}"},
            )

        counting = CountingLedger(ledger)
        service = RuntimeProjectionService(counting)
        warmup_started = time.perf_counter()
        warmup_projection = service.session_projection(session_id, limit=0, include_events=False)
        warmup_ms = (time.perf_counter() - warmup_started) * 1000.0
        counting.reset_counts()
        session_ms = []
        projected_request_count = len(warmup_projection.get("requests") or [])
        projected_event_count = len(warmup_projection.get("events") or [])
        for _ in range(iterations):
            started = time.perf_counter()
            projection = service.session_projection(session_id, limit=0, include_events=False)
            session_ms.append((time.perf_counter() - started) * 1000.0)
            projected_request_count = len(projection.get("requests") or [])
            projected_event_count = len(projection.get("events") or [])

        first_request_id = "perf-request-0000"
        request_ms = []
        for _ in range(iterations):
            started = time.perf_counter()
            service.request_projection(first_request_id, expected_session_id=session_id, include_events=False)
            request_ms.append((time.perf_counter() - started) * 1000.0)

    metrics = {
        "requestCount": request_count,
        "eventCount": request_count * 4,
        "projectedRequestCount": projected_request_count,
        "projectedEventCount": projected_event_count,
        "includeEvents": False,
        "warmupSessionProjectionMs": round(warmup_ms, 3),
        "sessionProjectionP95Ms": round(percentile(session_ms, 95), 3),
        "sessionProjectionMeanMs": round(statistics.mean(session_ms), 3),
        "requestProjectionP95Ms": round(percentile(request_ms, 95), 3),
        "requestProjectionMeanMs": round(statistics.mean(request_ms), 3),
        "listEventsCalls": counting.list_events_calls,
        "eventsForRequestsCalls": counting.events_for_requests_calls,
        "eventsForRequestCalls": counting.events_for_request_calls,
        "latestEventIdForRequestCalls": counting.latest_event_id_for_request_calls,
    }
    artifact = {
        "version": "0.2.3",
        "slice": "R23-16P-01",
        "scenario": "long-session-projection",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "metrics": metrics,
        "redaction": {
            "containsRawPrompts": False,
            "containsRawMessageBodies": False,
            "containsFullPaths": False,
            "containsSecretShapedValues": False,
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return artifact


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="docs/v0.2.3/artifacts/perf-long-session.json")
    parser.add_argument("--request-count", type=int, default=200)
    parser.add_argument("--iterations", type=int, default=5)
    args = parser.parse_args()
    artifact = run(Path(args.output), max(1, args.request_count), max(1, args.iterations))
    print(json.dumps(artifact["metrics"], ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
