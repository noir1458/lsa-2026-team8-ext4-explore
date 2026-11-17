#!/usr/bin/env python3
"""Paired leaf-prefilter lookup trials using the existing lookup_bench binary.

Timing: --bench /path/lookup_bench --fixtures /mnt/leaf --names /run/leaf-names
        --output /run/leaf-time --rounds 10 --drop-caches 2 --cpu 0
Profile separately with --profile --rounds 1 --output /run/leaf-profile.
Uses baseline=0, shadow=0, tail=1. No image creation, mounting, or remote access.
The profile counts only enabled tail checks, so baseline/shadow counts are zero.
"""
import argparse
import csv
import gzip
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import re
import select
import shutil
import subprocess
import time

POLICIES = (("baseline", 0), ("shadow", 0), ("tail", 1))


def read_bool(path):
    value = path.read_text().strip().lower()
    if value not in ("0", "1", "n", "y", "false", "true"):
        raise RuntimeError("invalid boolean control: " + path.name)
    return value in ("1", "y", "true")


def write_control(path, value):
    path.write_text(str(value).strip() + "\n")


def read_stats(path):
    text = path.read_text().strip()
    match = re.fullmatch(r"candidates=(\d+) rejects=(\d+)", text)
    if not match:
        raise RuntimeError("unexpected leaf_stats format")
    result = dict(zip(("candidates", "rejects"), map(int, match.groups())))
    if result["rejects"] > result["candidates"]:
        raise RuntimeError("reject count exceeds candidate count")
    return result


def wait_byte(fd, process, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        ready, _, _ = select.select([fd], [], [], min(0.25, max(0, end - time.monotonic())))
        if ready:
            if os.read(fd, 1) == b"R":
                return
            raise RuntimeError("benchmark closed its handshake pipe")
        if process.poll() is not None:
            raise RuntimeError("benchmark exited before handshake")
    raise TimeoutError("benchmark handshake timed out")


def run_trial(args, case, cache, label, policy, rep, trial, seed):
    prefix = args.output / ("trial_%04d_%s_%s_%s" % (trial, case["case"], cache, label))
    controls = args.controls
    write_control(controls / "leaf_profile", 0)
    write_control(controls / "leaf_prefilter", policy)
    if read_bool(controls / "leaf_prefilter") != bool(policy):
        raise RuntimeError("leaf policy write/readback mismatch")
    if args.drop_caches:
        os.sync()
        Path("/proc/sys/vm/drop_caches").write_text(str(args.drop_caches) + "\n")
    ready_r, ready_w = os.pipe()
    start_r, start_w = os.pipe()
    done_r, done_w = os.pipe()
    release_r, release_w = os.pipe()
    parent_fds = [ready_r, start_w, done_r, release_w]
    child_fds = [ready_w, start_r, done_w, release_r]
    latencies = Path(str(prefix) + ".latencies.csv")
    command = [str(args.bench), "--dir", str(args.fixtures / case["directory"]),
               "--names", str(args.names / case["names_file"]), "--seed", str(seed),
               "--trial", str(trial), "--label", label, "--latencies", str(latencies),
               "--warm-dir", "--ready-fd", str(ready_w), "--start-fd", str(start_r),
               "--done-fd", str(done_w), "--release-fd", str(release_r)]
    if cache == "warm":
        command += ["--warmup", "1"]
    if args.cpu is not None:
        command = ["taskset", "-c", str(args.cpu)] + command
    record = {"trial": trial, "round": rep, "case": case["case"], "cache": cache,
              "label": label, "policy": policy, "profile": args.profile,
              "seed": seed, "drop_caches": args.drop_caches, "names_sha256": case["names_sha256"],
              "start_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    process = None
    try:
        with Path(str(prefix) + ".csv").open("x") as out, Path(str(prefix) + ".stderr").open("x") as err:
            process = subprocess.Popen(command, stdout=out, stderr=err, pass_fds=child_fds)
            for fd in child_fds:
                os.close(fd)
            child_fds = []
            wait_byte(ready_r, process, args.timeout)
            write_control(controls / "leaf_reset", 1)
            before = read_stats(controls / "leaf_stats")
            if any(before.values()):
                raise RuntimeError("leaf counter reset failed")
            write_control(controls / "leaf_profile", int(args.profile))
            if read_bool(controls / "leaf_profile") != args.profile:
                raise RuntimeError("profile switch write/readback mismatch")
            os.write(start_w, b"G")
            wait_byte(done_r, process, args.timeout)
            write_control(controls / "leaf_profile", 0)
            record["stats"] = read_stats(controls / "leaf_stats")
            if (not args.profile or policy == 0 or cache == "warm") and any(record["stats"].values()):
                raise RuntimeError("unexpected leaf counters in a control condition")
            if args.profile and policy == 1 and cache == "warmdir" and not record["stats"]["candidates"]:
                raise RuntimeError("tail profiling did not reach name candidates")
            os.write(release_w, b"G")
            record["returncode"] = process.wait(timeout=args.timeout)
            if record["returncode"]:
                raise RuntimeError("lookup_bench correctness or runtime failure")
    finally:
        write_control(controls / "leaf_profile", 0)
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        for fd in parent_fds + child_fds:
            os.close(fd)
        Path(str(prefix) + ".json").write_text(json.dumps(record, indent=2) + "\n")
    with Path(str(prefix) + ".csv").open() as f:
        rows = list(csv.DictReader(f))
    if len(rows) != 1:
        raise RuntimeError("expected exactly one measured pass")
    row = rows[0]
    if int(row["errors"]) or int(row["positive"]) != case["positive"] or int(row["negative"]) != case["negative"]:
        raise RuntimeError("incorrect lookup outcomes or query counts")
    # Compression is after timing/profile collection and before the next cache reset.
    with latencies.open("rb") as src, gzip.open(str(latencies) + ".gz", "wb", compresslevel=3) as dst:
        shutil.copyfileobj(src, dst)
    latencies.unlink()
    row.update(case=case["case"], cache=cache, round=rep, policy=policy,
               profile=int(args.profile), drop_caches=args.drop_caches,
               candidates=record["stats"]["candidates"], rejects=record["stats"]["rejects"])
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bench", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--names", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--controls", type=Path, default=Path("/sys/module/ext4/parameters"))
    parser.add_argument("--cases", help="comma-separated subset, default all manifest cases")
    parser.add_argument("--modes", default="warmdir,warm")
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--seed", type=int, default=20261118)
    parser.add_argument("--drop-caches", type=int, choices=(0, 2), default=2)
    parser.add_argument("--cpu", type=int)
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    for key in ("bench", "fixtures", "names", "output", "controls"):
        setattr(args, key, getattr(args, key).resolve())
    if args.rounds <= 0 or args.timeout <= 0 or (args.profile and args.rounds != 1):
        parser.error("positive rounds/timeout required; profiling uses --rounds 1")
    if args.output.exists():
        parser.error("output directory must be absent")
    modes = args.modes.split(",")
    if len(set(modes)) != len(modes) or any(m not in ("warmdir", "warm") for m in modes):
        parser.error("invalid cache modes")
    manifest = json.loads((args.names / "manifest.json").read_text())
    available = {c["case"]: c for c in manifest["cases"]}
    selected = args.cases.split(",") if args.cases else list(available)
    if len(set(selected)) != len(selected) or any(c not in available for c in selected):
        parser.error("invalid case selection")
    cases = [available[c] for c in selected]
    for case in cases:
        if not (args.fixtures / case["directory"]).is_dir():
            parser.error("fixture directory missing")
        content = (args.names / case["names_file"]).read_bytes()
        if hashlib.sha256(content).hexdigest() != case["names_sha256"]:
            parser.error("names file hash mismatch")
    for name in ("leaf_prefilter", "leaf_profile", "leaf_reset", "leaf_stats"):
        if not (args.controls / name).exists():
            parser.error("missing patched-kernel control: " + name)
    if read_bool(args.controls / "leaf_profile"):
        parser.error("leaf profiling already active; refusing to interrupt it")
    for name in ("htree_profile", "es_lookup_profile"):
        if (args.controls / name).exists() and read_bool(args.controls / name):
            parser.error("other experiment profiling must be off: " + name)
    if (args.controls / "htree_search").exists() and int((args.controls / "htree_search").read_text()) != 0:
        parser.error("HTree search must remain binary")
    original_policy = (args.controls / "leaf_prefilter").read_text()
    args.output.mkdir(parents=True)
    metadata = {"kernel": platform.release(), "seed": args.seed, "rounds": args.rounds,
                "profile": args.profile, "drop_caches": args.drop_caches, "cpu": args.cpu,
                "cases": cases, "modes": modes, "policies": POLICIES,
                "benchmark_sha256": hashlib.sha256(args.bench.read_bytes()).hexdigest(),
                "metric": "elapsed_ns / queries; per-call timing is a separate field",
                "cache_note": "warmdir enumerates names after drop_caches=2; warm also looks up all names once"}
    (args.output / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    rng = random.Random(args.seed)
    cells = [(c, m, label, policy) for c in cases for m in modes for label, policy in POLICIES]
    rows, checksums = [], {}
    try:
        for rep in range(args.rounds):
            if rep % 2 == 0:
                rng.shuffle(cells)
                order = list(cells)
            else:
                order.reverse()
            seed = (args.seed + rep * 1000003) % (2**64)
            for case, cache, label, policy in order:
                row = run_trial(args, case, cache, label, policy, rep, len(rows), seed)
                key = (case["case"], cache, rep)
                if checksums.setdefault(key, row["digest"]) != row["digest"]:
                    raise RuntimeError("paired semantic checksum mismatch")
                rows.append(row)
                with (args.output / "summary.csv").open("w", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=list(row))
                    writer.writeheader()
                    writer.writerows(rows)
                print(json.dumps({k: row[k] for k in ("case", "cache", "round", "label", "elapsed_ns", "errors", "candidates", "rejects")}), flush=True)
    finally:
        write_control(args.controls / "leaf_profile", 0)
        write_control(args.controls / "leaf_prefilter", original_policy)


if __name__ == "__main__":
    main()
