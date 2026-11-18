#!/usr/bin/env python3
"""
Hourly Gold & Silver (India) price check.

Every run (hourly via GitHub Actions):
  1. Fetches live gold & silver prices in INR (xaus.com, free, no key),
     falling back to the daily fawazahmed0 feed if that is unavailable.
  2. Converts to Indian units (gold /10g 24K & 22K, silver /kg) and adds
     Indian import duty (and optionally GST).
  3. Updates today's row in docs/history.json (one row per India date; the last
     run of the day becomes that day's close) and today's hourly points.
  4. Sends ntfy push alerts on falls -- each kind of alert at most once a day --
     and one daily summary at the configured India-time hour.

Standard library only.

Usage:
    NTFY_TOPIC=my-topic python scripts/check_prices.py
    python scripts/check_prices.py --dry-run
    python scripts/check_prices.py --dry-run --mock m.json --now 2026-11-19T06:30:00Z
        (m.json: {"xau_inr_oz": 360000, "xag_inr_oz": 4400})
"""

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "docs" / "config.json"
HISTORY_PATH = ROOT / "docs" / "history.json"

IST = timezone(timedelta(hours=5, minutes=30))
TROY_OZ_G = 31.1034768
PURITY_22K = 0.916
MAX_HISTORY = 800
UA = {"User-Agent": "gold-silver-tracker (personal use)"}

METALS = {
    "gold": {"key": "gold_24k_10g", "label": "Gold 24K", "unit": "10g", "emoji": "1st_place_medal"},
    "silver": {"key": "silver_1kg", "label": "Silver", "unit": "kg", "emoji": "2nd_place_medal"},
}


# ---------------------------------------------------------------- fetching
def get_json(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def fetch_live():
    """Live INR/oz for gold and silver from xaus.com."""
    d = get_json("https://xaus.com/api/v1/spot?currency=INR&unit=oz")
    gold_inr = float(d["xau"]["price"])
    gold_usd = float(d["spot_usd_oz"])
    silver_usd = float(d["silver_usd_oz"])
    if min(gold_inr, gold_usd, silver_usd) <= 0:
        raise ValueError("bad live values")
    fx = gold_inr / gold_usd  # INR per USD implied by the same response
    return gold_inr, silver_usd * fx, "xaus.com live"


def fetch_daily():
    """Fallback: daily INR/oz from the fawazahmed0 currency API."""
    out = {}
    for m in ("xau", "xag"):
        for url in (f"https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/{m}.min.json",
                    f"https://latest.currency-api.pages.dev/v1/currencies/{m}.min.json"):
            try:
                out[m] = float(get_json(url)[m]["inr"])
                break
            except Exception:
                continue
        if m not in out:
            raise RuntimeError(f"daily feed unavailable for {m}")
    return out["xau"], out["xag"], "daily feed (fallback)"


def fetch_prices():
    try:
        return fetch_live()
    except Exception as e:
        print(f"Live source failed ({e}); using daily fallback.", file=sys.stderr)
        return fetch_daily()


# ---------------------------------------------------------------- helpers
def prices(cfg, gold_oz, silver_oz):
    duty = 1 + cfg.get("import_duty_pct", 0) / 100
    gst = 1 + (cfg.get("gst_pct", 0) / 100 if cfg.get("include_gst") else 0)
    g_spot = gold_oz / TROY_OZ_G * 10
    s_spot = silver_oz / TROY_OZ_G * 1000
    g24 = g_spot * duty * gst
    return {
        "gold_24k_10g": round(g24, 2),
        "gold_22k_10g": round(g24 * PURITY_22K, 2),
        "silver_1kg": round(s_spot * duty * gst, 2),
        "gold_spot_10g": round(g_spot, 2),
        "silver_spot_kg": round(s_spot, 2),
    }


def pct(new, old):
    return (new - old) / old * 100 if old else 0.0


def inr(x):
    whole = f"{x:.0f}"
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


def notify(topic, title, message, priority=3, tags=None, click=None, dry_run=False):
    payload = {"topic": topic, "title": title, "message": message,
               "priority": priority, "tags": tags or []}
    if click:
        payload["click"] = click
    if dry_run or not topic:
        print(f"[notify p{priority}] {title}\n    " + message.replace("\n", "\n    "))
        return
    req = urllib.request.Request("https://ntfy.sh/", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


# ---------------------------------------------------------------- alerts
def evaluate(cfg, past, today):
    """past = rows before today (oldest first); today = today's row (already updated).
    Returns list of new alerts (each kind at most once per India day)."""
    sent = set(today.setdefault("sent", []))
    alerts = []
    lookback = int(cfg.get("lookback_days", 30))
    prev = past[-1] if past else None

    for metal, meta in METALS.items():
        key, rules = meta["key"], cfg.get("alerts", {}).get(metal, {})
        price = today[key]

        thr = rules.get("daily_drop_pct")
        if prev and thr is not None and f"{metal}:daily_drop" not in sent:
            chg = pct(price, prev[key])
            if chg <= -abs(thr):
                alerts.append((metal, "daily_drop", 4,
                               f"{meta['label']} down {abs(chg):.2f}% today",
                               f"{inr(prev[key])} → {inr(price)} per {meta['unit']} since yesterday's close"))

        thr = rules.get("drop_from_high_pct")
        window = past[-lookback:]
        if prev and thr is not None and f"{metal}:drop_from_high" not in sent:
            high = max([r[key] for r in window] + [price])
            dd = pct(price, high)
            prev_high = max(r[key] for r in window)
            prev_dd = pct(prev[key], prev_high)
            if dd <= -abs(thr) and prev_dd > -abs(thr):  # newly crossed today
                alerts.append((metal, "drop_from_high", 4,
                               f"{meta['label']} {abs(dd):.1f}% below {lookback}-day high",
                               f"Now {inr(price)} vs high {inr(high)} per {meta['unit']}"))

        floor = rules.get("below_price")
        if floor and f"{metal}:below_price" not in sent:
            was_above = prev is None or prev[key] >= floor
            if price < floor and was_above:
                alerts.append((metal, "below_price", 5,
                               f"{meta['label']} below your target {inr(floor)}",
                               f"Now {inr(price)} per {meta['unit']}"))
    return alerts


def summary_text(past, today):
    prev = past[-1] if past else None
    lines = []
    for metal, meta in METALS.items():
        k = meta["key"]
        s = f"{meta['label']}: {inr(today[k])}/{meta['unit']}"
        if prev:
            s += f" ({pct(today[k], prev[k]):+.2f}%)"
        lines.append(s)
    lines.append(f"22K gold: {inr(today['gold_22k_10g'])}/10g")
    return "\n".join(lines)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--mock", help='JSON file {"xau_inr_oz":..., "xag_inr_oz":...}')
    ap.add_argument("--now", help="override current time (ISO, for testing)")
    ap.add_argument("--history", default=str(HISTORY_PATH))
    args = ap.parse_args()

    cfg = load_json(CONFIG_PATH, {})
    hist_path = Path(args.history)
    history = load_json(hist_path, [])
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    click = cfg.get("dashboard_url") or None
    now = (datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now
           else datetime.now(timezone.utc))
    now_ist = now.astimezone(IST)
    today_str = now_ist.date().isoformat()

    try:
        if args.mock:
            m = load_json(args.mock, {})
            gold_oz, silver_oz, source = m["xau_inr_oz"], m["xag_inr_oz"], "mock"
        else:
            gold_oz, silver_oz, source = fetch_prices()
    except Exception as e:
        print(e, file=sys.stderr)
        # one failure note per day, not every hour
        if history and history[-1].get("date") == today_str and "fail" in history[-1].get("sent", []):
            return 1
        notify(topic, "Price check failed", str(e)[:300], 2, ["warning"], dry_run=args.dry_run)
        if history and history[-1].get("date") == today_str:
            history[-1].setdefault("sent", []).append("fail")
            hist_path.write_text(json.dumps(history, indent=1), encoding="utf-8")
        return 1

    p = prices(cfg, gold_oz, silver_oz)

    # today's row: create on the first run of the India day, update after
    if history and history[-1].get("date") == today_str:
        today = history[-1]
        past = history[:-1]
    else:
        if history:
            history[-1].pop("points", None)  # keep only today's hourly points
        today = {"date": today_str, "alerts": [], "sent": [], "points": []}
        history.append(today)
        past = history[:-1]
    today.update(p)
    today["fetched_at"] = now.isoformat(timespec="seconds")
    today["source"] = source
    today.setdefault("points", []).append(
        {"t": now_ist.strftime("%H:%M"), **{k: p[k] for k in
         ("gold_24k_10g", "silver_1kg", "gold_spot_10g", "silver_spot_kg")}})
    today["points"] = today["points"][-48:]

    alerts = evaluate(cfg, past, today)
    summary = summary_text(past, today)
    print(f"{now_ist:%Y-%m-%d %H:%M} IST ({source})\n{summary}")

    for metal, kind, prio, title, msg in alerts:
        notify(topic, title, f"{msg}\n\n{summary}", prio,
               ["chart_with_downwards_trend", METALS[metal]["emoji"]], click, args.dry_run)
        today["sent"].append(f"{metal}:{kind}")
        today["alerts"].append(f"{now_ist:%H:%M} · {title}")

    hour = int(cfg.get("daily_summary_hour_ist", 12))
    if cfg.get("daily_summary", True) and now_ist.hour >= hour and "summary" not in today["sent"]:
        notify(topic, f"Gold & Silver · {now_ist:%d %b}", summary, 3,
               ["bar_chart"], click, args.dry_run)
        today["sent"].append("summary")

    hist_path.write_text(json.dumps(history[-MAX_HISTORY:], indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
