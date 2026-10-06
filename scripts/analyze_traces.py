#!/usr/bin/env python3
"""Aggregate OTel traces from Tempo into per-stack time breakdowns + charts.

Input: results/limits-v3/traces-window.json (fetched from the orchestrator PVC)
Usage: python3 scripts/analyze_traces.py [--tempo http://192.168.1.177:3200] [--samples 250]
"""
import argparse
import json
import os
import statistics
import urllib.parse
import urllib.request

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

ORDER = ["go", "rust", "bun", "express-bun", "express", "nestjs", "python", "rails", "dotnet"]
NAMES = {
    "go": "Go (Gin)", "rust": "Rust (Actix)", "bun": "Bun (native)",
    "express-bun": "Bun (Express)", "express": "TS (Express)",
    "nestjs": "TS (NestJS)", "python": "Python (FastAPI)",
    "rails": "Ruby (Rails)", "dotnet": ".NET (minimal)",
}
COLORS = {
    "go": "#0ea5e9", "rust": "#f97316", "bun": "#f43f5e", "express-bun": "#fb7185",
    "express": "#eab308", "nestjs": "#ef4444", "python": "#22c55e",
    "rails": "#a855f7", "dotnet": "#6366f1",
}
DB_SPANS = ["DB Q1 feed", "DB Q2 single post", "DB Q3 create post",
            "DB Q4a post exists", "DB Q4b insert like", "DB Q4c like count"]
DB_SHORT = {"DB Q1 feed": "Q1 feed", "DB Q2 single post": "Q2 post",
            "DB Q3 create post": "Q3 create", "DB Q4a post exists": "Q4a exists",
            "DB Q4b insert like": "Q4b insert", "DB Q4c like count": "Q4c count"}


class Tempo:
    def __init__(self, base):
        self.base = base.rstrip("/")

    def search_ids(self, service, start=None, end=None, limit=250):
        q = f'{{resource.service.name="langperf-{service}"}}'
        params = {"q": q, "limit": limit}
        qs = urllib.parse.urlencode(params)
        with urllib.request.urlopen(f"{self.base}/api/search?{qs}", timeout=60) as r:
            d = json.load(r)
        return [t["traceID"] for t in d.get("traces", [])]

    def get_trace(self, trace_id):
        with urllib.request.urlopen(f"{self.base}/api/traces/{trace_id}", timeout=60) as r:
            return json.load(r)


def parse_trace(doc):
    """Return (http_durs_ns, {db_name: dur_ns}) for one trace; skips timed-out roots."""
    http_durs, db_durs = [], {}
    for batch in doc.get("batches", []):
        for ss in batch.get("scopeSpans", []):
            for s in ss.get("spans", []):
                name = s.get("name", "")
                dur = int(s["endTimeUnixNano"]) - int(s["startTimeUnixNano"])
                if name.startswith("HTTP "):
                    if dur > 2_000_000_000:  # over the 2s budget = overqueue artifact
                        continue
                    http_durs.append(dur)
                elif name in DB_SPANS:
                    db_durs[name] = db_durs.get(name, 0) + dur
    return http_durs, db_durs


def collect(tempo, service, start, end, samples):
    ids = tempo.search_ids(service, start, end, limit=samples)
    http_durs, db_by_name, db_totals = [], {n: [] for n in DB_SPANS}, []
    for tid in ids:
        try:
            doc = tempo.get_trace(tid)
        except Exception:
            continue
        h, db = parse_trace(doc)
        if not h:
            continue
        http_durs.extend(h)
        total = 0
        for n in DB_SPANS:
            if n in db:
                db_by_name[n].append(db[n])
                total += db[n]
        db_totals.append(total)
    return http_durs, db_totals, db_by_name


def ms(ns_list, q=None):
    if not ns_list:
        return None
    vals = sorted(ns_list)
    if q:
        k = min(len(vals) - 1, max(0, int(q * len(vals))))
        return round(vals[k] / 1e6, 2)
    return round(statistics.mean(vals) / 1e6, 2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tempo", default="http://192.168.1.177:3200")
    ap.add_argument("--windows", default="results/limits-v3/traces-window.json")
    ap.add_argument("--samples", type=int, default=250)
    ap.add_argument("--out-md", default="results/traces-breakdown.md")
    ap.add_argument("--out-png", default="results/traces-breakdown.png")
    args = ap.parse_args()

    windows = json.load(open(args.windows))
    tempo = Tempo(args.tempo)
    stats = {}
    for app in ORDER:
        if app not in windows:
            continue
        w = windows[app]
        start = int(datetime_epoch(w["start"]))
        end = int(datetime_epoch(w["end"]))
        try:
            http, db_tot, db_by = collect(tempo, app, start, end, args.samples)
        except Exception as e:
            print(f"{app}: collect failed: {e}")
            continue
        if not http:
            print(f"{app}: no traces found in window")
            continue
        stats[app] = {
            "traces": len(http),
            "http_mean_ms": ms(http),
            "http_p95_ms": ms(http, 0.95),
            "db_mean_ms": ms(db_tot) or 0.0,
            "db_by_name_ms": {n: ms(db_by[n]) for n in DB_SPANS if db_by[n]},
        }
        s = stats[app]
        s["app_self_ms"] = round(max(0.0, s["http_mean_ms"] - s["db_mean_ms"]), 2)
        s["db_pct"] = round(100 * s["db_mean_ms"] / s["http_mean_ms"], 1) if s["http_mean_ms"] else 0

    lines = ["# Trace breakdown (mean per request, from OTel spans via Tempo)", "",
             "| stack | traces | HTTP mean | HTTP p95 | DB total | DB % | app self | |",
             "|---|---|---|---|---|---|---|---|"]
    for app in ORDER:
        if app not in stats:
            continue
        s = stats[app]
        lines.append(f"| {NAMES[app]} | {s['traces']} | {s['http_mean_ms']} ms | {s['http_p95_ms']} ms "
                     f"| {s['db_mean_ms']} ms | {s['db_pct']}% | {s['app_self_ms']} ms | |")
    lines += ["", "## Mean DB span time per query (ms)", "",
              "| stack | " + " | ".join(DB_SHORT[n] for n in DB_SPANS) + " |",
              "|---|" + "---|" * len(DB_SPANS)]
    for app in ORDER:
        if app not in stats:
            continue
        s = stats[app]
        lines.append(f"| {NAMES[app]} | " + " | ".join(
            str(s["db_by_name_ms"].get(n, "—")) for n in DB_SPANS) + " |")
    os.makedirs(os.path.dirname(args.out_md), exist_ok=True)
    with open(args.out_md, "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))

    # charts
    apps = [a for a in ORDER if a in stats]
    if not apps:
        return
    fig, axes = plt.subplots(1, 2, figsize=(15, 6))
    ax = axes[0]
    db = [stats[a]["db_mean_ms"] for a in apps]
    self_t = [stats[a]["app_self_ms"] for a in apps]
    y = range(len(apps))
    ax.barh(y, db, color="#219ebc", label="DB (Postgres round-trips)")
    ax.barh(y, self_t, left=db, color="#8ecae6", label="app self (compute/serialization)")
    ax.set_yticks(list(y))
    ax.set_yticklabels([NAMES[a] for a in apps], fontsize=9)
    ax.invert_yaxis()
    ax.set_xlabel("mean ms per request")
    ax.set_title("where a request's time goes (from OTel spans)")
    for i, a in enumerate(apps):
        tot = stats[a]["http_mean_ms"]
        ax.annotate(f"{tot:.2f} ms", (tot, i), xytext=(4, 0), textcoords="offset points",
                    va="center", fontsize=8)
    ax.legend(fontsize=8)
    ax.grid(axis="x", alpha=0.3)

    ax2 = axes[1]
    width = 0.13
    for i, n in enumerate(DB_SPANS):
        vals = [stats[a]["db_by_name_ms"].get(n) or 0 for a in apps]
        ax2.bar([x + (i - 2.5) * width for x in range(len(apps))], vals, width,
                label=DB_SHORT[n])
    ax2.set_xticks(range(len(apps)))
    ax2.set_xticklabels([NAMES[a].replace(" (", "\n(") for a in apps], fontsize=8)
    ax2.set_ylabel("mean DB span ms")
    ax2.set_title("per-query DB time by stack (driver comparison)")
    ax2.legend(fontsize=8)
    ax2.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(args.out_png, dpi=150)
    print(f"chart saved: {args.out_png}")


def datetime_epoch(iso):
    from datetime import datetime, timezone
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


if __name__ == "__main__":
    main()
