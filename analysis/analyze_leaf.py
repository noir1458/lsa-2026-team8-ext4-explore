#!/usr/bin/env python3
"""Validate and summarize one leaf-prefilter session without pooling sessions.

python3 analysis/analyze_leaf.py --input results/leaf-time \
    --profiles results/leaf-profile --output analysis/leaf
Use a separate --output for results/leaf-confirm. Matplotlib is needed for
fig1_leaf.png; --no-plot keeps validation and CSV/JSON analysis dependency-free.
"""
import argparse
import collections
import csv
import gzip
import hashlib
import json
from pathlib import Path
import statistics


def csv_rows(path):
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def quantile(values, percent):
    return sorted(values)[max(0, (len(values) * percent + 99) // 100 - 1)]


def write_csv(path, rows):
    with path.open("w", newline="") as f:
        if rows:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)


def validate_session(directory, expected_profile, errors):
    meta = json.loads((directory / "metadata.json").read_text())
    summary = csv_rows(directory / "summary.csv")
    expected = {(c["case"], mode, rep, label)
                for c in meta["cases"] for mode in meta["modes"]
                for rep in range(meta["rounds"]) for label, _ in meta["policies"]}
    case_map = {c["case"]: c for c in meta["cases"]}
    policy_map = dict(meta["policies"])
    seen, sequences, semantic = set(), {}, {}
    trials, kinds = [], []

    def check(condition, message):
        if not condition:
            errors.append(directory.name + ": " + message)

    check(bool(meta["profile"]) == expected_profile, "incorrect timing/profile session type")
    check(policy_map == {"baseline": 0, "shadow": 0, "tail": 1}, "unexpected policy mapping")
    check(len(summary) == len(expected), "summary row count does not match planned cells")
    for row in summary:
        identity = (row["case"], row["cache"], int(row["round"]), row["label"])
        prefix = directory / ("trial_%04d_%s_%s_%s" %
                              (int(row["trial"]), row["case"], row["cache"], row["label"]))
        tag = prefix.name
        check(identity in expected and identity not in seen, tag + " unexpected/duplicate cell")
        seen.add(identity)
        if row["case"] not in case_map or row["label"] not in policy_map:
            continue
        case = case_map[row["case"]]
        record = json.loads(Path(str(prefix) + ".json").read_text())
        raw_summary = csv_rows(Path(str(prefix) + ".csv"))
        check(len(raw_summary) == 1, tag + " raw result row count")
        if raw_summary:
            check(all(row.get(k) == v for k, v in raw_summary[0].items()), tag + " summary/raw mismatch")
        query_count = int(row["queries"])
        positive = int(row["positive"])
        negative = int(row["negative"])
        candidates, rejects = int(row["candidates"]), int(row["rejects"])
        check(record.get("returncode") == 0 and int(row["errors"]) == 0, tag + " lookup failure")
        check(query_count == positive + negative == case["positive"] + case["negative"], tag + " query count")
        check(positive == case["positive"] and negative == case["negative"], tag + " hit/miss count")
        check(int(row["pass"]) == 0 and int(row["warm_dir"]) == 1, tag + " pass/warm-dir configuration")
        check(int(row["warmups"]) == (1 if row["cache"] == "warm" else 0), tag + " warmup configuration")
        check(int(row["policy"]) == policy_map[row["label"]], tag + " wrong selected policy")
        check(record["case"] == row["case"] and record["cache"] == row["cache"] and
              record["label"] == row["label"] and record["policy"] == int(row["policy"]) and
              record["round"] == int(row["round"]) and record["trial"] == int(row["trial"]), tag + " trial identity mismatch")
        check(int(row["drop_caches"]) == record["drop_caches"] == meta["drop_caches"], tag + " cache control mismatch")
        check(bool(int(row["profile"])) == expected_profile == bool(record["profile"]), tag + " profiling state")
        check(int(row["seed"]) == record["seed"] == (meta["seed"] + int(row["round"]) * 1000003) % (2**64), tag + " seed")
        check(record["names_sha256"] == case["names_sha256"], tag + " names manifest hash")
        check(row["kernel"] == meta["kernel"], tag + " kernel mismatch")
        check(record["stats"] == {"candidates": candidates, "rejects": rejects}, tag + " counter record mismatch")
        check(0 <= rejects <= candidates, tag + " impossible profile counts")
        if not expected_profile or row["label"] != "tail" or row["cache"] == "warm":
            check(candidates == rejects == 0, tag + " nonzero control/timing counters")
        else:
            check(candidates > 0, tag + " tail profile did not enter comparison path")
        latency_file = Path(str(prefix) + ".latencies.csv.gz")
        opener = gzip.open
        if not latency_file.exists():
            latency_file = Path(str(prefix) + ".latencies.csv")
            opener = open
        latencies, by_kind, names = [], {0: [], 1: []}, set()
        sequence = hashlib.sha256()
        digest = 14695981039346656037
        with opener(latency_file, "rt", newline="") as f:
            for index, sample in enumerate(csv.DictReader(f)):
                check(int(sample["trial"]) == int(row["trial"]) and int(sample["pass"]) == 0, tag + " sample trial/pass")
                check(int(sample["index"]) == index, tag + " sample index is not sequential")
                exists = int(sample["expected_exists"])
                latency = int(sample["latency_ns"])
                check(exists in (0, 1) and latency >= 0, tag + " invalid sample")
                check(sample["name"] not in names, tag + " duplicate query name")
                names.add(sample["name"])
                sequence.update((str(exists) + " " + sample["name"] + "\n").encode("ascii"))
                digest = ((digest ^ ((1 if exists else 2) + index * 17)) * 1099511628211) & ((1 << 64) - 1)
                latencies.append(latency)
                by_kind[exists].append(latency)
        check(len(latencies) == query_count and len(by_kind[1]) == positive and len(by_kind[0]) == negative, tag + " full latency record coverage")
        check(row["digest"] == "%016x" % digest, tag + " semantic digest does not match expected outcomes")
        if not latencies:
            continue
        check(abs(statistics.mean(latencies) - float(row["mean_ns"])) <= 0.001, tag + " per-call mean mismatch")
        for field, percent in (("p50_ns", 50), ("p95_ns", 95), ("p99_ns", 99)):
            check(int(row[field]) == quantile(latencies, percent), tag + " " + field + " mismatch")
        check(int(row["min_ns"]) == min(latencies) and int(row["max_ns"]) == max(latencies), tag + " min/max mismatch")
        check(int(row["elapsed_ns"]) >= sum(latencies), tag + " loop elapsed shorter than timed calls")
        sequence_key = (row["case"], int(row["round"]))
        sequence_hash = sequence.hexdigest()
        check(sequences.setdefault(sequence_key, sequence_hash) == sequence_hash, tag + " same-round query order mismatch")
        check(semantic.setdefault(sequence_key, row["digest"]) == row["digest"], tag + " semantic digest mismatch")
        trial = {"case": row["case"], "cache": row["cache"], "round": int(row["round"]),
                 "label": row["label"], "trial": int(row["trial"]), "profile": expected_profile,
                 "queries": query_count, "loop_ns_per_query": int(row["elapsed_ns"]) / query_count,
                 "mean_syscall_ns": float(row["mean_ns"]), "p50_syscall_ns": int(row["p50_ns"]),
                 "p95_syscall_ns": int(row["p95_ns"]), "p99_syscall_ns": int(row["p99_ns"]),
                 "digest": row["digest"], "query_sequence_sha256": sequence_hash,
                 "candidates": candidates, "rejects": rejects}
        trials.append(trial)
        for exists, values in by_kind.items():
            if values:
                kinds.append({"case": row["case"], "cache": row["cache"], "round": int(row["round"]),
                              "label": row["label"], "kind": "hit" if exists else "miss", "queries": len(values),
                              "mean_ns": statistics.mean(values), "p50_ns": quantile(values, 50),
                              "p95_ns": quantile(values, 95), "p99_ns": quantile(values, 99)})
    check(seen == expected, "missing planned cells")
    check(len({r["trial"] for r in trials}) == len(trials), "duplicate trial IDs")
    check(len(list(directory.glob("trial_*.json"))) == len(expected), "trial metadata file coverage")
    return meta, trials, kinds


def paired(trials):
    cells = {(r["case"], r["cache"], r["round"], r["label"]): r for r in trials}
    pairs, groups = [], collections.defaultdict(list)
    for key, row in cells.items():
        if row["label"] == "baseline":
            continue
        baseline = cells.get(key[:-1] + ("baseline",))
        if baseline is None:
            continue
        ratio = row["loop_ns_per_query"] / baseline["loop_ns_per_query"]
        pair = {"case": row["case"], "cache": row["cache"], "round": row["round"], "label": row["label"],
                "ratio_to_baseline": ratio, "change_percent": (ratio - 1) * 100,
                "baseline_ns_per_query": baseline["loop_ns_per_query"], "policy_ns_per_query": row["loop_ns_per_query"]}
        pairs.append(pair)
        groups[(row["case"], row["cache"], row["label"])].append(ratio)
    summaries = []
    for (case, cache, label), values in sorted(groups.items()):
        summaries.append({"case": case, "cache": cache, "label": label, "pairs": len(values),
                          "median_change_percent": (statistics.median(values) - 1) * 100,
                          "min_change_percent": (min(values) - 1) * 100,
                          "max_change_percent": (max(values) - 1) * 100,
                          "faster_pairs": sum(v < 1 for v in values), "slower_pairs": sum(v > 1 for v in values),
                          "comparison": "same_policy_control" if label == "shadow" else "tail_prefilter"})
    return pairs, summaries


def plot_report(report, output):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    summaries = report["paired_summary"]
    cases = [c["case"] for c in report["metadata"]["cases"]]
    modes = report["metadata"]["modes"]
    fig, axes = plt.subplots(len(modes), 1, figsize=(10, 3.4 * len(modes)), squeeze=False)
    for ax, mode in zip(axes[:, 0], modes):
        for label, offset, color in (("shadow", -0.12, "#69758a"), ("tail", 0.12, "#167d9a")):
            selected = [next(r for r in summaries if r["case"] == case and r["cache"] == mode and r["label"] == label) for case in cases]
            median = [r["median_change_percent"] for r in selected]
            error = [[r["median_change_percent"] - r["min_change_percent"] for r in selected],
                     [r["max_change_percent"] - r["median_change_percent"] for r in selected]]
            ax.errorbar([i + offset for i in range(len(cases))], median, yerr=error, marker="o", linestyle="none", capsize=4, color=color, label=label)
        ax.axhline(0, color="#222222", linewidth=0.8)
        ax.set_xticks(range(len(cases)), cases)
        ax.set_title(mode + ": paired round ratios; bars are observed min–max")
        ax.set_ylabel("Time change vs baseline (%)")
        ax.grid(axis="y", alpha=0.2)
        ax.legend()
    fig.suptitle("Leaf tail prefilter — negative values are faster")
    fig.tight_layout()
    fig.savefig(output / "fig1_leaf.png", dpi=180, bbox_inches="tight")
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--profiles", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--no-plot", action="store_true")
    args = parser.parse_args()
    errors = []
    meta, trials, kinds = validate_session(args.input, False, errors)
    profiles = []
    if args.profiles:
        profile_meta, profiles, _ = validate_session(args.profiles, True, errors)
        if profile_meta["kernel"] != meta["kernel"] or profile_meta["benchmark_sha256"] != meta["benchmark_sha256"]:
            errors.append("profiling and timing used different kernel or benchmark")
    pairs, summary = paired(trials)
    report = {"status": "ok" if not errors else "invalid", "validation_errors": errors,
              "metadata": meta, "timing_trials": len(trials), "profile_trials": len(profiles),
              "paired_summary": summary, "profile_summary": profiles,
              "notes": ["Each invocation analyzes one session; discovery/confirmation sessions are never pooled.",
                        "Primary metric is complete loop elapsed_ns / queries, including timer and correctness overhead.",
                        "Same-round ratios use identical query order; individual queries are not independent replications.",
                        "shadow and baseline both set leaf_prefilter=0; shadow measures same-policy variability.",
                        "Min/max bars are observed ranges, not confidence intervals or equivalence tests.",
                        "Profile times are excluded from all timing comparisons.",
                        "leaf_stats only counts enabled tail checks; zero baseline/shadow counters cannot prove dcache hits."]}
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "leaf_summary.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    write_csv(args.output / "leaf_trials.csv", trials)
    write_csv(args.output / "leaf_query_kind_trials.csv", kinds)
    write_csv(args.output / "leaf_paired_trials.csv", pairs)
    write_csv(args.output / "leaf_paired_summary.csv", summary)
    write_csv(args.output / "leaf_profiles.csv", profiles)
    print(json.dumps({"status": report["status"], "timing_trials": len(trials), "profile_trials": len(profiles), "validation_errors": len(errors)}))
    if errors:
        raise SystemExit(1)
    if not args.no_plot:
        plot_report(report, args.output)


if __name__ == "__main__":
    main()
