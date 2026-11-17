#!/usr/bin/env python3
"""Validate and summarize the existing inline_data feature experiment.

Replications are paired fresh-image rounds, never individual files. Primary
write metric includes final fsync(directory)+syncfs. Raw JSON remains intact.
"""
import argparse
import csv
import json
import math
from pathlib import Path
import statistics
import sys

KEYS = ("fixture", "mode", "cache_mode", "size")
METRICS = ("elapsed_ns_per_file", "operations_ns_per_file", "sync_ns_per_file")


def dump_csv(path, rows):
    if not rows:
        return
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)


def header(path):
    result = {}
    for line in path.read_text().splitlines():
        if ":" in line:
            k, value = line.split(":", 1); result[k.strip()] = value.strip()
    return result


def load(results):
    issues = []; rows = []
    def issue(path, message):
        issues.append({"source": str(path), "message": message})
    def read(path):
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            issue(path, str(exc)); return {}
    plan = read(results / "plan.json")
    summary = read(results / "run-summary.json")
    if not plan:
        return rows, issues, plan
    count = plan["files_per_size"]
    seed = plan.get("seed", plan["schedule"][0]["order_seed"])
    expected = set()
    for run in plan["schedule"]:
        for label in ("default", "inline"):
            for size in plan["sizes"]:
                for mode, cache in (("create", "fresh"), ("read", "cold_guest"), ("read", "warm")):
                    expected.add((run["round"], label, "original", mode, cache, size))
            for mode, cache in (("grow", "warm"), ("read", "cold_guest"), ("read", "warm")):
                expected.add((run["round"], label, "grown_32_to_4096", mode, cache, 4096))
    if summary.get("status") != "PASS":
        issue(results / "run-summary.json", "runner did not report PASS")
    if summary.get("completed_trials") != len(expected) or summary.get("expected_trials") != len(expected):
        issue(results / "run-summary.json", "runner trial count differs from the declared schedule")
    seen = set()
    for path in sorted(results.glob("r[0-9][0-9]_*.json")):
        if any(path.name.endswith(s) for s in (".stdout.json", ".metadata.json", ".command.json")):
            continue
        data = read(path)
        if not data:
            continue
        if "trial" not in data or "state" not in data:
            issue(path, "unrecognized trial JSON"); continue
        try:
            t, state = data["trial"], data["state"]
            key = (t["round"], t["label"], t["fixture"], data["mode"], t["cache_mode"], data["size"])
            errors = []
            if key in seen: errors.append("duplicate trial cell")
            if key not in expected: errors.append("unexpected trial cell")
            seen.add(key)
            if data["count"] != count or state["logical_bytes"] != count * data["size"]: errors.append("count or byte mismatch")
            if t["mode"] != data["mode"] or t["size"] != data["size"] or t["count"] != count or t["returncode"] != 0: errors.append("driver/benchmark metadata mismatch")
            if data["kernel"] != plan["kernel"]: errors.append("kernel mismatch")
            if data["content_seed"] != seed: errors.append("content seed mismatch")
            scheduled = next(r for r in plan["schedule"] if r["round"] == t["round"])
            expected_order = scheduled["order_seed"] + (9001 if t["fixture"] == "grown_32_to_4096" else data["size"])
            if data["seed"] != expected_order: errors.append("order seed mismatch")
            if state["validation_errors"] or state["flag_query_errors"] or state["flag_queries_ok"] != count: errors.append("content/flag validation failed")
            if not data["syncfs_supported"]: errors.append("syncfs unavailable")
            if not data["process_io_before"]["available"] or not data["process_io_after"]["available"]: errors.append("process I/O unavailable")
            if data["elapsed_ns"] != data["operations_ns"] + data["sync_ns"]: errors.append("timing components do not sum")
            if data["elapsed_ns"] <= 0 or data["operations_ns"] <= 0 or data["sync_ns"] < 0: errors.append("invalid timing")
            if abs(data["ns_per_file"] - data["elapsed_ns"] / count) > 0.000002: errors.append("per-file timing mismatch")
            if data["mode"] == "read" and data["sync_ns"] != 0: errors.append("read unexpectedly has sync time")
            if data["warmup"] != (data["mode"] == "read" and t["cache_mode"] == "warm"): errors.append("warmup mismatch")
            if t["label"] == "default" and state["inline_files"] != 0: errors.append("default has inline inode")
            if t["label"] == "inline" and data["size"] == 32 and state["inline_files"] != count: errors.append("32-byte inline path missing")
            if data["size"] == 4096 and state["inline_files"] != 0: errors.append("unexpected 4096-byte inline inode")
            if data["mode"] == "grow":
                before = data["before_state"]
                if data["initial_size"] != 32 or before["logical_bytes"] != count * 32 or before["validation_errors"]: errors.append("invalid growth source")
                if before["inline_files"] != (count if t["label"] == "inline" else 0): errors.append("growth source storage path mismatch")
            if errors:
                for message in errors: issue(path, message)
            row = {"source": path.name, "round": t["round"], "label": t["label"],
                   **{k: t[k] if k in t else data[k] for k in KEYS}, "count": count,
                   "validation": "FAIL" if errors else "PASS",
                   **{name: data[name.replace("_per_file", "")] / count for name in METRICS},
                   "logical_bytes": state["logical_bytes"], "st_blocks_512_total": state["st_blocks_512_total"],
                   "stat_reported_bytes_per_file": state["st_blocks_512_total"] * 512 / count,
                   "inline_files": state["inline_files"], "inline_fraction": state["inline_files"] / count,
                   "content_digest_fnv64": state["content_digest_fnv64"], "order_digest_fnv64": data["order_digest_fnv64"]}
            before, after = t["filesystem_before"], t["filesystem_after"]
            if before["f_frsize"] != after["f_frsize"] or before["f_blocks"] != after["f_blocks"]:
                issue(path, "filesystem geometry changed during trial")
            row["filesystem_consumed_bytes_per_file"] = (before["f_bfree"] - after["f_bfree"]) * before["f_frsize"] / count
            for name in ("read_bytes", "write_bytes", "rchar", "wchar", "syscr", "syscw", "cancelled_write_bytes"):
                delta = data["process_io_after"][name] - data["process_io_before"][name]
                row["io_" + name] = delta
                if delta < 0: issue(path, "negative process I/O counter delta: " + name)
            rows.append(row)
        except (KeyError, TypeError, ValueError, StopIteration, ZeroDivisionError) as exc:
            issue(path, "invalid trial schema: " + str(exc))
    for key in sorted(expected - seen): issue(results, "missing trial cell: " + repr(key))
    # Independently recheck filesystem feature/geometry parity and completed fsck.
    for r in range(plan["rounds"]):
        headers = {}
        for label in ("default", "inline"):
            path = results / ("r%02d_%s_header.stdout.txt" % (r, label))
            try:
                headers[label] = header(path)
            except OSError as exc: issue(path, str(exc))
            fsck = read(results / ("r%02d_%s_fsck.command.json" % (r, label)))
            if fsck.get("returncode") != 0: issue(results, "fsck was not clean: round %d %s" % (r, label))
        if len(headers) == 2:
            a, b = headers["default"], headers["inline"]
            af, bf = set(a.get("Filesystem features", "").split()), set(b.get("Filesystem features", "").split())
            if bf - af != {"inline_data"} or af - bf: issue(results, "feature sets differ by more than inline_data")
            for name in ("Block count", "Inode count", "Inode size", "Block size", "Directory Hash Seed"):
                if not a.get(name) or a.get(name) != b.get(name): issue(results, "filesystem geometry/hash mismatch: " + name)
    if len(rows) != len(expected): issue(results, "raw trial count differs from expected coverage")
    return rows, issues, plan


def summarize(rows):
    groups = {}; pair_groups = {}; summaries = []; pairs = []; paired_summary = []; issues = []
    for row in rows:
        group = tuple(row[k] for k in KEYS)
        groups.setdefault(group + (row["label"],), []).append(row)
        pair_groups.setdefault(group + (row["round"],), []).append(row)
    for key, values in sorted(groups.items()):
        row = dict(zip(KEYS + ("label",), key)); row["rounds"] = len(values)
        for metric in METRICS + ("stat_reported_bytes_per_file", "filesystem_consumed_bytes_per_file", "inline_fraction"):
            ns = [v[metric] for v in values]
            row[metric + "_median"] = statistics.median(ns)
            row[metric + "_min"] = min(ns); row[metric + "_max"] = max(ns)
        summaries.append(row)
    for key, values in sorted(pair_groups.items()):
        if len(values) != 2 or {v["label"] for v in values} != {"default", "inline"}:
            issues.append({"source": "paired", "message": "invalid paired cell " + repr(key)}); continue
        default = next(v for v in values if v["label"] == "default")
        inline = next(v for v in values if v["label"] == "inline")
        for digest in ("content_digest_fnv64", "order_digest_fnv64"):
            if default[digest] != inline[digest]: issues.append({"source": "paired", "message": digest + " mismatch " + repr(key)})
        for metric in METRICS:
            if metric == "sync_ns_per_file" and key[1] == "read": continue
            if default[metric] <= 0:
                issues.append({"source": "paired", "message": "nonpositive denominator " + repr(key)}); continue
            ratio = inline[metric] / default[metric]
            pairs.append({**dict(zip(KEYS + ("round",), key)), "metric": metric,
                          "default": default[metric], "inline": inline[metric], "inline_over_default": ratio,
                          "change_percent": (ratio - 1) * 100, "faster": int(ratio < 1)})
    groups = {}
    for row in pairs: groups.setdefault(tuple(row[k] for k in KEYS) + (row["metric"],), []).append(row)
    for key, values in sorted(groups.items()):
        ratios = [v["inline_over_default"] for v in values]; median = statistics.median(ratios)
        paired_summary.append({**dict(zip(KEYS + ("metric",), key)), "pairs": len(values),
                               "inline_over_default_median": median, "median_change_percent": (median - 1) * 100,
                               "min_change_percent": (min(ratios) - 1) * 100, "max_change_percent": (max(ratios) - 1) * 100,
                               "faster_pairs": sum(v["faster"] for v in values)})
    return summaries, pairs, paired_summary, issues


def plot(rows, output, rounds):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    sizes = sorted({r["size"] for r in rows if r["fixture"] == "original"})
    colors = {"default": "#64748B", "inline": "#2563EB"}
    fig, axes = plt.subplots(2, 2, figsize=(12, 8.4))
    def values(label, size, mode, cache, metric, fixture="original"):
        return [r[metric] for r in rows if r["label"] == label and r["size"] == size and r["mode"] == mode and r["cache_mode"] == cache and r["fixture"] == fixture]
    x = np.arange(len(sizes))
    for j, label in enumerate(("default", "inline")):
        ys = [statistics.median(values(label, size, "create", "fresh", "stat_reported_bytes_per_file")) for size in sizes]
        axes[0, 0].bar(x + (j - .5) * .34, ys, .34, label=label, color=colors[label])
        ys = [values(label, size, "create", "fresh", "elapsed_ns_per_file") for size in sizes]
        med = np.array([statistics.median(y) for y in ys]) / 1000
        lo = med - np.array([min(y) for y in ys]) / 1000; hi = np.array([max(y) for y in ys]) / 1000 - med
        axes[0, 1].errorbar(x + (j - .5) * .08, med, yerr=[lo, hi], label=label, color=colors[label], marker="o", capsize=3)
        for cache, style in (("cold_guest", "-"), ("warm", "--")):
            med = [statistics.median(values(label, size, "read", cache, "elapsed_ns_per_file")) / 1000 for size in sizes]
            axes[1, 0].plot(x, med, style, label=label + " " + cache, color=colors[label], marker="o")
        metrics = ["operations_ns_per_file", "sync_ns_per_file", "elapsed_ns_per_file"]
        ys = [values(label, 4096, "grow", "warm", m, "grown_32_to_4096") for m in metrics]
        med = np.array([statistics.median(y) for y in ys]) / 1000
        lo = med - np.array([min(y) for y in ys]) / 1000; hi = np.array([max(y) for y in ys]) / 1000 - med
        axes[1, 1].bar(np.arange(3) + (j - .5) * .34, med, .34, yerr=[lo, hi], capsize=3, label=label, color=colors[label])
    axes[0, 0].set(title="stat-reported file blocks", ylabel="st_blocks × 512 / files (bytes)")
    axes[0, 1].set(title="Create including final sync", ylabel="µs/file")
    axes[1, 0].set(title="Open, read and close", ylabel="µs/file")
    axes[1, 1].set(title="Warm growth: 32 to 4096 bytes", ylabel="µs/file")
    for ax in (axes[0, 0], axes[0, 1], axes[1, 0]): ax.set_xticks(x, [str(s) for s in sizes]); ax.set_xlabel("File size (bytes)")
    axes[1, 1].set_xticks(range(3), ["File operations", "Final sync", "Total"])
    for ax in axes.flat:
        ax.set_ylim(bottom=0); ax.grid(axis="y", alpha=.2); ax.set_axisbelow(True); ax.legend(fontsize=8)
    fig.suptitle("Existing ext4 inline_data feature: %d paired fresh-image rounds" % rounds, fontsize=14)
    fig.text(.02, .028, "Medians; error bars are observed min–max, not confidence intervals. Guest cold cache only.", fontsize=9)
    fig.text(.02, .010, "Inline stat includes a synthetic 512-byte sector; st_blocks excludes inode tables and is not physical space usage.", fontsize=9)
    fig.tight_layout(rect=(0, .055, 1, .96))
    fig.savefig(output / "fig3_inline.png", dpi=180)
    fig.savefig(output / "fig3_inline.pdf")
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parents[1]
    parser.add_argument("--results", type=Path, default=root / "results/inline")
    parser.add_argument("--output", type=Path, default=root / "analysis")
    parser.add_argument("--no-plot", action="store_true")
    args = parser.parse_args(); args.output.mkdir(parents=True, exist_ok=True)
    rows, issues, plan = load(args.results)
    summaries, pairs, paired, pair_issues = summarize(rows); issues += pair_issues
    report = {"status": "no_data" if not rows else "FAIL" if issues else "PASS", "trials": len(rows),
              "expected_trials": plan.get("trial_count"), "validation_errors": len(issues), "issues": issues,
              "summary": summaries, "paired_summary": paired,
              "notes": ["Fresh-image round is the replication unit; do not count files as independent repetitions.",
                        "Write primary metric includes final directory fsync and filesystem syncfs. Separate component medians need not sum to the total median.",
                        "Paired percentage is median(inline/default) - 1; not the ratio of independently computed medians.",
                        "Read metric includes open/read/EOF/close; byte validation, flag/stat queries and buffer generation are outside the timer.",
                        "Guest cold cache does not establish cold host/VMM/device caches.",
                        "Linux 5.4.214 ext4_file_getattr adds ceil(size/512) to stat blocks for inline files. Thus 32/128-byte inline st_blocks=1 is a synthetic sector, not a separately allocated data block. st_blocks also omits the preallocated inode table.",
                        "filesystem_consumed_bytes_per_file is the before/after decrease in statvfs free blocks, including directory expansion. It excludes inode tables already allocated at mkfs and is not the total physical footprint per file.",
                        "This compares an existing feature, which may affect directory representation too. It is not a new kernel optimization.",
                        "Observed min/max ranges are not confidence intervals. All planned valid rounds are retained."]}
    (args.output / "inline_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    dump_csv(args.output / "inline_trials.csv", rows)
    dump_csv(args.output / "inline_summary.csv", summaries)
    dump_csv(args.output / "inline_paired_trials.csv", pairs)
    dump_csv(args.output / "inline_paired_summary.csv", paired)
    if not issues and rows and not args.no_plot: plot(rows, args.output, plan["rounds"])
    print(json.dumps({"status": report["status"], "trials": len(rows), "errors": len(issues)}))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
