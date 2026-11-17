#!/usr/bin/env python3
"""Describe validated leaf sessions using absolute times, without cross-boot ratios.

Example:
  python3 analysis/compare_leaf_sessions.py \
    --session stock=analysis/leaf-stock --session initial=analysis/leaf \
    --session confirm=analysis/leaf-confirm --output analysis/leaf-sessions

Run analyze_leaf.py on every session first. The chart uses baseline and tail;
shadow remains in each session's paired-control report. Error bars show the
observed minimum and maximum trial times, not confidence intervals.
"""
import argparse
import csv
import json
from pathlib import Path
import statistics


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--session", action="append", required=True, metavar="LABEL=ANALYSIS_DIR")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--no-plot", action="store_true")
    args = parser.parse_args()
    datasets, names, benchmark_hash, fixtures = [], set(), None, None
    for spec in args.session:
        if "=" not in spec:
            parser.error("each --session must be LABEL=ANALYSIS_DIR")
        label, directory = spec.split("=", 1)
        if not label or label in names:
            parser.error("session labels must be nonempty and unique")
        names.add(label)
        directory = Path(directory)
        report = json.loads((directory / "leaf_summary.json").read_text())
        if report.get("status") != "ok" or report.get("validation_errors"):
            parser.error("session did not pass full per-query validation: " + label)
        metadata = report["metadata"]
        signature = {c["case"]: (c["names_sha256"], c["positive"], c["negative"])
                     for c in metadata["cases"]}
        if benchmark_hash is None:
            benchmark_hash = metadata["benchmark_sha256"]
            fixtures = signature
        if metadata["benchmark_sha256"] != benchmark_hash or signature != fixtures:
            parser.error("sessions differ in benchmark binary or name fixtures")
        if metadata["drop_caches"] != 2 or "warmdir" not in metadata["modes"]:
            parser.error("this comparison requires warmdir with drop_caches=2")
        with (directory / "leaf_trials.csv").open() as stream:
            trials = list(csv.DictReader(stream))
        if len(trials) != report["timing_trials"]:
            parser.error("trial count differs from the validated report: " + label)
        datasets.append((label, metadata, trials))
    rows = []
    for label, metadata, trials in datasets:
        for case in fixtures:
            for policy in [p[0] for p in metadata["policies"] if p[0] in ("baseline", "tail")]:
                selected = [r for r in trials if r["case"] == case and r["cache"] == "warmdir" and r["label"] == policy]
                if len(selected) != metadata["rounds"]:
                    parser.error("incomplete selected trial group")
                values = [float(r["loop_ns_per_query"]) / 1000 for r in selected]
                rows.append({"session": label, "kernel": metadata["kernel"], "case": case,
                             "cache": "warmdir", "label": policy, "rounds": len(values),
                             "median_us_per_query": statistics.median(values),
                             "min_us_per_query": min(values), "max_us_per_query": max(values)})
    args.output.mkdir(parents=True, exist_ok=True)
    with (args.output / "leaf_session_absolute.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    note = {"status": "ok", "benchmark_sha256": benchmark_hash, "summary": rows,
            "notes": ["Different sessions include different boots and different random order seeds; this is descriptive only.",
                      "No cross-session paired effect or percentage is calculated.",
                      "Primary causal policy comparisons are same-round ratios within each patched session.",
                      "Minimum and maximum describe observed trial times; they are not confidence intervals."]}
    (args.output / "leaf_session_absolute.json").write_text(json.dumps(note, indent=2) + "\n")
    if not args.no_plot:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        groups = list(dict.fromkeys((r["session"], r["label"]) for r in rows))
        cases = list(fixtures)
        fig, ax = plt.subplots(figsize=(10, 5))
        palette = ["#5c667a", "#a2abb9", "#53a7ad", "#cc9660", "#155f68"]
        for index, (session, policy) in enumerate(groups):
            chosen = [next(r for r in rows if r["case"] == case and r["session"] == session and r["label"] == policy) for case in cases]
            medians = [r["median_us_per_query"] for r in chosen]
            errors = [[r["median_us_per_query"] - r["min_us_per_query"] for r in chosen],
                      [r["max_us_per_query"] - r["median_us_per_query"] for r in chosen]]
            positions = [i + (index - (len(groups) - 1) / 2) * 0.13 for i in range(len(cases))]
            ax.errorbar(positions, medians, yerr=errors, linestyle="none", marker="o", capsize=3,
                        label=session + " / " + policy, color=palette[index % len(palette)])
        ax.set_xticks(range(len(cases)), cases)
        ax.set_ylabel("Time per query (microseconds)")
        ax.set_ylim(bottom=0)
        ax.set_title("Leaf lookup across sessions: absolute warmdir times\nMedian and observed min–max; separate boots are not paired effects")
        ax.grid(axis="y", alpha=0.2)
        ax.legend(ncol=2)
        fig.tight_layout()
        fig.savefig(args.output / "fig_leaf_sessions.png", dpi=180, bbox_inches="tight")
        plt.close(fig)
    print(json.dumps({"status": "ok", "sessions": len(datasets), "summary_rows": len(rows)}))


if __name__ == "__main__":
    main()
