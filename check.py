#!/usr/bin/env python3
"""Texas AV registry monitor (TxDMV TxMCCS "Truck Stop" public lookup).

Source: Texas DMV, Motor Carrier & Automated Motor Vehicle Operator Lookup
  UI : https://txmccs.txdmv.gov/automated-vehicles/
  API: https://txmccs.txdmv.gov/api/TruckStop/companies?searchType=company_name&searchValue=...
       https://txmccs.txdmv.gov/api/TruckStop/companies/{id}/automated-motor-vehicles?limit=100&offset=N

Compares current authorized AV operators + their vehicle lists (VIN, year, make, model)
against snapshot.json and reports NEW entries, highlighting Tesla Model Y / Cybercab.

Exit codes: 0 nothing new | 10 new Tesla entry (Model Y/Cybercab or any Tesla)
            20 new non-Tesla entries only | 1 error (fetch/format failure; snapshot NOT updated)
Usage: python3 check.py [--snapshot PATH] [--no-save] [--tesla-only] [--status PATH]

Each run (unless --no-save) also writes status.json (dashboard feed): last_checked, Tesla counts by
model, per-operator counts, change since baseline, and an appended change log. With --tesla-only the
non-Tesla operators are not re-fetched; their last known data is carried forward (snapshot + status).

A failed fetch exits 1 and leaves the snapshot untouched. That is never reported as "no change".
"""
import argparse, json, os, re, sys, time, urllib.parse, urllib.request
from datetime import datetime, timezone

BASE = "https://txmccs.txdmv.gov/api/TruckStop/companies"
UI = "https://txmccs.txdmv.gov/truckstop/companies/{}/automated-motor-vehicles"
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SNAPSHOT = os.path.join(HERE, "snapshot.json")
DEFAULT_STATUS = os.path.join(HERE, "status.json")
CHANGE_LOG_MAX = 1000
VIN_SAMPLE_MAX = 40
UA = "Mozilla/5.0 (tx-av-monitor; personal read-only checker)"
PAGE = 100  # API max
MAX_SEARCH_RESULTS = 1000  # cap per broad search term
# Search terms used to discover AV-authorized companies (API has no "list all AV operators" call).
# Includes the 15 operators TxDMV reported as authorized (May/Jun 2026) + generic terms for new ones.
SEARCH_TERMS = ["tesla", "robotaxi", "cybercab", "waymo", "zoox", "nuro", "moia", "may mobility",
                "gatik", "kodiak", "aurora operations", "truck opco", "stack opco", "stack av", "opco", "bot auto", "torc",
                "avride", "waabi", "international transport engineering", "autonomous", "driverless",
                "automated", "self driving", "self-driving", "robot"]
MODEL_RE = re.compile(r"model\s*-?\s*y\b|cyber\s*-?\s*cab", re.I)


class SourceError(Exception):
    pass


def eprint(msg):
    print(msg, file=sys.stderr, flush=True)


def get_json(url, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                if r.status != 200:
                    raise SourceError(f"HTTP {r.status} for {url}")
                ctype = r.headers.get("Content-Type", "")
                body = r.read()
            if "json" not in ctype:
                raise SourceError(f"Unexpected content-type {ctype!r} for {url} (site format may have changed)")
            return json.loads(body)
        except (SourceError, ValueError, OSError) as e:
            last = e
            time.sleep(2 * (i + 1))
    raise SourceError(f"Fetch failed after {tries} tries: {url}: {last}")


def is_tesla(*texts):
    return any("tesla" in (t or "").lower() for t in texts)


def discover_av_companies(terms):
    found = {}
    for term in terms:
        eprint(f"search {term!r}")
        offset = 0
        while True:
            q = urllib.parse.urlencode({"searchType": "company_name", "searchValue": term, "limit": PAGE, "offset": offset})
            d = get_json(f"{BASE}?{q}")
            if not isinstance(d, dict) or "results" not in d:
                raise SourceError(f"Company search format changed (no 'results') for term {term!r}")
            for r in d["results"]:
                if r.get("autonomousVehicleAuthorizationNumber"):
                    found[r["businessEntityId"]] = {
                        "id": r["businessEntityId"],
                        "name": r.get("companyName", ""),
                        "av_auth_number": r.get("autonomousVehicleAuthorizationNumber"),
                        "av_status": r.get("autonomousVehicleStatus"),
                    }
            offset += PAGE
            time.sleep(0.3)
            if offset >= min(d.get("total", 0), MAX_SEARCH_RESULTS) or not d["results"]:
                break
    return found


def fetch_company_detail(cid):
    d = get_json(f"{BASE}/{cid}")
    return {"authorized_at": d.get("autonomousVehicleAuthorizedAt"), "av_status": d.get("autonomousVehicleStatus"),
            "av_auth_number": d.get("autonomousVehicleAuthorizationNumber")}


def fetch_vehicles(cid):
    out, offset, total = {}, 0, None
    while True:
        q = urllib.parse.urlencode({"limit": PAGE, "offset": offset})
        d = get_json(f"{BASE}/{cid}/automated-motor-vehicles?{q}")
        if not isinstance(d, dict) or "vehicles" not in d or "total" not in d:
            raise SourceError(f"Vehicle list format changed for company {cid}: keys={list(d)[:10] if isinstance(d, dict) else type(d)}")
        total = d["total"]
        for v in d["vehicles"]:
            if "vin" not in v:
                raise SourceError(f"Vehicle record missing 'vin' for company {cid}")
            out[v["vin"].upper()] = {"vin": v["vin"].upper(), "year": v.get("modelYear"),
                                     "make": v.get("make"), "model": v.get("model")}
        offset += PAGE
        if offset >= total or not d["vehicles"]:
            break
        time.sleep(0.3)
    if len(out) != total:
        raise SourceError(f"Company {cid}: got {len(out)} unique VINs but API says total={total} "
                          f"(list changed mid-fetch or paging broke); not trusting this run")
    return out


def collect(tesla_only):
    terms = ["tesla", "robotaxi", "cybercab"] if tesla_only else SEARCH_TERMS
    companies = discover_av_companies(terms)
    if not companies:
        raise SourceError("No AV-authorized companies found at all; source/format likely changed")
    if not any(is_tesla(c["name"]) for c in companies.values()):
        raise SourceError("Tesla Robotaxi, LLC not found in search; source/format likely changed")
    for cid, c in companies.items():
        eprint(f"vehicles {c.get('name')} ({cid})")
        c.update({k: v for k, v in fetch_company_detail(cid).items() if v is not None})
        c["vehicles"] = fetch_vehicles(cid)
        c["vehicle_count"] = len(c["vehicles"])
        eprint(f"  {c['vehicle_count']} vehicles")
    return companies


def summarize(vehicles):
    s = {}
    for v in vehicles:
        k = " ".join(str(x).strip().title() for x in (v.get("year"), v.get("make"), v.get("model")))
        s[k] = s.get(k, 0) + 1
    return dict(sorted(s.items()))


def model_bucket(model):
    m = (model or "").lower()
    if re.search(r"model\s*-?\s*y\b", m):
        return "model_y"
    if re.search(r"cyber\s*-?\s*cab", m):
        return "cybercab"
    return "other"


def tesla_counts(companies):
    out = {"model_y": 0, "cybercab": 0, "other": 0, "total": 0}
    for c in companies.values():
        if not is_tesla(c.get("name")):
            continue
        for v in c.get("vehicles", {}).values():
            out[model_bucket(v.get("model"))] += 1
            out["total"] += 1
    return out


def sample_vehicles(records, limit=VIN_SAMPLE_MAX):
    out = []
    for v in (records or [])[:limit]:
        out.append({k: v.get(k) for k in ("vin", "year", "make", "model", "operator")})
    return out


def write_json_atomic(path, obj, indent=2):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        if indent is None:
            json.dump(obj, f)
        else:
            json.dump(obj, f, indent=indent)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_status(path):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except ValueError:
            pass
    return {}


def write_status(path, now, code, companies=None, refreshed_ids=(), report=None, error=None, prev_snapshot=None):
    st = load_status(path)
    st["source"] = "https://txmccs.txdmv.gov/automated-vehicles/"
    st["last_attempt"] = now
    st.setdefault("change_log", [])
    if error is not None:
        st["last_attempt_status"] = "error"
        st["last_error"] = error
        write_json_atomic(path, st)
        return
    st["last_attempt_status"] = "ok"
    st["last_error"] = None
    st["last_checked"] = now
    counts = tesla_counts(companies)
    if "baseline" not in st:
        base_src = prev_snapshot if prev_snapshot else {"checked_at": now, "companies": companies}
        st["baseline"] = dict(tesla_counts(base_src.get("companies", {})), as_of=base_src.get("checked_at"))
    b = st["baseline"]
    st["tesla_counts"] = counts
    st["change_since_baseline"] = {k: counts[k] - b.get(k, 0) for k in ("model_y", "cybercab", "other", "total")}
    teslas = []
    for cid, c in companies.items():
        if is_tesla(c.get("name")):
            teslas.append({"name": c["name"], "av_auth_number": c.get("av_auth_number"), "status": c.get("av_status"),
                           "authorized_at": c.get("authorized_at"), "page": UI.format(cid),
                           "counts": tesla_counts({cid: c}), "by_model_year": summarize(c["vehicles"].values())})
    st["tesla_operators"] = teslas
    prev_ops = {o["id"]: o for o in st.get("operators", []) if "id" in o}
    ops = []
    for cid, c in companies.items():
        ops.append({"id": cid, "name": c["name"], "av_auth_number": c.get("av_auth_number"), "status": c.get("av_status"),
                    "authorized_at": c.get("authorized_at"), "vehicle_count": c.get("vehicle_count", len(c.get("vehicles", {}))),
                    "tesla": is_tesla(c["name"]), "page": UI.format(cid),
                    "as_of": now if cid in refreshed_ids else prev_ops.get(cid, {}).get("as_of", (prev_snapshot or {}).get("checked_at"))})
    st["operators"] = sorted(ops, key=lambda o: (-o["vehicle_count"], o["name"]))
    if report is not None and code in (10, 20):
        nt = report.get("new_tesla_model_y_or_cybercab") or {}
        nt_vehicles = nt.get("vehicles") or []
        other_vehicles = report.get("new_tesla_other_or_unspecified_model") or []
        st["change_log"].append({
            "checked_at": now, "result": report.get("result"), "exit_code": code,
            "tesla_counts_after": counts,
            "new_tesla_model_y_or_cybercab": {k: nt[k] for k in ("count", "by_model") if k in nt},
            "new_tesla_model_y_or_cybercab_sample": sample_vehicles(nt_vehicles),
            "new_tesla_model_y_or_cybercab_sample_omitted": max(0, len(nt_vehicles) - VIN_SAMPLE_MAX),
            "new_tesla_other_or_unspecified_model": len(other_vehicles),
            "new_tesla_other_sample": sample_vehicles(other_vehicles),
            "new_companies": [{k: n[k] for k in ("name", "tesla", "page") if k in n} for n in report.get("new_companies", [])],
            "new_non_tesla": report.get("new_non_tesla"),
            "removed_vehicles_count": report.get("removed_vehicles_count"),
            "removed_vehicles_by_model": report.get("removed_vehicles_by_model"),
            "removed_vehicles_sample": report.get("removed_vehicles_sample"),
            "removed_vehicles_sample_omitted": report.get("removed_vehicles_sample_omitted", 0)})
    elif report is not None and report.get("removed_vehicles_count"):
        st["change_log"].append({"checked_at": now, "result": "vehicles removed only", "exit_code": code,
                                 "tesla_counts_after": counts, "removed_vehicles_count": report["removed_vehicles_count"],
                                 "removed_vehicles_by_model": report.get("removed_vehicles_by_model"),
                                 "removed_vehicles_sample": report.get("removed_vehicles_sample"),
                                 "removed_vehicles_sample_omitted": report.get("removed_vehicles_sample_omitted", 0)})
    st["change_log"] = st["change_log"][-CHANGE_LOG_MAX:]
    st["last_result"] = report.get("result") if report else None
    write_json_atomic(path, st)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default=DEFAULT_SNAPSHOT)
    ap.add_argument("--no-save", action="store_true", help="don't update snapshot")
    ap.add_argument("--tesla-only", action="store_true", help="only discover Tesla-named operators")
    ap.add_argument("--status", default=DEFAULT_STATUS, help="dashboard status file (written unless --no-save)")
    a = ap.parse_args()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        cur = collect(a.tesla_only)
    except Exception as e:
        if not a.no_save:
            try:
                write_status(a.status, now, 1, error=str(e))
            except Exception:
                pass
        print(json.dumps({"status": "error", "checked_at": now, "error": str(e),
                          "note": "Fetch/format failure - NOT treated as 'no change'; snapshot left untouched."}, indent=2))
        return 1

    prev = None
    if os.path.exists(a.snapshot):
        with open(a.snapshot, encoding="utf-8") as f:
            prev = json.load(f)

    tesla_current = {c["name"]: {"av_auth_number": c.get("av_auth_number"), "status": c.get("av_status"),
                                 "authorized_at": c.get("authorized_at"), "vehicle_count": c["vehicle_count"],
                                 "by_model": summarize(c["vehicles"].values()),
                                 "page": UI.format(cid)}
                     for cid, c in cur.items() if is_tesla(c["name"])}
    totals = {c["name"]: c["vehicle_count"] for c in cur.values()}

    report = {"status": "ok", "checked_at": now, "source": "https://txmccs.txdmv.gov/automated-vehicles/",
              "operators_found": len(cur), "vehicle_totals_by_operator": totals, "tesla_current": tesla_current}
    code = 0
    if prev is None:
        report["baseline"] = True
        report["message"] = "Baseline snapshot created; no comparison on first run."
    else:
        pc = prev.get("companies", {})
        new_companies, new_tesla_target, new_tesla_other, new_other, removed = [], [], [], [], []
        for cid, c in cur.items():
            tesla_co = is_tesla(c["name"])
            if cid not in pc:
                new_companies.append({"name": c["name"], "av_auth_number": c.get("av_auth_number"),
                                      "status": c.get("av_status"), "tesla": tesla_co, "page": UI.format(cid)})
            old_v = pc.get(cid, {}).get("vehicles", {})
            for vin, v in c["vehicles"].items():
                if vin in old_v:
                    continue
                rec = dict(v, operator=c["name"])
                if tesla_co or is_tesla(v.get("make")):
                    (new_tesla_target if MODEL_RE.search(v.get("model") or "") else new_tesla_other).append(rec)
                else:
                    new_other.append(rec)
            for vin in old_v:
                if vin not in c["vehicles"]:
                    removed.append(dict(old_v[vin], operator=c["name"]))
        report["new_companies"] = new_companies
        report["new_tesla_model_y_or_cybercab"] = {"count": len(new_tesla_target), "by_model": summarize(new_tesla_target), "vehicles": new_tesla_target}
        report["new_tesla_other_or_unspecified_model"] = new_tesla_other
        report["new_non_tesla"] = {"count": len(new_other), "by_operator_model": summarize(
            [dict(v, make=v["operator"] + " / " + str(v.get("make"))) for v in new_other])}
        report["removed_vehicles_count"] = len(removed)
        report["removed_vehicles_by_model"] = summarize(removed)
        report["removed_vehicles_sample"] = sample_vehicles(removed)
        report["removed_vehicles_sample_omitted"] = max(0, len(removed) - VIN_SAMPLE_MAX)
        report["previous_check"] = prev.get("checked_at")
        if new_tesla_target or new_tesla_other or any(n["tesla"] for n in new_companies):
            code = 10
        elif new_other or new_companies:
            code = 20
        report["result"] = {0: "no new entries", 10: "NEW TESLA ENTRIES", 20: "new non-Tesla entries only"}[code]

    if not a.no_save:
        refreshed = set(cur)
        saved = dict(cur)
        if a.tesla_only and prev:
            # carry forward operators this run did not re-fetch, so the snapshot keeps the full picture
            for cid, c in prev.get("companies", {}).items():
                if cid not in saved and not is_tesla(c.get("name")):
                    saved[cid] = c
        write_json_atomic(a.snapshot, {"checked_at": now, "companies": saved}, indent=None)
        try:
            write_status(a.status, now, code, companies=saved, refreshed_ids=refreshed, report=report, prev_snapshot=prev)
        except Exception as e:
            report["status_file_error"] = str(e)
    print(json.dumps(report, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
