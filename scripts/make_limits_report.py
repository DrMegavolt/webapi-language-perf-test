#!/usr/bin/env python3
"""Aggregate results/limits/*.limit.json into results/limits-summary.md + chart."""
import glob
import json
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
LIMITS = os.path.join(HERE, "..", "results", "limits")
RESULTS = os.path.join(HERE, "..", "results")

ORDER = ["go", "rust", "bun", "express", "nestjs", "python", "rails", "dotnet"]
NAMES = {
    "go": "Go (Gin)",
    "rust": "Rust (Actix)",
    "bun": "Bun (native serve + SQL)",
    "express": "TS (Express)",
    "nestjs": "TS (NestJS)",
    "python": "Python (FastAPI)",
    "rails": "Ruby (Rails)",
    "dotnet": ".NET (minimal)",
}
COLORS = {
    "go": "#0ea5e9",
    "rust": "#f97316",
    "bun": "#f43f5e",
    "express": "#eab308",
    "nestjs": "#ef4444",
    "python": "#22c55e",
    "rails": "#a855f7",
    "dotnet": "#6366f1",
}


def load():
    out = {}
    for f in glob.glob(os.path.join(LIMITS, "*.limit.json")):
        with open(f) as fh:
            d = json.load(fh)
        out[d["app"]] = d
    return out


def chart(data, stable):
    fig, axes = plt.subplots(1, 2, figsize=(15, 6), gridspec_kw={"width_ratios": [3, 2]})

    ax = axes[0]
    for app in ORDER:
        d = data.get(app)
        if not d:
            continue
        pts = sorted([c for c in d["curve"] if c.get("p99_ms") is not None], key=lambda c: c["offered_rps"])
        if not pts:
            continue
        x = [p["offered_rps"] for p in pts]
        y = [max(p["p99_ms"], 0.3) for p in pts]
        ax.plot(x, y, "-", color=COLORS[app], alpha=0.6, linewidth=1.2, zorder=2)
        for p in pts:
            if p.get("gate_pass"):
                ax.plot(p["offered_rps"], max(p["p99_ms"], 0.3), "o", color=COLORS[app], zorder=3,
                        label=None)
            else:
                ax.plot(p["offered_rps"], max(p["p99_ms"], 0.3), "x", color=COLORS[app], markersize=9,
                        markeredgewidth=2.2, zorder=3)
        ax.plot([], [], "o-", color=COLORS[app], label=NAMES[app])
    ax.axhline(1000, color="#bc4749", linestyle="--", linewidth=1.6)
    ax.annotate("gate: p99 < 1s", (ax.get_xlim()[0], 1000), va="bottom", fontsize=9, color="#bc4749")
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("offered load (rps = users, log)")
    ax.set_ylabel("p99 latency (ms, log)")
    ax.set_title("p99 vs offered load — markers: ○ pass ✗ fail (gate: p99<1s, 0 errors, 0 drops)")
    ax.legend(fontsize=9, loc="upper left")
    ax.grid(alpha=0.3, which="both")

    ax2 = axes[1]
    apps = [a for a in ORDER if a in data]
    limits = [stable[a][0] for a in apps]
    bars = ax2.barh([NAMES[a] for a in apps][::-1], limits[::-1],
                    color=[COLORS[a] for a in apps][::-1])
    for b, v in zip(bars, limits[::-1]):
        ax2.annotate(f"{int(v):,}", (b.get_width(), b.get_y() + b.get_height() / 2),
                     va="center", ha="left", fontsize=9, xytext=(4, 0), textcoords="offset points")
    ax2.set_xlabel("max sustainable rps passing all gates (avg of 3 confirm runs at limit)")
    ax2.set_title("per-stack limit")
    ax2.set_xlim(0, max(limits + [1000]) * 1.18)
    ax2.grid(axis="x", alpha=0.3)

    fig.suptitle("webapi-language-perf-test — breaking-point search (p99<1s, 0 errors)", fontsize=14, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.94])
    out = os.path.join(RESULTS, "limits.png")
    fig.savefig(out, dpi=150)
    print(f"chart saved: {out}")


def stable_limit(d):
    """Max offered level where ALL runs passed the gate; levels above with mixed
    results are reported as unstable."""
    by_level = {}
    for c in d["curve"]:
        by_level.setdefault(c["offered_rps"], []).append(bool(c.get("gate_pass")))
    all_pass = sorted(lvl for lvl, runs in by_level.items() if all(runs))
    stable = max(all_pass) if all_pass else 0
    unstable = sorted(lvl for lvl, runs in by_level.items()
                      if lvl > stable and any(runs) and not all(runs))
    return stable, unstable


def report(data, stable):
    lines = [
        "## Breaking-point search — adaptive ladder (2k start, ×4 jumps when easy, geometric bisection on failure)",
        "",
        "Gate: p99 < 1s AND 0× 5xx AND 0 k6 failed requests AND 0 dropped iterations AND served ≥ 95% of offered.",
        "Stable limit = highest offered load where **every** run passed the gate (fresh-seeded DB per stack).",
        "",
        "| stack | stable limit (rps) | unstable above | first fail | bottleneck at failure | confirm p99 avg (ms) | confirm 5xx |",
        "|---|---|---|---|---|---|---|",
    ]
    for app in ORDER:
        d = data.get(app)
        if not d:
            continue
        st, unst = stable[app]
        cp99 = [p for p in d.get("confirm_p99_ms", []) if p is not None]
        avg99 = round(sum(cp99) / len(cp99), 1) if cp99 else "n/a"
        cerr = d.get("confirm_err", [])
        cerr_s = f"{round(100 * sum(cerr) / len(cerr), 4)}%" if cerr else "n/a"
        lines.append(
            f"| {NAMES[app]} | {int(st):,} | {(', '.join(f'{int(u):,}' for u in unst)) or '—'} "
            f"| {d.get('first_fail_rps') or '—'} "
            f"| {d.get('bottleneck_at_first_fail')} | {avg99} | {cerr_s} |"
        )
    lines += ["", "### Full search curves", ""]
    for app in ORDER:
        d = data.get(app)
        if not d:
            continue
        lines.append(f"**{NAMES[app]}** — limit {int(d['limit_rps']):,} rps")
        lines.append("")
        lines.append("| offered rps | phase | p95 ms | p99 ms | served rps | 5xx | dropped | app cpu | pg cpu | gate |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for c in sorted(d["curve"], key=lambda c: (c["offered_rps"], c.get("label", ""))):
            lines.append(
                f"| {c['offered_rps']:,} | {c.get('label')} | {c.get('p95_ms')} | {c.get('p99_ms')} "
                f"| {c.get('rps')} | {c.get('error_rate_5xx')} | {c.get('k6_dropped')} "
                f"| {c.get('cpu_avg_cores')} | {c.get('pg_cpu_cores')} | {'PASS' if c.get('gate_pass') else 'FAIL'} |"
            )
        lines.append("")
    out = os.path.join(RESULTS, "limits-summary.md")
    with open(out, "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines[:20]))
    print(f"report saved: {out}")


def main():
    data = load()
    if not data:
        print("no limit results yet")
        return
    stable = {app: stable_limit(d) for app, d in data.items()}
    chart(data, stable)
    report(data, stable)


if __name__ == "__main__":
    main()
