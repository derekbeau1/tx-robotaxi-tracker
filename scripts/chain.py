#!/usr/bin/env python3
"""Decide whether this workflow run should start the next one.

One run executes at a time (the workflow concurrency group). At most one
more run may wait. If several are queued, the newer ones are cancelled.
The chain stops after three consecutive checker failures so a bad feed
does not dispatch forever. Cron remains the backup restart.
"""
import argparse, json, sys

PENDING = {"queued", "waiting", "requested", "pending", "action_required"}
MAX_FAILURES = 3


def plan(runs, current_id, failures, max_failures=MAX_FAILURES):
    try:
        failure_count = int(failures)
    except (TypeError, ValueError):
        failure_count = 0
    pending = []
    for run in runs or []:
        if not isinstance(run, dict):
            continue
        if str(run.get("databaseId")) == str(current_id):
            continue
        if str(run.get("status") or "").lower() in PENDING:
            pending.append(run)
    pending.sort(key=lambda run: (run.get("createdAt") or "", str(run.get("databaseId"))))
    if failure_count >= max_failures:
        return {
            "dispatch": False,
            "reason": "checker failed 3 runs in a row",
            "cancel_ids": [run.get("databaseId") for run in pending],
            "keep_id": None,
        }
    cancel = [run.get("databaseId") for run in pending[1:]]
    if pending:
        return {
            "dispatch": False,
            "reason": "a run is already pending",
            "cancel_ids": cancel,
            "keep_id": pending[0].get("databaseId"),
        }
    return {"dispatch": True, "reason": "start the next run", "cancel_ids": [], "keep_id": None}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Plan the next workflow run")
    parser.add_argument("--runs", required=True, help="JSON list from gh run list")
    parser.add_argument("--current", required=True)
    parser.add_argument("--failures", default="0")
    args = parser.parse_args(argv)
    with open(args.runs, encoding="utf-8") as handle:
        runs = json.load(handle)
    if isinstance(runs, dict):
        runs = runs.get("runs") or runs.get("workflow_runs") or []
    print(json.dumps(plan(runs, args.current, args.failures), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
