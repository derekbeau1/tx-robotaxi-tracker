#!/usr/bin/env python3
"""Send one ntfy phone push for a new Tesla DMV entry.

The push uses the same alert id as the GitHub issue. A given id is sent
once. --dry-run prints the curl command and does not call ntfy. A network
failure is reported and does not become a non-zero exit.
"""
import argparse, json, os, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import decide  # noqa: E402

DEFAULT_CONFIG = os.path.join(ROOT, "notify.json")


def eprint(msg):
    print(msg, file=sys.stderr, flush=True)


def load_config(path):
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("notify config is not an object")
    return data


def push_title(cybercab, model_y):
    return f"Tesla added {int(cybercab)} Cybercab / {int(model_y)} Model Y to Texas list"


def push_body(totals, cybercab, model_y, click):
    lines = []
    if isinstance(totals, dict) and totals:
        lines.append(
            "Tesla now has %s Model Y, %s Cybercab, and %s total on the Texas list."
            % (int(totals.get("model_y") or 0), int(totals.get("cybercab") or 0), int(totals.get("total") or 0))
        )
    lines.append("This change: %s Cybercab, %s Model Y." % (int(cybercab), int(model_y)))
    if click:
        lines.append(click)
    return "\n".join(lines)


def header_value(value):
    return str(value or "").replace("\r", " ").replace("\n", " ").strip()


def curl_command(url, title, body, click):
    return [
        "curl",
        "-fsS",
        "--max-time",
        "15",
        "--retry",
        "0",
        "-H",
        "Title: " + header_value(title),
        "-H",
        "Click: " + header_value(click),
        "--data-binary",
        body,
        url,
    ]


def plan_push(decision, state):
    if not decision.get("open_tesla_issue"):
        return {"action": "skipped", "reason": "no tesla alert"}
    alert_id = decision.get("tesla_alert_id") or ""
    if not alert_id:
        return {"action": "skipped", "reason": "missing alert id"}
    if state.get("ntfy_sent_alert_id") == alert_id:
        return {"action": "skipped", "reason": "already sent", "alert_id": alert_id}
    cybercab = int(decision.get("tesla_cybercab_added") or 0)
    model_y = int(decision.get("tesla_model_y_added") or 0)
    return {
        "action": "send",
        "alert_id": alert_id,
        "title": push_title(cybercab, model_y),
        "body": None,
        "cybercab": cybercab,
        "model_y": model_y,
        "totals": decision.get("tesla_totals") or {},
    }


def run_curl(cmd):
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(detail or f"curl exited {proc.returncode}")
    return proc.stdout


def notify(decision, state, config, dry_run, transport):
    url = os.environ.get("NTFY_URL") or config.get("ntfy_url") or ""
    click = os.environ.get("TRACKER_URL") or config.get("click_url") or ""
    planned = plan_push(decision, state)
    if planned["action"] != "send":
        return planned
    title = planned["title"]
    body = push_body(planned["totals"], planned["cybercab"], planned["model_y"], click)
    cmd = curl_command(url, title, body, click)
    result = {
        "action": "dry-run" if dry_run else "sent",
        "alert_id": planned["alert_id"],
        "title": title,
        "body": body,
        "click": click,
        "url": url,
        "curl": cmd,
    }
    if dry_run:
        result["action"] = "dry-run"
        return result
    if not url.startswith("https://"):
        return {"action": "failed", "error": "ntfy url is missing", "alert_id": planned["alert_id"], "title": title}
    try:
        transport(cmd)
    except Exception as exc:
        return {
            "action": "failed",
            "error": str(exc),
            "alert_id": planned["alert_id"],
            "title": title,
            "body": body,
        }
    state["ntfy_sent_alert_id"] = planned["alert_id"]
    result["state_changed"] = True
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Send one ntfy push for a new Tesla entry")
    parser.add_argument("--decision", required=True)
    parser.add_argument("--state", default=os.path.join(ROOT, "data", "run_state.json"))
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--result", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        decision = json.load(open(args.decision, encoding="utf-8"))
        config = load_config(args.config)
        state = decide.normalize_state(decide.load_json(args.state, {}))
        outcome = notify(decision, state, config, args.dry_run, run_curl)
        if outcome.get("state_changed"):
            decide.save_if_changed(args.state, decide.normalize_state(state))
    except Exception as exc:
        outcome = {"action": "failed", "error": str(exc)}
        eprint(f"phone push failed: {exc}")
    text = json.dumps(outcome, indent=2)
    print(text)
    if args.result:
        parent = os.path.dirname(args.result)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(args.result, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    if outcome.get("action") == "failed":
        eprint("ntfy was unreachable or rejected the push. The run will continue.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
