#!/usr/bin/env python3
"""Plot the separate measured 4 KiB pread controls after validation."""
import argparse
import csv
import json
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--input", type=Path, required=True, help="directory containing es_io_summary.json")
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
report = json.loads((args.input / "es_io_summary.json").read_text())
if report["status"] != "PASS" or report["validation_errors"] or not report["timing_trials"]:
    parser.error("Validated measured pread timing data are required.")
with (args.input / "es_io_paired_trials.csv").open(newline="") as handle:
    raw = list(csv.DictReader(handle))
with (args.input / "es_io_profiles.csv").open(newline="") as handle:
    profiles = list(csv.DictReader(handle))
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10,
                     "axes.spines.top": False, "axes.spines.right": False})
args.output.mkdir(parents=True, exist_ok=True)
generated = []
for dataset in sorted({row["dataset"] for row in report["paired_summary"]}):
    summary = [row for row in report["paired_summary"] if row["dataset"] == dataset and row["reference"] == "baseline"]
    fig, axes = plt.subplots(1, 2, figsize=(10.5, 4.4), sharey=True, constrained_layout=True)
    for ax, io in zip(axes, ("buffered", "direct")):
        for label_index, (label, color) in enumerate((("baseline_shadow", "#777777"), ("refresh", "#386cb0"))):
            shift = (label_index - .5) * .22
            for x, pattern in enumerate(("hot", "random")):
                matches = [row for row in summary if row["label"] == label and row["pattern"] == pattern and row["io"] == io]
                if len(matches) != 1:
                    continue
                row = matches[0]
                ax.errorbar(x + shift, row["ratio_median"],
                            yerr=[[row["ratio_median"] - row["ratio_min"]], [row["ratio_max"] - row["ratio_median"]]],
                            marker="D", color=color, capsize=4, label=label if x == 0 else None)
                for point in raw:
                    if (point["dataset"], point["label"], point["pattern"], point["io"], point["reference"]) == (dataset, label, pattern, io, "baseline"):
                        ax.scatter(x + shift + ((int(point["round"]) % 7) - 3) * .012, float(point["ratio"]), s=16, alpha=.35, color=color)
        path_counts = {float(row["lookups_per_query"]) for row in profiles if row["io"] == io and row["validation"] == "PASS"}
        suffix = "; ES lookups/query = %g" % next(iter(path_counts)) if len(path_counts) == 1 else ""
        ax.set_title(("Buffered, page cache warm" if io == "buffered" else "Aligned O_DIRECT") + suffix, fontsize=10)
        ax.axhline(1, color="#333333", linestyle="--", linewidth=1)
        ax.set_xticks([0, 1], ["hot", "random"])
        ax.set_xlabel("Written-offset read pattern")
        ax.grid(axis="y", alpha=.2)
        ax.legend(frameon=False, fontsize=8)
    axes[0].set_ylabel("Policy pread loop time / baseline (same round)")
    fig.suptitle("4 KiB data-read control — separate from SEEK_DATA metadata timing", fontsize=12)
    fig.supxlabel("Profiling off; full payload checks after timing. Medians and observed min–max. Device/host caches uncontrolled.", fontsize=8)
    name = "fig4_es_io_" + dataset.replace("/", "_") + ".png"
    fig.savefig(args.output / name, dpi=180, bbox_inches="tight")
    plt.close(fig)
    generated.append(name)
print(json.dumps({"generated": generated}))
