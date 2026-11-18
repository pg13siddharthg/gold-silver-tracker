#!/usr/bin/env python3
"""
Daily Gold & Silver (India) price check.

- Fetches gold (XAU) and silver (XAG) prices in INR per troy ounce from the
  free, key-less fawazahmed0 currency API (with a fallback mirror).
- Converts to Indian quoting units: gold per 10 g (24K and 22K), silver per 1 kg,
  and adds Indian import duty (and optionally GST) to estimate the domestic rate.
- Appends the day's prices to docs/history.json (read by the phone app).
- Sends a push notification to your Android phone through ntfy.sh when a price
  falls past your thresholds.

Standard library only, so it runs on GitHub Actions with no installs.

Usage:
    NTFY_TOPIC=my-secret-topic python scripts/check_prices.py
    python scripts/check_prices.py --dry-run            # print, don't notify
    python scripts/check_prices.py --mock prices.json   # use fake prices (testing)
"""

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "docs" / "config.json"
HISTORY_PATH = ROOT / "docs" / "history.json"

TROY_OZ_G = 31.1034768
PURITY_22K = 0.916
MAX_HISTORY = 800  # ~2 years of daily points

SOURCES = [
    "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/{m}.min.json",
    "https://latest.currency-api.pages.dev/v1/currencies/{m}.min.json",
]

METALS = {
    "gold": {"key": "gold_24k_10g", "label": "Gold 24K", "unit": "10g", "emoji": "1st_place_medal"},
    "silver": {"key": "silver_1kg", "label": "Silver", "unit": "kg", "emoji": "2nd_place_medal"},
}


# ---------------------------------------------------------------- fetching
def fetch_inr_per_oz(metal_code):
    """Return (date_str, INR per troy ounce) for 'xau' or 'xag'."""
    last_err = None
    for url in SOURCES:
        try:
            req = urllib.request.Request(url.format(m=metal_code),
                                         headers={"User-Agent": "gold-silver-tracker"})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.load(r)
            inr = float(data[metal_code]["inr"])
            if inr <= 0:
                raise ValueError("non-positive price")
            return data.get("date"), inr
        except Exception as e:  # try next mirror
            last_err = e
    raise RuntimeError(f"Could not fetch {metal_code}: {last_err}")


def build_entry(cfg, gold_oz, silver_oz, date_str):
    duty = 1 + cfg.get("import_duty_pct", 0) / 100
    gst = 1 + (cfg.get("gst_pct", 0) / 100 if cfg.get("include_gst") else 0)
    gold_spot_10g = gold_oz / TROY_OZ_G * 10
    silver_spot_kg = silver_oz / TROY_OZ_G * 1000
    g24 = gold_spot_10g * duty * gst
    return {
        "date": date_str,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "gold_24k_10g": round(g24, 2),
        "gold_22k_10g": round(g24 * PURITY_22K, 2),
        "silver_1kg": round(silver_spot_kg * duty * gst, 2),
        "gold_spot_10g": round(gold_spot_10g, 2),
        "silver_spot_kg": round(silver_spot_kg, 2),
    }


# ---------------------------------------------------------------- analysis
def pct(new, old):
    return (new - old) / old * 100 if old else 0.0


def inr(x):
    """Indian digit grouping, e.g. 1,23,456."""
    s = f"{x:,.0f}"
    whole = s.replace(",", "")
    if len(whole) <= 3:
        return "₹" + whole
    head, tail = whole[:-3], whole[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return "₹" + ",".join(groups) + "," + tail


def evaluate(cfg, history, entry):
    """Return (alerts, summary_lines). history excludes today's entry."""
    alerts, summary = [], []
    lookback = int(cfg.get("lookback_days", 30))
    prev = history[-1] if history else None
    window = history[-lookback:] if history else []

    for metal, meta in METALS.items():
        key = meta["key"]
        rules = cfg.get("alerts", {}).get(metal, {})
        price = entry[key]
        line = f"{meta['label']}: {inr(price)}/{meta['unit']}"

        if prev and prev.get(key):
            day = pct(price, prev[key])
            line += f" ({day:+.2f}%)"
            thr = rules.get("daily_drop_pct")
            if thr is not None and day <= -abs(thr):
                alerts.append({
                    "metal": metal, "type": "daily_drop",
                    "title": f"{meta['label']} down {abs(day):.2f}% today",
                    "message": f"{inr(prev[key])} → {inr(price)} per {meta['unit']} "
                               f"(threshold {abs(thr)}%)",
                })

        if window:
            high = max(e[key] for e in window + [entry] if e.get(key))
            dd = pct(price, high)
            thr = rules.get("drop_from_high_pct")
            if thr is not None and dd <= -abs(thr):
                # alert only on the day it first crosses, not every day after
                prev_high = max(e[key] for e in window if e.get(key))
                prev_dd = pct(prev[key], prev_high) if prev else 0
                if prev_dd > -abs(thr):
                    alerts.append({
                        "metal": metal, "type": "drop_from_high",
                        "title": f"{meta['label']} {abs(dd):.1f}% below {lookback}-day high",
                        "message": f"Now {inr(price)} vs high {inr(high)} per {meta['unit']}",
                    })

        floor = rules.get("below_price")
        if floor:
            was_above = (prev is None) or prev.get(key, float("inf")) >= floor
            if price < floor and was_above:
                alerts.append({
                    "metal": metal, "type": "below_price",
                    "title": f"{meta['label']} below your target {inr(floor)}",
                    "message": f"Now {inr(price)} per {meta['unit']}",
                })
        summary.append(line)

    summary.append(f"22K gold: {inr(entry['gold_22k_10g'])}/10g")
    return alerts, summary


# ---------------------------------------------------------------- notify
def notify(topic, title, message, priority=3, tags=None, click=None, dry_run=False):
    payload = {"topic": topic, "title": title, "message": message,
               "priority": priority, "tags": tags or []}
    if click:
        payload["click"] = click
    if dry_run or not topic:
        print(f"[notify p{priority}] {title}\n    {message}")
        return
    req = urllib.request.Request(
        "https://ntfy.sh/", data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


# ---------------------------------------------------------------- main
def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print notifications instead of sending")
    ap.add_argument("--mock", help='JSON file: {"date": "...", "xau_inr": ..., "xag_inr": ...}')
    ap.add_argument("--history", default=str(HISTORY_PATH))
    args = ap.parse_args()

    cfg = load_json(CONFIG_PATH, {})
    hist_path = Path(args.history)
    history = load_json(hist_path, [])
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    click = cfg.get("dashboard_url") or None

    try:
        if args.mock:
            m = json.loads(Path(args.mock).read_text())
            date_str, gold_oz, silver_oz = m["date"], m["xau_inr"], m["xag_inr"]
        else:
            d1, gold_oz = fetch_inr_per_oz("xau")
            d2, silver_oz = fetch_inr_per_oz("xag")
            date_str = d1 or d2 or datetime.now(timezone.utc).date().isoformat()
    except Exception as e:
        notify(topic, "Price check failed", str(e)[:300], priority=2,
               tags=["warning"], dry_run=args.dry_run)
        print(e, file=sys.stderr)
        return 1

    entry = build_entry(cfg, gold_oz, silver_oz, date_str)

    # If the feed hasn't published a new day yet, refresh today's row and stop
    # (prevents duplicate alerts when the job runs twice on the same data).
    if history and history[-1].get("date") == date_str:
        entry["alerts"] = history[-1].get("alerts", [])
        history[-1] = entry
        hist_path.write_text(json.dumps(history, indent=1), encoding="utf-8")
        print(f"No new data for {date_str}; updated existing row, no alerts sent.")
        return 0

    alerts, summary = evaluate(cfg, history, entry)
    entry["alerts"] = [a["title"] for a in alerts]
    history.append(entry)
    hist_path.write_text(json.dumps(history[-MAX_HISTORY:], indent=1), encoding="utf-8")

    summary_text = "\n".join(summary)
    print(f"{date_str}\n{summary_text}")

    for a in alerts:
        notify(topic, a["title"], a["message"] + "\n\n" + summary_text,
               priority=4 if a["type"] != "below_price" else 5,
               tags=["chart_with_downwards_trend", METALS[a["metal"]]["emoji"]],
               click=click, dry_run=args.dry_run)

    if not alerts and cfg.get("daily_summary"):
        notify(topic, f"Gold & Silver – {date_str}", summary_text, priority=2,
               tags=["bar_chart"], click=click, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
