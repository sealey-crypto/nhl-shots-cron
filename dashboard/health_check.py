"""Read-only health check for the public NHL research dashboard.

Uses Python's standard library only. No secrets, paid APIs, model execution,
wagers, Render deploys, or writes to historical research data.
"""
import datetime as dt
import json
import sys
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

BASE = "https://nhl-parlay-command-centre.onrender.com"
TORONTO = ZoneInfo("America/Toronto")


def get(path):
    req = urllib.request.Request(
        BASE + path,
        headers={"User-Agent": "nhl-research-healthcheck/1.0"},
    )
    with urllib.request.urlopen(req, timeout=25) as response:
        if response.status != 200:
            raise RuntimeError(f"{path}: HTTP {response.status}")
        return response.read()


def main():
    now = dt.datetime.now(dt.timezone.utc)
    local = now.astimezone(TORONTO)
    issues = []
    try:
        html = get("/").decode("utf-8", errors="replace")
        if "<html" not in html.lower():
            issues.append("Dashboard root did not return HTML")
        else:
            print("PASS dashboard root: HTTP 200 HTML")
    except (urllib.error.URLError, TimeoutError, ValueError, RuntimeError) as exc:
        issues.append(f"Dashboard root unavailable: {exc}")

    try:
        data = json.loads(get("/picks.json"))
        if not isinstance(data, dict):
            raise ValueError("snapshot must be an object")
        required = ("snapshot_date", "generated_at_utc", "legs", "model_b_count", "model_c_count")
        missing = [key for key in required if key not in data]
        if missing:
            issues.append(f"Snapshot missing fields: {', '.join(missing)}")
        elif not isinstance(data["legs"], list):
            issues.append("Snapshot legs is not a list")
        else:
            print(f"PASS snapshot schema: {len(data['legs'])} research legs")
        stamp = dt.datetime.fromisoformat(str(data["generated_at_utc"]).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("snapshot timestamp lacks timezone")
        age = (now - stamp.astimezone(dt.timezone.utc)).total_seconds() / 3600
        print(f"Snapshot age: {age:.2f} hours; date: {data['snapshot_date']}; "
              f"B={data['model_b_count']} C={data['model_c_count']}")
        # Publishing is scheduled during the NHL day, not overnight.
        # Grace period avoids false alarms before the first scheduled publish.
        if 13 <= local.hour <= 23:
            if data["snapshot_date"] != local.date().isoformat():
                issues.append("Snapshot date is stale during the publishing window")
            if age > 3:
                issues.append(f"Snapshot is {age:.1f} hours old during the publishing window")
        # Zero legs is not an outage: there may be no eligible positive-EV picks.
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        issues.append(f"Snapshot unavailable or invalid: {exc}")

    if issues:
        for issue in issues:
            print(f"::error::{issue}")
        print("FAIL dashboard health check")
        return 1
    print("PASS dashboard health check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
