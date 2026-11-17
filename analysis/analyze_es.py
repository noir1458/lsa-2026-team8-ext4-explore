#!/usr/bin/env python3
"""Validate ES trials and summarize paired effects without pooling sessions."""
import argparse
import csv
import json
import math
from pathlib import Path
import statistics


def boolean(value):
    if str(value).lower() in ("1", "y", "true"):
        return 1
    if str(value).lower() in ("0", "n", "false"):
        return 0
    raise ValueError("unrecognized boolean %r" % value)


def describe(values, stem):
    return {stem + "_median": statistics.median(values), stem + "_min": min(values), stem + "_max": max(values)}


def write_csv(path, rows):
    with path.open("w", newline="") as handle:
        fields = list(dict.fromkeys(key for row in rows for key in row))
        if fields:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)


def analyze(directory):
    trials, profiles, issues, coverage = [], [], [], []
    paths = [directory] if directory.is_file() else sorted(directory.rglob("summary.csv"))
    source_root = directory.parent if directory.is_file() else directory

    def issue(source, code, detail):
        issues.append({"source": source, "code": code, "detail": detail})

    for path in paths:
        with path.open(newline="") as handle:
            raw = list(csv.DictReader(handle))
        if not raw or not {"pattern", "cache_state", "profile", "extents", "span"}.issubset(raw[0]):
            continue
        dataset = str(path.parent.relative_to(source_root))
        if dataset == ".":
            dataset = path.parent.name
        metadata_path = path.parent / "metadata.json"
        metadata = json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
        planned = {(rep, label, pattern, cache)
                   for rep in range(int(metadata.get("rounds", 0)))
                   for label in metadata.get("labels", "").split(",")
                   for pattern in metadata.get("patterns", "").split(",")
                   for cache in metadata.get("cache_states", "").split(",")}
        observed = {(int(row["round"]), row["label"], row["pattern"], row["cache_state"]) for row in raw}
        complete = observed == planned and len(raw) == len(planned)
        coverage.append({"dataset": dataset, "planned": len(planned), "observed": len(raw), "complete": complete})
        if not complete:
            issue(dataset, "coverage", "planned cells missing, duplicated, or unexpected")
        seen = set()
        for row in raw:
            start = len(issues)
            rep, trial = int(row["round"]), int(row["trial"])
            cell = (rep, row["label"], row["pattern"], row["cache_state"])
            name = "trial_%04d_%s_%s_%s" % (trial, row["label"], row["pattern"], row["cache_state"])
            source = dataset + "/" + name + ".json"
            record_path = path.parent / (name + ".json")
            record = json.loads(record_path.read_text()) if record_path.exists() else {}
            if cell in seen:
                issue(source, "duplicate_cell", str(cell))
            seen.add(cell)
            queries = int(row["queries"])
            elapsed = int(row["elapsed_ns"])
            profiled = int(row["profile"])
            if queries <= 0 or elapsed <= 0:
                issue(source, "invalid_timing", "queries/elapsed must be positive")
            if int(row["errors"]) != 0 or record.get("returncode") != 0:
                issue(source, "execution_error", "nonzero errors, failed process, or missing record")
            if row.get("validation") != "PASS":
                issue(source, "runner_validation", repr(row.get("validation")))
            if queries != int(metadata.get("queries", -1)) * int(metadata.get("threads", -1)):
                issue(source, "query_count", "queries != metadata queries per thread * threads")
            if str(record.get("seed")) != row["seed"] or record.get("setup_policy") != 0:
                issue(source, "setup", "seed mismatch or setup policy not 0")
            expected_policy = {"baseline": 0, "baseline_shadow": 0, "refresh": 1}.get(row["label"])
            try:
                if expected_policy is None or boolean(record.get("actual_refresh")) != expected_policy:
                    issue(source, "policy", "actual hint policy differs from label")
                if boolean(record.get("actual_profile")) != profiled or boolean(record.get("profile_during_trial")) != profiled:
                    issue(source, "profile", "actual profiling gate differs from trial type")
            except ValueError as error:
                issue(source, "missing_policy", str(error))
            stats = record.get("stats", {})
            expected_counters = {"lookups", "pointer_hits", "tree_nodes", "tree_hits", "tree_misses", "refreshes"}
            if set(stats) != expected_counters or any(not isinstance(value, int) or value < 0 for value in stats.values()):
                issue(source, "stats_schema", "missing, unknown, or invalid profiling counters")
            elif profiled:
                if stats["lookups"] != queries:
                    issue(source, "path_coverage", "not exactly one ES lookup per SEEK_DATA query")
                if stats["pointer_hits"] + stats["tree_hits"] + stats["tree_misses"] != stats["lookups"]:
                    issue(source, "stats_partition", "pointer hits + tree hits + misses != lookups")
                if stats["refreshes"] != (stats["tree_hits"] if expected_policy else 0):
                    issue(source, "refresh_count", "unexpected hint-refresh count")
            elif any(stats.values()):
                issue(source, "profiled_timing", "nonzero counters in timing trial")
            item = {"dataset": dataset, "source": source, "label": row["label"], "round": rep, "trial": trial,
                    "pattern": row["pattern"], "cache_state": row["cache_state"], "profile": profiled,
                    "kernel": row["kernel"], "seed": int(row["seed"]), "queries": queries,
                    "written_spans": int(row["extents"]), "span_bytes": int(row["span"]),
                    "threads": int(row["threads"]), "digest": row["digest"],
                    "elapsed_ns": elapsed, "loop_ns_per_query": elapsed / queries if queries else None,
                    "errors": int(row["errors"]), "validation": "PASS" if len(issues) == start else "FAIL"}
            if profiled:
                item.update(stats)
                if queries > 0 and set(stats) == expected_counters:
                    item.update(pointer_hit_rate=stats["pointer_hits"] / queries,
                                tree_nodes_per_query=stats["tree_nodes"] / queries,
                                lookup_calls_per_query=stats["lookups"] / queries,
                                es_tree_miss_rate=stats["tree_misses"] / queries)
                profiles.append(item)
            else:
                trials.append(item)
    groups = {}
    for row in trials:
        if row["validation"] == "PASS":
            key = tuple(row[field] for field in ("dataset", "kernel", "pattern", "cache_state", "threads", "written_spans", "span_bytes", "label"))
            groups.setdefault(key, []).append(row)
    summaries = [{**dict(zip(("dataset", "kernel", "pattern", "cache_state", "threads", "written_spans", "span_bytes", "label"), key)),
                  "trials": len(rows), **describe([row["loop_ns_per_query"] for row in rows], "loop_ns_per_query")}
                 for key, rows in sorted(groups.items())]
    pairs = []
    for row in trials:
        if row["validation"] != "PASS":
            continue
        for baseline in ("baseline", "baseline_shadow"):
            if row["label"] == baseline:
                continue
            matches = [base for base in trials if base["validation"] == "PASS" and base["label"] == baseline and
                       all(base[key] == row[key] for key in ("dataset", "kernel", "pattern", "cache_state", "round", "threads"))]
            if not matches:
                continue  # Profile-only sessions or intentionally absent shadow references.
            if len(matches) != 1:
                issue(row["source"], "ambiguous_pair", baseline)
                continue
            base = matches[0]
            if any(base[key] != row[key] for key in ("seed", "queries", "digest", "written_spans", "span_bytes")):
                issue(row["source"], "workload_mismatch", baseline)
                continue
            ratio = row["loop_ns_per_query"] / base["loop_ns_per_query"]
            pairs.append({**{key: row[key] for key in ("dataset", "kernel", "pattern", "cache_state", "threads", "written_spans", "span_bytes", "round", "label")},
                          "reference": baseline, "policy_trial": row["trial"], "reference_trial": base["trial"],
                          "ratio": ratio, "time_change_percent": (ratio - 1) * 100,
                          "comparison": "same_policy_control" if row["label"].startswith("baseline") else "policy_change"})
    pair_groups = {}
    for row in pairs:
        key = tuple(row[field] for field in ("dataset", "kernel", "pattern", "cache_state", "threads", "written_spans", "span_bytes", "label", "reference", "comparison"))
        pair_groups.setdefault(key, []).append(row)
    paired = [{**dict(zip(("dataset", "kernel", "pattern", "cache_state", "threads", "written_spans", "span_bytes", "label", "reference", "comparison"), key)),
               "pairs": len(rows), **describe([row["ratio"] for row in rows], "ratio"),
               "median_time_change_percent": (statistics.median(row["ratio"] for row in rows) - 1) * 100,
               "faster_pairs": sum(row["ratio"] < 1 for row in rows)} for key, rows in sorted(pair_groups.items())]
    return {"status": "no_data" if not trials and not profiles else "FAIL" if issues else "PASS",
            "timing_trials": len(trials), "profile_trials": len(profiles), "validation_errors": len(issues),
            "coverage": coverage, "issues": issues, "trials": trials, "profiles": profiles,
            "timing_summary": summaries, "paired_trials": pairs, "paired_summary": paired,
            "notes": ["Primary timing is elapsed_ns / queries, including worker synchronization overhead.",
                      "Profile rows are never used as performance timing.",
                      "Each dataset/session stays separate; confirmation does not replace discovery.",
                      "Paired ratios require identical round, seed, query workload, and digest.",
                      "Observed min/max ranges are not confidence intervals or equivalence tests.",
                      "ES cold clears only extent-status entries, not page or device caches.",
                      "written_spans counts fixture regions; physical extent layout must be verified separately.",
                      "tree_nodes counts visited RB nodes, not CPU cycles or individual comparison operators.",
                      "The two baseline references share trials and are not independent effect replications."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = analyze(args.input.resolve())
    args.output.mkdir(parents=True, exist_ok=True)
    for key in ("trials", "profiles", "timing_summary", "paired_trials", "paired_summary", "coverage"):
        write_csv(args.output / ("es_" + key + ".csv"), report[key])
    (args.output / "es_summary.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(json.dumps({key: report[key] for key in ("status", "timing_trials", "profile_trials", "validation_errors")}))
    return int(bool(report["validation_errors"]))


if __name__ == "__main__":
    raise SystemExit(main())
