#!/usr/bin/env python3
"""Render only validated measured ES summaries; preserve separate sessions."""
import argparse
import json
from pathlib import Path
import re


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = args.input / "es_summary.json" if args.input.is_dir() else args.input
    report = json.loads(source.read_text())
    if report["status"] != "PASS" or report["validation_errors"]:
        parser.error("Measured ES data must pass validation before plotting.")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10,
                         "axes.spines.top": False, "axes.spines.right": False})
    args.output.mkdir(parents=True, exist_ok=True)
    generated = []
    colors = {"baseline_shadow": "#777777", "refresh": "#386cb0", "baseline": "#777777"}
    patterns = ["hot", "sequential", "random"]
    for dataset in sorted({row["dataset"] for row in report["paired_summary"]}):
        summary = [row for row in report["paired_summary"] if row["dataset"] == dataset and row["reference"] == "baseline"]
        raw = [row for row in report["paired_trials"] if row["dataset"] == dataset and row["reference"] == "baseline"]
        caches = [cache for cache in ("es-warm", "es-cold") if any(row["cache_state"] == cache for row in summary)]
        fig, axes = plt.subplots(1, len(caches), figsize=(5.6 * len(caches), 4.4), squeeze=False, sharey=True, constrained_layout=True)
        for ax, cache in zip(axes[0], caches):
            for index, label in enumerate(("baseline_shadow", "refresh")):
                offset = (index - .5) * .22
                for x, pattern in enumerate(patterns):
                    cells = [row for row in summary if row["cache_state"] == cache and row["pattern"] == pattern and row["label"] == label]
                    if len(cells) != 1:
                        continue
                    row = cells[0]
                    ax.errorbar(x + offset, row["ratio_median"],
                                yerr=[[row["ratio_median"] - row["ratio_min"]], [row["ratio_max"] - row["ratio_median"]]],
                                marker="D", capsize=4, color=colors[label], label=label if x == 0 else None)
                    for point in raw:
                        if point["cache_state"] == cache and point["pattern"] == pattern and point["label"] == label:
                            ax.scatter(x + offset + ((point["round"] % 7) - 3) * .009, point["ratio"], s=13, alpha=.35, color=colors[label])
            ax.axhline(1, color="#333333", linestyle="--", linewidth=1)
            ax.set_xticks(range(len(patterns)), patterns)
            ax.set_xlabel("Written-offset SEEK_DATA query pattern")
            ax.set_title(cache + " (ES state only)")
            ax.grid(axis="y", alpha=.2)
            ax.legend(frameon=False, fontsize=8)
        axes[0, 0].set_ylabel("Policy time / baseline time (same round)")
        fig.suptitle("ES lookup hint refresh — " + dataset, fontsize=12)
        fig.supxlabel("Profiling off. Dots: paired trials; diamonds: medians; whiskers: observed min–max, not confidence intervals.", fontsize=8)
        name = "fig2_es_timing_" + re.sub(r"[^A-Za-z0-9_-]", "_", dataset) + ".png"
        fig.savefig(args.output / name, dpi=180, bbox_inches="tight")
        plt.close(fig)
        generated.append(name)
    for dataset in sorted({row["dataset"] for row in report["profiles"]}):
        rows = [row for row in report["profiles"] if row["dataset"] == dataset and row["validation"] == "PASS"]
        cells = [(pattern, cache) for cache in ("es-warm", "es-cold") for pattern in patterns
                 if any(row["pattern"] == pattern and row["cache_state"] == cache for row in rows)]
        fig, axes = plt.subplots(1, 2, figsize=(11.5, 4.5), constrained_layout=True)
        for ax, metric, title in zip(axes, ("pointer_hit_rate", "tree_nodes_per_query"),
                                     ("Last-extent pointer hits / query", "RB-tree nodes visited / query")):
            for index, label in enumerate(("baseline", "refresh")):
                for x, (pattern, cache) in enumerate(cells):
                    selected = [row for row in rows if row["label"] == label and row["pattern"] == pattern and row["cache_state"] == cache]
                    if len(selected) != 1:
                        continue
                    ax.bar(x + (index - .5) * .34, selected[0][metric], width=.32, color=colors[label], label=label if x == 0 else None)
            ax.set_xticks(range(len(cells)), [pattern + "\n" + cache for pattern, cache in cells], fontsize=8)
            ax.set_title(title)
            ax.set_ylim(bottom=0)
            ax.grid(axis="y", alpha=.2)
            ax.legend(frameon=False, fontsize=8)
        fig.suptitle("Separate profiling diagnostics — " + dataset, fontsize=12)
        fig.supxlabel("Profiling times excluded. ES cold is not disk cold; visited-node counts are not CPU time or individual comparisons.", fontsize=8)
        name = "fig3_es_profile_" + re.sub(r"[^A-Za-z0-9_-]", "_", dataset) + ".png"
        fig.savefig(args.output / name, dpi=180, bbox_inches="tight")
        plt.close(fig)
        generated.append(name)
    print(json.dumps({"generated": generated}))


if __name__ == "__main__":
    main()
