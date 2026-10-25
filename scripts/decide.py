#!/usr/bin/env python3
"""Commit and alert rules for the TxDMV checker workflow.

check.py owns the fetch and the VIN diff. This script decides what the
workflow commits and which GitHub issues to open. It does not talk to
the network.

  python3 scripts/decide.py hash data/snapshot.json
  python3 scripts/decide.py record --exit-code N --report report.json \\
      --companies-changed true|false --open-error-issues N
  python3 scripts/decide.py ack --tesla --error

Exit codes from check.py: 0 no new vehicles, 10 new Tesla entry,
20 non-Tesla additions only, 1 fetch/format error (snapshot not updated).
"""
import argparse, hashlib, json, os, re, sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import check  # noqa: E402

HEARTBEAT_SECONDS = 30 * 60
FAILURES_BEFORE_ISSUE = 3
TESLA_LOOKUP = "https://txmccs.txdmv.gov/truckstop/companies/81edcff1-8a6e-4ed0-be1f-60668515e223/automated-motor-vehicles"
SOURCE = "https://txmccs.txdmv.gov/automated-vehicles/"
ISSUE_VIN_MAX = 80
ERROR_ISSUE_TITLE = "Texas DMV checker failed 3 times in a row"


def eprint(msg):
    print(msg, file=sys.stderr, flush=True)


def companies_hash(path):
    if not os.path.isfile(path):
        return "MISSING"
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    payload = json.dumps(data.get("companies"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def load_json(path, default):
    if not os.path.isfile(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return default
    return data if isinstance(data, dict) else default


def save_if_changed(path, obj):
    text = json.dumps(obj, indent=2) + "\n"
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            if f.read() == text:
                return False
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return True


def as_bool(value):
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def blank_state():
    return {
        "consecutive_failures": 0,
        "last_error": None,
        "last_failure_at": None,
        "error_alert_sent": False,
        "pending_tesla_alert": None,
        "pending_error_alert": None,
    }


def normalize_state(state):
    base = blank_state()
    if isinstance(state, dict):
        for key in base:
            if key in state:
                base[key] = state[key]
    base["consecutive_failures"] = int(base["consecutive_failures"] or 0)
    base["error_alert_sent"] = bool(base["error_alert_sent"])
    return base


def parse_time(value):
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def heartbeat_due(path, now):
    data = load_json(path, {})
    then = parse_time(data.get("last_checked")) if isinstance(data, dict) else None
    if then is None:
        return True
    return (now - then).total_seconds() >= HEARTBEAT_SECONDS


def load_report(path, exit_code):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("report is not an object")
    except (OSError, ValueError) as exc:
        data = {"status": "error", "error": f"unreadable checker report: {exc}"}
        exit_code = 1
    if data.get("status") == "error" and exit_code == 0:
        exit_code = 1
    if exit_code not in (0, 1, 10, 20):
        data = {"status": "error", "error": data.get("error") or f"unexpected checker exit {exit_code}"}
        exit_code = 1
    return data, exit_code


def md_cell(value):
    text = "" if value is None else str(value)
    return text.replace("|", "/").replace("\n", " ").strip()


def split_new_tesla(report):
    block = report.get("new_tesla_model_y_or_cybercab") or {}
    vehicles = block.get("vehicles") or []
    model_y = cybercab = 0
    for vehicle in vehicles:
        bucket = check.model_bucket(vehicle.get("model"))
        if bucket == "model_y":
            model_y += 1
        elif bucket == "cybercab":
            cybercab += 1
    others = report.get("new_tesla_other_or_unspecified_model") or []
    companies = [item for item in (report.get("new_companies") or []) if item.get("tesla")]
    return model_y, cybercab, others, companies, vehicles


def vehicle_table(vehicles, limit):
    rows = list(vehicles or [])
    shown = rows[:limit]
    lines = ["| VIN | Year | Make | Model | Operator |", "| --- | --- | --- | --- | --- |"]
    for vehicle in shown:
        lines.append("| " + " | ".join(md_cell(vehicle.get(key)) for key in ("vin", "year", "make", "model", "operator")) + " |")
    omitted = max(0, len(rows) - limit)
    if omitted:
        lines.append("")
        lines.append(f"{omitted} more vehicle(s) are not listed here.")
    return "\n".join(lines)


def slug_token(value):
    return re.sub(r"[^A-Za-z0-9]", "", value or "unknown")[:40]


def tesla_alert_id(report, vehicles, companies):
    checked = slug_token(report.get("checked_at"))
    vins = sorted(str(vehicle.get("vin") or "") for vehicle in vehicles)
    names = sorted(str(company.get("name") or "") for company in companies)
    digest = hashlib.sha256(("\n".join(vins) + "\n" + "\n".join(names)).encode()).hexdigest()[:12]
    return f"tesla-{checked}-{digest}"


def build_tesla_alert(report, status, run_url):
    model_y, cybercab, others, companies, vehicles = split_new_tesla(report)
    title = f"New Tesla robotaxi(s) on Texas DMV list: +{model_y} Model Y, +{cybercab} Cybercab"
    counts = (status or {}).get("tesla_counts") or {}
    delta = (status or {}).get("change_since_baseline") or {}
    lines = [
        "The Texas DMV automated vehicle list has a new Tesla entry.",
        "",
        f"- **Model Y:** +{model_y}",
        f"- **Cybercab:** +{cybercab}",
        f"- **Other Tesla vehicles:** +{len(others)}",
        f"- **New Tesla companies:** {len(companies)}",
    ]
    if counts:
        lines.append(
            "- **Tesla total after this check:** "
            f"{counts.get('total', 0)} "
            f"(Model Y {counts.get('model_y', 0)}, Cybercab {counts.get('cybercab', 0)}, other {counts.get('other', 0)})"
        )
    if delta:
        lines.append(
            "- **Change since the Oct 24, 2026 baseline:** "
            f"Model Y {delta.get('model_y', 0):+d}, "
            f"Cybercab {delta.get('cybercab', 0):+d}, "
            f"total {delta.get('total', 0):+d}"
        )
    lines.append("")
    if vehicles:
        lines.append(f"### New Model Y and Cybercab vehicles ({len(vehicles)})")
        lines.append("")
        lines.append(vehicle_table(vehicles, ISSUE_VIN_MAX))
        lines.append("")
    if others:
        lines.append(f"### Other new Tesla vehicles ({len(others)})")
        lines.append("")
        lines.append(vehicle_table(others, ISSUE_VIN_MAX))
        lines.append("")
    if companies:
        lines.append("### New Tesla companies")
        lines.append("")
        for company in companies:
            name = company.get("name") or "Unnamed company"
            page = company.get("page") or TESLA_LOOKUP
            auth = company.get("av_auth_number") or ""
            extra = f" ({auth})" if auth else ""
            lines.append(f"- [{name}]({page}){extra}")
        lines.append("")
    removed = report.get("removed_vehicles_count") or 0
    if removed:
        noun = "removal" if removed == 1 else "removals"
        lines.append(f"This same check also saw {removed} vehicle {noun}.")
        sample = report.get("removed_vehicles_sample") or []
        if sample:
            lines.append("")
            lines.append(vehicle_table(sample, 20))
        lines.append("")
    lines.append(f"Tesla's vehicle list: {TESLA_LOOKUP}")
    lines.append("")
    lines.append(f"Source: {SOURCE}")
    lines.append(f"Checked at: {report.get('checked_at') or 'unknown'}")
    if report.get("previous_check"):
        lines.append(f"Previous snapshot: {report['previous_check']}")
    if run_url:
        lines.append(f"Workflow run: {run_url}")
    alert_id = tesla_alert_id(report, vehicles, companies)
    lines.append("")
    lines.append(f"<!-- alert-id: {alert_id} -->")
    return {"alert_id": alert_id, "title": title, "body": "\n".join(lines) + "\n"}


def build_error_alert(report, state, run_url):
    error = str(report.get("error") or state.get("last_error") or "unknown error").replace("```", "'''")
    checked = report.get("checked_at") or state.get("last_failure_at") or "unknown"
    lines = [
        "The Texas DMV checker failed on 3 consecutive runs. A single failure does not open an issue.",
        "",
        "A failed fetch leaves the saved vehicle snapshot untouched. It is not treated as a list with no changes.",
        "",
        "**Last error**",
        "",
        "```",
        error,
        "```",
        "",
        f"**Last attempt:** {checked}",
        "",
        f"Source: {SOURCE}",
    ]
    if run_url:
        lines.append(f"Workflow run: {run_url}")
    alert_id = f"error-{slug_token(str(checked))}"
    lines.append("")
    lines.append(f"<!-- alert-id: {alert_id} -->")
    return {"alert_id": alert_id, "title": ERROR_ISSUE_TITLE, "body": "\n".join(lines) + "\n"}


def data_commit_message(report, exit_code):
    if report.get("baseline"):
        return "Create baseline AV snapshot"
    if exit_code == 10:
        model_y, cybercab, others, companies, _vehicles = split_new_tesla(report)
        return (
            "Track DMV list change: "
            f"+{model_y} Model Y, +{cybercab} Cybercab, +{len(others)} other Tesla, +{len(companies)} Tesla companies"
        )
    if exit_code == 20:
        added = (report.get("new_non_tesla") or {}).get("count", 0)
        companies = len(report.get("new_companies") or [])
        return f"Track DMV list change: +{added} non-Tesla vehicles, +{companies} companies"
    removed = report.get("removed_vehicles_count") or 0
    if removed:
        return f"Track DMV list change: {removed} vehicles removed"
    return "Track DMV list change"


def build_heartbeat(report, status, now_iso):
    counts = None
    operators = report.get("operators_found")
    result = report.get("result")
    if isinstance(status, dict):
        counts = status.get("tesla_counts")
        if operators is None and isinstance(status.get("operators"), list):
            operators = len(status["operators"])
        if result is None:
            result = status.get("last_result")
    return {
        "last_checked": now_iso,
        "last_result": result,
        "tesla_counts": counts,
        "operators_found": operators,
        "source": SOURCE,
    }


def record(args):
    report, exit_code = load_report(args.report, args.exit_code)
    status = load_json(args.status, {})
    state = normalize_state(load_json(args.state, {}))
    companies_changed = as_bool(args.companies_changed)
    open_error_issues = max(0, int(args.open_error_issues))
    now = parse_time(report.get("checked_at")) or datetime.now(timezone.utc)
    now_iso = report.get("checked_at") if parse_time(report.get("checked_at")) else now.isoformat(timespec="seconds")

    commit_data = exit_code in (0, 10, 20) and (companies_changed or exit_code in (10, 20))
    # A successful first snapshot (or any real company diff) commits. Exit 10/20
    # always commit even if the hash comparison was missed. Failures never commit
    # the vehicle files: check.py leaves the snapshot unchanged, and a partial
    # write must not replace the last good list.
    if exit_code == 1:
        commit_data = False

    open_tesla = False
    open_error = False
    prev_failures = state["consecutive_failures"]

    if exit_code == 1:
        error_text = str(report.get("error") or "unknown error")
        if prev_failures < FAILURES_BEFORE_ISSUE:
            state["consecutive_failures"] = prev_failures + 1
            state["last_error"] = error_text
            state["last_failure_at"] = now_iso
        reached = state["consecutive_failures"] >= FAILURES_BEFORE_ISSUE
        pending = state.get("pending_error_alert")
        if reached and not state["error_alert_sent"]:
            if open_error_issues > 0:
                state["error_alert_sent"] = True
                state["pending_error_alert"] = None
            elif not pending:
                state["pending_error_alert"] = build_error_alert(report, state, args.run_url)
        if state.get("pending_error_alert"):
            if open_error_issues > 0:
                state["error_alert_sent"] = True
                state["pending_error_alert"] = None
            else:
                open_error = True
    else:
        if (
            state["consecutive_failures"]
            or state["last_error"]
            or state["last_failure_at"]
            or state["error_alert_sent"]
            or state.get("pending_error_alert")
        ):
            state["consecutive_failures"] = 0
            state["last_error"] = None
            state["last_failure_at"] = None
            if not state.get("pending_error_alert"):
                state["error_alert_sent"] = False
        if state.get("pending_error_alert"):
            if open_error_issues > 0:
                state["error_alert_sent"] = True
                state["pending_error_alert"] = None
            else:
                open_error = True

    if exit_code == 10:
        state["pending_tesla_alert"] = build_tesla_alert(report, status, args.run_url)
    if state.get("pending_tesla_alert"):
        open_tesla = True

    commit_heartbeat = False
    if exit_code != 1 and (commit_data or heartbeat_due(args.heartbeat, now)):
        heartbeat = build_heartbeat(report, status, now_iso)
        commit_heartbeat = save_if_changed(args.heartbeat, heartbeat)

    commit_state = save_if_changed(args.state, normalize_state(state))

    if commit_data:
        message = data_commit_message(report, exit_code)
    elif commit_state and state["consecutive_failures"] == 0 and prev_failures:
        message = "Checker recovered; reset failure count"
    elif commit_heartbeat and commit_state:
        message = "Note last DMV check and update checker failure state"
    elif commit_heartbeat:
        message = "Note last DMV check (no list change)"
    elif commit_state and state["consecutive_failures"]:
        message = f"Record checker failure ({state['consecutive_failures']} consecutive)"
    elif commit_state:
        message = "Update checker failure state"
    else:
        message = "No tracker commit"

    tesla = state.get("pending_tesla_alert") or {}
    error = state.get("pending_error_alert") or {}
    decision = {
        "commit_data": commit_data,
        "commit_heartbeat": commit_heartbeat,
        "commit_state": commit_state,
        "commit_any": bool(commit_data or commit_heartbeat or commit_state),
        "commit_message": message,
        "open_tesla_issue": open_tesla,
        "open_error_issue": open_error,
        "tesla_issue_title": tesla.get("title"),
        "tesla_issue_body": tesla.get("body"),
        "tesla_alert_id": tesla.get("alert_id"),
        "error_issue_title": error.get("title"),
        "error_issue_body": error.get("body"),
        "error_alert_id": error.get("alert_id"),
        "checker_exit": exit_code,
        "companies_changed": companies_changed,
        "consecutive_failures": state["consecutive_failures"],
        "summary": message,
    }
    print(json.dumps(decision, indent=2))
    return 0


def ack(args):
    state = normalize_state(load_json(args.state, {}))
    if args.tesla:
        state["pending_tesla_alert"] = None
    if args.error:
        state["pending_error_alert"] = None
        state["error_alert_sent"] = True
    changed = save_if_changed(args.state, normalize_state(state))
    print(json.dumps({"cleared_tesla": bool(args.tesla), "cleared_error": bool(args.error), "state_changed": changed}, indent=2))
    return 0


def main():
    parser = argparse.ArgumentParser(description="Decide tracker commits and GitHub issue alerts")
    sub = parser.add_subparsers(dest="cmd", required=True)

    hashed = sub.add_parser("hash")
    hashed.add_argument("snapshot")

    recorded = sub.add_parser("record")
    recorded.add_argument("--exit-code", type=int, required=True)
    recorded.add_argument("--report", required=True)
    recorded.add_argument("--companies-changed", required=True)
    recorded.add_argument("--open-error-issues", type=int, required=True)
    recorded.add_argument("--run-url", default="")
    recorded.add_argument("--state", default=os.path.join(ROOT, "data", "run_state.json"))
    recorded.add_argument("--heartbeat", default=os.path.join(ROOT, "data", "last_checked.json"))
    recorded.add_argument("--status", default=os.path.join(ROOT, "data", "status.json"))

    acknowledged = sub.add_parser("ack")
    acknowledged.add_argument("--tesla", action="store_true")
    acknowledged.add_argument("--error", action="store_true")
    acknowledged.add_argument("--state", default=os.path.join(ROOT, "data", "run_state.json"))

    args = parser.parse_args()
    if args.cmd == "hash":
        print(companies_hash(args.snapshot))
        return 0
    if args.cmd == "record":
        return record(args)
    if args.cmd == "ack":
        if not args.tesla and not args.error:
            eprint("ack needs --tesla and/or --error")
            return 2
        return ack(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
