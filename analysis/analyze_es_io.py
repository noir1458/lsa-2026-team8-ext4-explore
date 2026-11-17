#!/usr/bin/env python3
"""Analyze the separate 4 KiB pread controls; never pool with SEEK_DATA timing."""
import argparse
import csv
import json
from pathlib import Path
import statistics

from analyze_es import describe, write_csv


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.input.resolve()
    paths = [root] if root.is_file() else sorted(root.rglob("summary.csv"))
    sources = root.parent if root.is_file() else root
    trials, profiles, issues, coverage = [], [], [], []
    for path in paths:
        with path.open(newline="") as handle:
            raw = list(csv.DictReader(handle))
        if not raw or not {"io", "actual_profile", "query_digest", "bytes"}.issubset(raw[0]):
            continue
        dataset = str(path.parent.relative_to(sources))
        if dataset == ".":
            dataset = path.parent.name
        meta = json.loads((path.parent / "metadata.json").read_text())
        labels = ("baseline", "refresh") if meta["profile"] else ("baseline", "baseline_shadow", "refresh")
        planned = {(rep, label, pattern, io) for rep in range(meta["rounds"]) for label in labels
                   for pattern in ("hot", "random") for io in ("buffered", "direct")}
        actual = {(int(row["round"]), row["label"], row["pattern"], row["io"]) for row in raw}
        complete = planned == actual and len(raw) == len(planned)
        coverage.append({"dataset": dataset, "planned": len(planned), "observed": len(raw), "complete": complete})
        if not complete:
            issues.append({"source": dataset, "error": "incomplete or duplicate condition matrix"})
        for row in raw:
            start = len(issues)
            trial, rep = int(row["trial"]), int(row["round"])
            prefix = "trial_%04d_%s_%s_%s" % (trial, row["label"], row["pattern"], row["io"])
            record = json.loads((path.parent / (prefix + ".json")).read_text())
            source = dataset + "/" + prefix + ".json"
            def error(message):
                issues.append({"source": source, "error": message})
            query_count, elapsed = int(row["queries"]), int(row["elapsed_ns"])
            profile = int(row["profile"])
            policy = int(row["label"] == "refresh")
            if record["returncode"] != 0 or int(row["errors"]) != 0 or row["validation"] != "PASS":
                error("execution or complete-payload correctness failed")
            if query_count != meta["queries"] or int(row["bytes"]) != query_count * 4096 or elapsed <= 0:
                error("query/byte/timing fields invalid")
            if int(row["seed"]) != meta["seed"] + rep * 1000003:
                error("query seed differs from planned round")
            if int(row["policy"]) != policy or int(row["actual_policy"]) != policy:
                error("actual policy differs from condition label")
            if profile != int(meta["profile"]) or int(row["actual_profile"]) != profile:
                error("profile gate differs from trial type")
            stats = {key: int(row[key]) for key in ("lookups", "pointer_hits", "tree_nodes", "tree_hits", "tree_misses", "refreshes")}
            if profile:
                expected = query_count if row["io"] == "direct" else 0
                if stats["lookups"] != expected:
                    error("buffered/direct ES path coverage differs from expectation")
                if stats["pointer_hits"] + stats["tree_hits"] + stats["tree_misses"] != stats["lookups"]:
                    error("profile counters do not partition lookups")
                if stats["refreshes"] != (stats["tree_hits"] if policy else 0):
                    error("refresh count differs from expected tree hits")
            elif any(stats.values()):
                error("nonzero profiling counters during performance timing")
            item = {"dataset": dataset, "source": source, "label": row["label"], "pattern": row["pattern"],
                    "io": row["io"], "round": rep, "trial": trial, "kernel": row["kernel"],
                    "queries": query_count, "seed": int(row["seed"]), "query_digest": row["query_digest"],
                    "elapsed_ns": elapsed, "loop_ns_per_query": elapsed / query_count,
                    "validation": "PASS" if len(issues) == start else "FAIL"}
            if profile:
                item.update(stats, lookups_per_query=stats["lookups"] / query_count,
                            pointer_hits_per_query=stats["pointer_hits"] / query_count,
                            tree_nodes_per_query=stats["tree_nodes"] / query_count)
                profiles.append(item)
            else:
                trials.append(item)
    summaries, groups = [], {}
    for row in trials:
        if row["validation"] == "PASS":
            key = tuple(row[field] for field in ("dataset", "kernel", "pattern", "io", "label"))
            groups.setdefault(key, []).append(row)
    for key, rows in sorted(groups.items()):
        summaries.append({**dict(zip(("dataset", "kernel", "pattern", "io", "label"), key)), "trials": len(rows),
                          **describe([row["loop_ns_per_query"] for row in rows], "loop_ns_per_query")})
    pairs = []
    for row in trials:
        if row["validation"] != "PASS":
            continue
        for reference in ("baseline", "baseline_shadow"):
            if row["label"] == reference:
                continue
            bases = [base for base in trials if base["validation"] == "PASS" and base["label"] == reference and
                     all(base[key] == row[key] for key in ("dataset", "kernel", "pattern", "io", "round"))]
            if len(bases) != 1:
                issues.append({"source": row["source"], "error": "missing or ambiguous baseline pair"})
                continue
            base = bases[0]
            if any(base[key] != row[key] for key in ("queries", "seed", "query_digest")):
                issues.append({"source": row["source"], "error": "paired workloads differ"})
                continue
            ratio = row["loop_ns_per_query"] / base["loop_ns_per_query"]
            pairs.append({**{key: row[key] for key in ("dataset", "kernel", "pattern", "io", "label", "round")},
                          "reference": reference, "ratio": ratio, "time_change_percent": (ratio - 1) * 100})
    paired, groups = [], {}
    for row in pairs:
        key = tuple(row[field] for field in ("dataset", "kernel", "pattern", "io", "label", "reference"))
        groups.setdefault(key, []).append(row)
    for key, rows in sorted(groups.items()):
        values = [row["ratio"] for row in rows]
        paired.append({**dict(zip(("dataset", "kernel", "pattern", "io", "label", "reference"), key)),
                       "pairs": len(rows), **describe(values, "ratio"),
                       "median_time_change_percent": (statistics.median(values) - 1) * 100,
                       "faster_pairs": sum(value < 1 for value in values)})
    report = {"status": "no_data" if not trials and not profiles else "FAIL" if issues else "PASS",
              "timing_trials": len(trials), "profile_trials": len(profiles), "validation_errors": len(issues),
              "coverage": coverage, "issues": issues, "timing_summary": summaries, "paired_summary": paired,
              "notes": ["4KiB written-offset pread controls are separate from SEEK_DATA metadata trials.",
                        "Profiling runs are excluded from timing; payload verification occurs after timing.",
                        "Both modes have ordinary file-page-cache warming before ES reset/warming.",
                        "O_DIRECT bypasses the file page cache; device, host and hypervisor caches are uncontrolled.",
                        "Ratios are paired within rounds; min/max is not a confidence interval.",
                        "Same-policy shadow variation must be considered before interpreting small effects."]}
    args.output.mkdir(parents=True, exist_ok=True)
    for name, rows in (("trials", trials), ("profiles", profiles), ("timing_summary", summaries), ("paired_trials", pairs), ("paired_summary", paired)):
        write_csv(args.output / ("es_io_" + name + ".csv"), rows)
    (args.output / "es_io_summary.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: report[key] for key in ("status", "timing_trials", "profile_trials", "validation_errors")}))
    return int(bool(issues))


if __name__ == "__main__":
    raise SystemExit(main())
