#!/usr/bin/env python3
"""One GitHub Actions run of the Texas DMV checker.

GitHub cron cannot start a workflow faster than every 5 minutes, and it
sometimes starts late. This loop is the faster cadence inside a single run:

- About 4.5 minutes long, so a run that starts on time finishes shortly
  before the next */5 mark. The Pages deploy after the loop still holds
  the workflow concurrency lock, so the next run may wait for that.
- Tesla-only check every 10 seconds (best effort: the next check starts
  after the previous one finishes, so a slow fetch lengthens the gap).
- One full all-operators check, and never more often than every 5 minutes.
- On errors, wait 30s then 60s. A third consecutive failure stops the loop.
- Exit 10 (new Tesla vehicle or company) stops the loop immediately so the
  workflow can commit, open the issue, and deploy without waiting.

The public site is published at the end of a quiet run, or as soon as this
loop stops for a Tesla change. It is not published every minute: Pages
deploys are serialized, often take longer than a minute, and repeating them
through the loop would stack deploys and can get rate-limited.
"""
import argparse, json, os, subprocess, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DURATION = 270
DEFAULT_INTERVAL = 10
DEFAULT_FULL_EVERY = 300
DEFAULT_MAX_FAILURES = 3
DEFAULT_BACKOFF = 30
DEFAULT_BACKOFF_CAP = 120
DEFAULT_FULL_MIN_REMAINING = 45


def eprint(msg):
    print(msg, file=sys.stderr, flush=True)


def backoff_seconds(failures, base, cap):
    return min(cap, base * (2 ** max(0, failures - 1)))


def choose_mode(iterations, last_full_at, failures, now, deadline, full_every, full_min_remaining):
    """Tesla first and after errors. A full check at most every `full_every` seconds."""
    if iterations == 0 or failures:
        return "tesla"
    if (deadline - now) < full_min_remaining:
        return "tesla"
    if last_full_at is None or (now - last_full_at) >= full_every:
        return "full"
    return "tesla"


def _read_bytes(path):
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        return None


def _write_bytes(path, data):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def write_json(path, obj):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(obj, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def run_watch(cfg, run_check, monotonic, sleep):
    start = monotonic()
    deadline = start + cfg["duration"]
    failures = 0
    iterations = 0
    full_checks = 0
    tesla_checks = 0
    last_full_at = None
    saw_20 = False
    saw_success = False
    last_code = None
    good_status = None
    report_20 = None
    report_ok = None
    report_err = None
    stopped = "duration"

    while True:
        now = monotonic()
        if now >= deadline:
            stopped = "duration"
            break
        mode = choose_mode(
            iterations, last_full_at, failures, now, deadline,
            cfg["full_every"], cfg["full_min_remaining"],
        )
        began = monotonic()
        code, report = run_check(mode)
        iterations += 1
        if mode == "full":
            full_checks += 1
            last_full_at = began
        else:
            tesla_checks += 1
        if not isinstance(report, dict):
            report = {"status": "error", "error": "checker report was not a JSON object"}
            code = 1
        if code not in (0, 1, 10, 20):
            report = {"status": "error", "error": report.get("error") or f"unexpected checker exit {code}"}
            code = 1
        if report.get("status") == "error" and code != 1:
            code = 1
        last_code = code
        eprint(f"watch {mode} check exit {code} at {monotonic() - start:.1f}s")

        if code in (0, 10, 20) and cfg.get("status"):
            saved = _read_bytes(cfg["status"])
            if saved:
                good_status = saved
        if code == 10:
            write_json(cfg["report"], report)
            stopped = "tesla_change"
            break
        if code == 20:
            saw_20 = True
            saw_success = True
            failures = 0
            report_20 = report
        elif code == 0:
            saw_success = True
            failures = 0
            report_ok = report
        else:
            failures += 1
            report_err = report
            if failures >= cfg["max_failures"]:
                stopped = "repeated_failures"
                write_json(cfg["report"], report)
                break
            delay = backoff_seconds(failures, cfg["backoff"], cfg["backoff_cap"])
            eprint(f"watch backing off {delay:.0f}s after {failures} consecutive failure(s)")
            sleep(delay)
            continue

        elapsed = monotonic() - began
        pause = cfg["interval"] - elapsed
        remaining = deadline - monotonic()
        if pause > 0 and remaining > 0:
            sleep(min(pause, remaining))

    if stopped == "tesla_change":
        final = 10
        chosen = None  # already written
    elif saw_20:
        final = 20
        chosen = report_20
    elif stopped == "repeated_failures" or not saw_success:
        final = 1
        chosen = report_err or {"status": "error", "error": "checker failed"}
    else:
        final = 0
        chosen = report_ok or {"status": "ok", "result": "no new entries"}
    # One blip at the end of the window should not replace a good status file.
    # Three failures in a row leave the error in place.
    if final != 1 and last_code == 1 and good_status and cfg.get("status"):
        _write_bytes(cfg["status"], good_status)
    if chosen is not None:
        write_json(cfg["report"], chosen)
    if iterations == 0 and not os.path.isfile(cfg["report"]):
        write_json(cfg["report"], {"status": "error", "error": "watch loop made no checks"})
        final = 1

    elapsed_total = monotonic() - start
    result = {
        "exit_code": final,
        "stopped": stopped,
        "iterations": iterations,
        "full_checks": full_checks,
        "tesla_checks": tesla_checks,
        "consecutive_failures": failures,
        "elapsed_seconds": round(elapsed_total, 3),
        "duration_seconds": cfg["duration"],
        "interval_seconds": cfg["interval"],
        "full_every_seconds": cfg["full_every"],
        "deploy": "now" if stopped == "tesla_change" else "end",
        "minute_deploys": False,
    }
    write_json(cfg["result"], result)
    print(json.dumps(result, indent=2))
    return result


def subprocess_check(mode, snapshot, status, check_script):
    cmd = [sys.executable, "-u", check_script, "--snapshot", snapshot, "--status", status]
    if mode == "tesla":
        cmd.append("--tesla-only")
    eprint("watch run: " + " ".join(cmd))
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr, end="" if proc.stderr.endswith("\n") else "\n", flush=True)
    code = proc.returncode
    try:
        data = json.loads(proc.stdout)
        if not isinstance(data, dict):
            raise ValueError("report is not an object")
    except (ValueError, json.JSONDecodeError) as exc:
        data = {
            "status": "error",
            "error": f"unreadable checker output (exit {code}): {exc}",
            "output": (proc.stdout or "")[:500],
        }
        code = 1
    if code not in (0, 1, 10, 20):
        data = {"status": "error", "error": data.get("error") or f"unexpected checker exit {code}"}
        code = 1
    return code, data


def main(argv=None):
    parser = argparse.ArgumentParser(description="Loop DMV checks for one workflow run")
    parser.add_argument("--duration", type=float, default=DEFAULT_DURATION)
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    parser.add_argument("--full-every", type=float, default=DEFAULT_FULL_EVERY)
    parser.add_argument("--max-failures", type=int, default=DEFAULT_MAX_FAILURES)
    parser.add_argument("--backoff", type=float, default=DEFAULT_BACKOFF)
    parser.add_argument("--backoff-cap", type=float, default=DEFAULT_BACKOFF_CAP)
    parser.add_argument("--full-min-remaining", type=float, default=DEFAULT_FULL_MIN_REMAINING)
    parser.add_argument("--snapshot", default=os.path.join(ROOT, "data", "snapshot.json"))
    parser.add_argument("--status", default=os.path.join(ROOT, "data", "status.json"))
    parser.add_argument("--report", default="/tmp/report.json")
    parser.add_argument("--result", default="/tmp/watch.json")
    parser.add_argument("--check-script", default=os.path.join(ROOT, "check.py"))
    args = parser.parse_args(argv)
    if args.duration < 0 or args.interval <= 0 or args.full_every <= 0 or args.max_failures < 1:
        eprint("duration, interval, full-every, and max-failures are out of range")
        return 2
    cfg = {
        "duration": args.duration,
        "interval": args.interval,
        "full_every": args.full_every,
        "max_failures": args.max_failures,
        "backoff": args.backoff,
        "backoff_cap": args.backoff_cap,
        "full_min_remaining": args.full_min_remaining,
        "report": args.report,
        "result": args.result,
        "status": args.status,
    }

    def run_check(mode):
        return subprocess_check(mode, args.snapshot, args.status, args.check_script)

    run_watch(cfg, run_check, time.monotonic, time.sleep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
