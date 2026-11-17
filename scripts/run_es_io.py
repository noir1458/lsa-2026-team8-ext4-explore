#!/usr/bin/env python3
"""Small, separate data-read control; no metadata-only timing is pooled here."""
import argparse
import csv
import json
import platform
from pathlib import Path
import random
import subprocess

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--bench", type=Path, required=True)
parser.add_argument("--file", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--extents", type=int, default=1024)
parser.add_argument("--span", type=int, default=65536)
parser.add_argument("--queries", type=int, default=4000)
parser.add_argument("--rounds", type=int, default=8)
parser.add_argument("--seed", type=int, default=20261119)
parser.add_argument("--profile", action="store_true")
parser.add_argument("--cpus", default="0")
args = parser.parse_args()
if args.output.exists() and any(args.output.iterdir()):
    parser.error("output must be new or empty")
args.output.mkdir(parents=True, exist_ok=True)
metadata = {key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()}
metadata.update(kernel=platform.release(), setup="mode0; ordinary pread all query offsets; clear ES; SEEK_DATA every written span; select measured policy",
                scope="4KiB pread from written offsets; file page cache warmed for both modes; device/host caches uncontrolled",
                checks="all returned lengths and all payload bytes checked outside timing; profiling runs excluded from timing")
(args.output / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
labels = ["baseline", "refresh"] if args.profile else ["baseline", "baseline_shadow", "refresh"]
cells = [(label, pattern, io) for label in labels for pattern in ("hot", "random") for io in ("buffered", "direct")]
rng = random.Random(args.seed)
rows = []
for rep in range(args.rounds):
    if rep % 2 == 0:
        rng.shuffle(cells)
        order = list(cells)
    else:
        order.reverse()
    for label, pattern, io in order:
        trial = len(rows)
        prefix = args.output / ("trial_%04d_%s_%s_%s" % (trial, label, pattern, io))
        cmd = ["taskset", "-c", args.cpus, str(args.bench.resolve()), "--file", str(args.file.resolve()),
               "--extents", str(args.extents), "--span", str(args.span), "--queries", str(args.queries),
               "--seed", str(args.seed + rep * 1000003), "--pattern", pattern, "--io", io,
               "--policy", str(int(label == "refresh")), "--profile", str(int(args.profile))]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        Path(str(prefix) + ".stdout").write_text(result.stdout)
        Path(str(prefix) + ".stderr").write_text(result.stderr)
        Path(str(prefix) + ".json").write_text(json.dumps({"round": rep, "trial": trial, "label": label, "command": cmd, "returncode": result.returncode}, indent=2) + "\n")
        if result.returncode:
            raise RuntimeError("ES I/O benchmark failed: " + str(prefix))
        row = json.loads(result.stdout)
        if row["errors"] or row["queries"] != args.queries:
            raise RuntimeError("I/O query/data validation failed")
        if args.profile:
            expected = args.queries if io == "direct" else 0
            if row["lookups"] != expected:
                raise RuntimeError("Unexpected ES path coverage; inspect data before interpreting")
        row.update(round=rep, trial=trial, label=label, kernel=platform.release(), validation="PASS")
        for prior in rows:
            if (prior["round"], prior["pattern"], prior["io"]) == (rep, pattern, io):
                if any(prior[key] != row[key] for key in ("seed", "queries", "query_digest")):
                    raise RuntimeError("paired read workloads differ")
        rows.append(row)
        with (args.output / "summary.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(row))
            writer.writeheader()
            writer.writerows(rows)
        print(json.dumps(row), flush=True)
