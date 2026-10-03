#!/usr/bin/env python3
"""Build the comparison chart + markdown summary from results/*.prom.json."""
import glob
import json
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "..", "results")

ORDER = ["go", "rust", "python", "express", "nestjs", "rails", "dotnet"]
LABELS = {
    "go": "Go\n(Gin)",
    "rust": "Rust\n(Actix)",
    "python": "Python\n(FastAPI)",
    "express": "TS\n(Express)",
    "nestjs": "TS\n(NestJS)",
    "rails": "Ruby\n(Rails)",
    "dotnet": ".NET\n(minimal)",
}
COLORS = {"p50": "#8ecae6", "p95": "#219ebc", "p999": "#bc4749"}


def load():
    data = {}
    for f in glob.glob(os.path.join(RESULTS, "*.prom.json")):
        with open(f) as fh:
            d = json.load(fh)
        data[d["app"]] = d
    return [data[a] for a in ORDER if a in data]


def main():
    data = load()
    if not data:
        print("no results found")
        return
    apps = [d["app"] for d in data]
    x = range(len(apps))
    width = 0.27

    fig, axes = plt.subplots(1, 2, figsize=(15, 6), gridspec_kw={"width_ratios": [2, 1]})

    ax = axes[0]
    series = [
        ("p50", "p50_ms", "p50"),
        ("p95", "p95_ms", "p95"),
        ("p999", "p999_ms", "p99.9"),
    ]
    for i, (key, field, legend) in enumerate(series):
        vals = [max(d[field], 0.01) for d in data]
        bars = ax.bar([xi + (i - 1) * width for xi in x], vals, width,
                      label=legend, color=COLORS[key])
        for b, v in zip(bars, vals):
            ax.annotate(f"{v:g}", (b.get_x() + b.get_width() / 2, v),
                        ha="center", va="bottom", fontsize=7, rotation=0)
    ax.set_yscale("log")
    ax.set_xticks(list(x))
    ax.set_xticklabels([LABELS[a] for a in apps], fontsize=9)
    ax.set_ylabel("latency (ms, log scale)")
    ax.set_title("HTTP latency percentiles per stack — Prometheus histogram_quantile\n"
                 "200 rps constant arrival, 5 min, 1 CPU / 1Gi per app")
    ax.legend()
    ax.grid(axis="y", alpha=0.3)

    ax2 = axes[1]
    cpu = [d.get("cpu_avg_cores", 0) for d in data]
    rps = [d.get("rps", 0) for d in data]
    bars = ax2.bar(list(x), cpu, color="#57cc99", label="avg CPU cores")
    for b, v in zip(bars, cpu):
        ax2.annotate(f"{v:.2f}", (b.get_x() + b.get_width() / 2, v),
                     ha="center", va="bottom", fontsize=8)
    ax2.set_ylim(0, 1.15)
    ax2.axhline(1.0, color="#bc4749", linestyle="--", linewidth=1, label="1 core limit")
    ax2b = ax2.twinx()
    ax2b.plot(list(x), rps, "o-", color="#40916c", label="served rps")
    for xi, v in zip(x, rps):
        ax2b.annotate(f"{v:g}", (xi, v), ha="center", va="bottom", fontsize=8, color="#2d6a4f")
    ax2b.set_ylim(0, max(rps + [200]) * 1.35)
    ax2.set_xticks(list(x))
    ax2.set_xticklabels([LABELS[a] for a in apps], fontsize=9)
    ax2.set_ylabel("avg CPU cores")
    ax2b.set_ylabel("served rps")
    ax2.set_title("resource usage under load")
    lines1, labels1 = ax2.get_legend_handles_labels()
    lines2, labels2 = ax2b.get_legend_handles_labels()
    ax2.legend(lines1 + lines2, labels1 + labels2, fontsize=8, loc="upper right")
    ax2.grid(axis="y", alpha=0.3)

    fig.suptitle("webapi-language-perf-test", fontsize=14, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.95])
    out = os.path.join(RESULTS, "comparison.png")
    fig.savefig(out, dpi=150)
    print(f"chart saved: {out}")

    md = ["| stack | p50 ms | p95 ms | p99 ms | p99.9 ms | served rps | avg CPU | max mem MB |",
          "|---|---|---|---|---|---|---|---|"]
    for d in data:
        md.append(
            f"| {d['app']} | {d['p50_ms']} | {d['p95_ms']} | {d['p99_ms']} | {d['p999_ms']} "
            f"| {d['rps']} | {d.get('cpu_avg_cores')} | {d.get('mem_max_mb')} |"
        )
    print("\n".join(md))


if __name__ == "__main__":
    main()
