#!/usr/bin/env python3
"""Synthetic methodological checks; fixtures never become experiment data."""
import csv
import json
import math
from pathlib import Path
import tempfile

from analyze_es import analyze

with tempfile.TemporaryDirectory(prefix="es-analysis-check-") as temporary:
    root = Path(temporary)
    for profile in (0, 1):
        directory = root / ("profile" if profile else "timing")
        directory.mkdir()
        metadata = dict(rounds=2, labels="baseline,baseline_shadow,refresh", patterns="hot", cache_states="es-warm", queries=2, threads=1)
        (directory / "metadata.json").write_text(json.dumps(metadata))
        rows = []
        for rep in range(2):
            for index, label in enumerate(metadata["labels"].split(",")):
                trial = rep * 3 + index
                row = dict(pattern="hot", cache_state="es-warm", profile=profile, extents=8, span=65536,
                           round=rep, trial=trial, label=label, seed=7+rep, queries=2, threads=1,
                           digest="same", kernel="test", errors=0, validation="PASS",
                           elapsed_ns=[[200, 220, 160], [400, 440, 440]][rep][index])
                rows.append(row)
                stats = dict(lookups=2, pointer_hits=1, tree_nodes=3, tree_hits=1, tree_misses=0, refreshes=int(label == "refresh"))
                if not profile:
                    stats = {key: 0 for key in stats}
                record = dict(returncode=0, seed=7+rep, setup_policy=0, actual_refresh=int(label == "refresh"),
                              actual_profile=profile, profile_during_trial=profile, stats=stats)
                prefix = directory / ("trial_%04d_%s_hot_es-warm.json" % (trial, label))
                prefix.write_text(json.dumps(record))
        with (directory / "summary.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    report = analyze(root)
    assert report["status"] == "PASS", report["issues"]
    assert report["timing_trials"] == 6 and report["profile_trials"] == 6
    refresh = next(row for row in report["paired_summary"] if row["label"] == "refresh" and row["reference"] == "baseline")
    assert math.isclose(refresh["ratio_median"], .95)  # Not the ratio of unpaired timing medians (1.0).
    assert all(row["pointer_hit_rate"] == .5 and row["tree_nodes_per_query"] == 1.5 for row in report["profiles"])
    path = root / "timing/trial_0000_baseline_hot_es-warm.json"
    bad = json.loads(path.read_text())
    bad["stats"]["lookups"] = 1
    path.write_text(json.dumps(bad))
    report = analyze(root)
    assert report["status"] == "FAIL" and any(item["code"] == "profiled_timing" for item in report["issues"])
    assert next(row for row in report["timing_summary"] if row["label"] == "baseline")["trials"] == 1
    print("ES analysis checks passed: paired median, shadow references, profile exclusion, path-count normalization, nonzero timing counters rejected")
