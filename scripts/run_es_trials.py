#!/usr/bin/env python3
"""Paired ES-hint trials. ES cold means only ES entries are cleared, not page cache."""
import argparse
import csv
import json
import os
from pathlib import Path
import platform
import random
import select
import subprocess
import time

PARAM = Path("/sys/module/ext4/parameters")


def setting(name, value):
    (PARAM / name).write_text(str(value) + "\n")


def read_stats():
    return {key: int(value) for key, value in
            (word.split("=") for word in (PARAM / "es_lookup_stats").read_text().split())}


def wait_byte(fd, child, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if select.select([fd], [], [], .2)[0]:
            if os.read(fd, 1) == b"R":
                return
            raise RuntimeError("benchmark handshake closed early")
        if child.poll() is not None:
            raise RuntimeError("benchmark exited before handshake")
    raise TimeoutError("benchmark handshake timed out")


def run_one(args, label, pattern, cache, rep, trial):
    prefix = args.output / ("trial_%04d_%s_%s_%s" % (trial, label, pattern, cache))
    setting("es_lookup_profile", 0)
    setting("es_lookup_refresh", 0)  # Identical reset/warm-up policy for every trial.
    ready_r, ready_w = os.pipe()
    start_r, start_w = os.pipe()
    done_r, done_w = os.pipe()
    release_r, release_w = os.pipe()
    seed = args.seed + rep * 1000003
    command = [str(args.bench), "--file", str(args.file), "--extents", str(args.extents),
               "--span", str(args.span), "--queries", str(args.queries), "--threads", str(args.threads),
               "--seed", str(seed), "--pattern", pattern, "--cache", cache,
               "--ready-fd", str(ready_w), "--start-fd", str(start_r),
               "--done-fd", str(done_w), "--release-fd", str(release_r)]
    if args.cpus:
        command = ["taskset", "-c", args.cpus] + command
    record = {"round": rep, "trial": trial, "label": label, "pattern": pattern,
              "cache_state": cache, "seed": seed, "command": command,
              "setup_policy": 0, "es_reset": "EXT4_IOC_CLEAR_ES_CACHE each trial",
              "profile_during_trial": int(args.profile)}
    parent_fds = [ready_r, start_w, done_r, release_w]
    child_fds = [ready_w, start_r, done_w, release_r]
    child = None
    with Path(str(prefix) + ".stdout").open("x") as out, Path(str(prefix) + ".stderr").open("x") as err:
        try:
            child = subprocess.Popen(command, stdout=out, stderr=err, pass_fds=child_fds)
            for fd in child_fds:
                os.close(fd)
            child_fds = []
            wait_byte(ready_r, child, args.timeout)
            setting("es_lookup_refresh", int(label == "refresh"))
            setting("es_lookup_reset", 1)
            setting("es_lookup_profile", int(args.profile))
            record["actual_refresh"] = (PARAM / "es_lookup_refresh").read_text().strip()
            record["actual_profile"] = (PARAM / "es_lookup_profile").read_text().strip()
            os.write(start_w, b"G")
            wait_byte(done_r, child, args.timeout)
            setting("es_lookup_profile", 0)
            record["stats"] = read_stats()
            os.write(release_w, b"G")
            record["returncode"] = child.wait(timeout=args.timeout)
            if record["returncode"]:
                raise RuntimeError("benchmark correctness or execution failure: %s" % prefix)
        finally:
            setting("es_lookup_profile", 0)
            setting("es_lookup_refresh", 0)
            if child is not None and child.poll() is None:
                child.kill()
                child.wait()
            for fd in parent_fds + child_fds:
                os.close(fd)
            Path(str(prefix) + ".json").write_text(json.dumps(record, indent=2) + "\n")
    result = json.loads(Path(str(prefix) + ".stdout").read_text())
    if result["errors"]:
        raise RuntimeError("query correctness errors")
    stats = record["stats"]
    if args.profile:
        if stats["lookups"] != result["queries"]:
            raise RuntimeError("profile path coverage differs from exactly one ES lookup per query")
        if stats["pointer_hits"] + stats["tree_hits"] + stats["tree_misses"] != stats["lookups"]:
            raise RuntimeError("profile counter partition mismatch")
    elif any(stats.values()):
        raise RuntimeError("nonzero counters in timing trial")
    result.update(label=label, round=rep, trial=trial, profile=int(args.profile),
                  kernel=platform.release(), validation="PASS")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bench", type=Path, required=True)
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--extents", type=int, default=1024)
    parser.add_argument("--span", type=int, default=65536)
    parser.add_argument("--queries", type=int, default=200000)
    parser.add_argument("--threads", type=int, choices=[1, 2], default=1)
    parser.add_argument("--rounds", type=int, default=8)
    parser.add_argument("--seed", type=int, default=20261118)
    parser.add_argument("--patterns", default="hot,sequential,random")
    parser.add_argument("--cache-states", default="es-warm,es-cold")
    parser.add_argument("--labels", default="baseline,baseline_shadow,refresh")
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--cpus", default="0")
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()
    args.bench = args.bench.resolve()
    args.file = args.file.resolve()
    args.output = args.output.resolve()
    patterns, caches, labels = args.patterns.split(","), args.cache_states.split(","), args.labels.split(",")
    if (not set(patterns) <= {"hot", "sequential", "random"} or
            not set(caches) <= {"es-warm", "es-cold"} or
            not set(labels) <= {"baseline", "baseline_shadow", "refresh"} or
            any(len(items) != len(set(items)) for items in (patterns, caches, labels))):
        parser.error("unknown or repeated experiment condition")
    if args.rounds < 1 or args.queries < 1:
        parser.error("positive rounds and queries required")
    if args.output.exists() and any(args.output.iterdir()):
        parser.error("output must be new or empty")
    args.output.mkdir(parents=True, exist_ok=True)
    metadata = {key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()}
    metadata.update(uname=list(platform.uname()), reset="per-trial EXT4_IOC_CLEAR_ES_CACHE; setup refresh=0",
                    scope="metadata SEEK_DATA calls inside known written spans; no user file-data reads")
    (args.output / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    cells = [(label, pattern, cache) for label in labels for pattern in patterns for cache in caches]
    randomizer = random.Random(args.seed)
    results = []
    try:
        for rep in range(args.rounds):
            if rep % 2 == 0:
                randomizer.shuffle(cells)
                order = list(cells)
            else:
                order.reverse()
            for label, pattern, cache in order:
                result = run_one(args, label, pattern, cache, rep, len(results))
                for prior in results:
                    if (prior["round"], prior["pattern"], prior["cache_state"]) == (rep, pattern, cache):
                        if any(prior[key] != result[key] for key in ("seed", "queries", "digest")):
                            raise RuntimeError("paired query workload/digest mismatch")
                results.append(result)
                with (args.output / "summary.csv").open("w", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=list(result))
                    writer.writeheader()
                    writer.writerows(results)
                print(json.dumps(result), flush=True)
    finally:
        setting("es_lookup_profile", 0)
        setting("es_lookup_refresh", 0)


if __name__ == "__main__":
    main()
