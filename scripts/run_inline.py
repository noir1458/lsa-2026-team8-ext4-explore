#!/usr/bin/env python3
"""Bounded comparison of ext4's existing inline_data feature on fresh loop images.

This runner formats ONLY newly created regular files under --work-dir. It never
accepts a block device or an existing image. Run on the guest, with other disk
experiments stopped. Cold means guest cache eviction requested, not cold host
storage. The runner preserves per-trial JSON/stdout/stderr and command metadata.
"""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import select
import shutil
import subprocess
import sys
import time
import uuid

BASE_FEATURES = "none,has_journal,ext_attr,resize_inode,dir_index,filetype,extent,64bit,flex_bg,sparse_super,large_file,huge_file,dir_nlink,extra_isize,metadata_csum"
HASH_NAMESPACE = uuid.UUID("d0f96b55-a4fe-4667-9e32-19c964737bbf")


def write_json(path, value):
    with path.open("x") as out:
        json.dump(value, out, indent=2, ensure_ascii=False)
        out.write("\n")


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def run(command, log, timeout=180, accepted=(0,)):
    begin = time.monotonic_ns()
    p = subprocess.run([str(x) for x in command], stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, text=True, timeout=timeout)
    log.with_suffix(".stdout.txt").write_text(p.stdout)
    log.with_suffix(".stderr.txt").write_text(p.stderr)
    write_json(log.with_suffix(".command.json"), {"argv": [str(x) for x in command],
               "returncode": p.returncode, "elapsed_ns": time.monotonic_ns() - begin})
    if p.returncode not in accepted:
        raise RuntimeError("command failed (%d): %s; see %s" % (p.returncode, command[0], log))
    return p.stdout


def filesystem_header(text):
    data = {}
    for line in text.splitlines():
        if ":" in line:
            k, value = line.split(":", 1)
            data[k.strip()] = value.strip()
    return data


def cache_drop():
    os.sync()
    with open("/proc/sys/vm/drop_caches", "w") as f:
        f.write("3\n")


def statvfs(path):
    v = os.statvfs(path)
    return {k: getattr(v, k) for k in ("f_bsize", "f_frsize", "f_blocks", "f_bfree", "f_bavail", "f_files", "f_ffree")}


def benchmark(args, directory, mode, size, trial, round_no, label, order_seed,
              cache_mode, warmup=False, initial=0, fixture="original"):
    ready_r, ready_w = os.pipe()
    start_r, start_w = os.pipe()
    command = [str(args.binary), "--dir", str(directory), "--mode", mode,
               "--count", str(args.count), "--size", str(size), "--seed", str(order_seed),
               "--content-seed", str(args.seed), "--ready-fd", str(ready_w), "--start-fd", str(start_r)]
    if warmup:
        command.append("--warmup")
    if initial:
        command += ["--initial-size", str(initial)]
    metadata = {"round": round_no, "label": label, "cache_mode": cache_mode, "fixture": fixture,
                "mode": mode, "size": size, "initial_size": initial, "count": args.count,
                "order_seed": order_seed, "content_seed": args.seed, "cpu": args.cpu,
                "argv": command, "filesystem_before": statvfs(directory)}
    started = time.monotonic_ns()
    process = None
    try:
        with trial.with_suffix(".stdout.json").open("x") as out, trial.with_suffix(".stderr.txt").open("x") as err:
            process = subprocess.Popen(command, stdout=out, stderr=err,
                                       pass_fds=(ready_w, start_r),
                                       preexec_fn=lambda: os.sched_setaffinity(0, {args.cpu}))
            os.close(ready_w); ready_w = -1
            os.close(start_r); start_r = -1
            if not select.select([ready_r], [], [], args.timeout)[0] or os.read(ready_r, 1) != b"R":
                raise RuntimeError("benchmark did not reach ready barrier")
            if cache_mode == "cold_guest":
                cache_drop()
            metadata["cache_preparation_completed_ns"] = time.monotonic_ns()
            os.write(start_w, b"S")
            os.close(start_w); start_w = -1
            returncode = process.wait(timeout=args.timeout)
            metadata.update(returncode=returncode, wall_elapsed_ns=time.monotonic_ns() - started,
                            filesystem_after=statvfs(directory))
            if returncode:
                raise RuntimeError("benchmark failed: %s" % trial)
        result = json.loads(trial.with_suffix(".stdout.json").read_text())
        if result["state"]["validation_errors"] or result["state"]["flag_query_errors"]:
            raise RuntimeError("content or inode flag validation failed")
        if result["count"] != args.count or result["state"]["logical_bytes"] != args.count * size:
            raise RuntimeError("unexpected file count or bytes")
        if not result["syncfs_supported"] or not result["process_io_before"]["available"]:
            raise RuntimeError("benchmark lacks required Linux syncfs/process I/O support")
        if result["state"]["flag_queries_ok"] != args.count:
            raise RuntimeError("inode flag coverage is incomplete")
        if label == "default" and result["state"]["inline_files"]:
            raise RuntimeError("inline inode found on default filesystem")
        result["process_io_delta"] = {k: result["process_io_after"][k] - result["process_io_before"][k]
                                      for k in result["process_io_before"] if k != "available"}
        result["trial"] = metadata
        write_json(trial.with_suffix(".json"), result)
        return result
    finally:
        if process is not None and process.poll() is None:
            process.kill(); process.wait()
        for fd in (ready_r, ready_w, start_r, start_w):
            if fd >= 0:
                os.close(fd)
        if not trial.with_suffix(".metadata.json").exists():
            write_json(trial.with_suffix(".metadata.json"), metadata)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", required=True, type=Path, help="NEW directory for disposable regular-file images")
    parser.add_argument("--output", required=True, type=Path, help="NEW directory for raw results")
    parser.add_argument("--binary", type=Path, default=Path(__file__).resolve().parents[1] / "bench/tinyfile_bench")
    parser.add_argument("--rounds", type=int, default=8)
    parser.add_argument("--count", type=int, default=1000)
    parser.add_argument("--sizes", default="32,128,512,4096")
    parser.add_argument("--image-mib", type=int, default=64, choices=(64, 128))
    parser.add_argument("--seed", type=int, default=20261118)
    parser.add_argument("--cpu", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--expected-kernel", default="5.4.214", help="required release prefix")
    parser.add_argument("--keep-images", action="store_true")
    parser.add_argument("--plan-only", action="store_true", help="print plan without filesystem/cache changes")
    args = parser.parse_args()
    try:
        sizes = [int(x) for x in args.sizes.split(",")]
    except ValueError:
        parser.error("sizes must be comma-separated integers")
    if len(set(sizes)) != len(sizes) or 32 not in sizes or any(s <= 0 or s > 4096 for s in sizes):
        parser.error("distinct sizes in 1..4096, including 32, are required")
    if not 1 <= args.rounds <= 32 or not 1 <= args.count <= 4000 or args.timeout <= 0:
        parser.error("bounds: rounds 1..32, count 1..4000, timeout > 0")
    # Data blocks plus generous journal/metadata headroom must fit the image.
    worst_data = args.count * (sum(max(4096, s) for s in sizes) + 4096)
    if worst_data > args.image_mib * 1024 * 1024 * 0.65:
        parser.error("fixture too large for selected image; lower count or use 128 MiB")
    rng = random.Random(args.seed)
    schedule = []
    previous = None
    for r in range(args.rounds):
        if r % 2 == 0:
            labels = ["default", "inline"]; rng.shuffle(labels); previous = labels
        else:
            labels = list(reversed(previous))
        order = sizes[:]; random.Random(args.seed + r * 101).shuffle(order)
        schedule.append({"round": r, "labels": labels, "size_order": order,
                         "order_seed": args.seed + r * 1009})
    plan = {"experiment": "existing_ext4_inline_data", "rounds": args.rounds, "files_per_size": args.count,
            "sizes": sizes, "image_mib": args.image_mib, "block_size": 4096, "inode_size": 256,
            "schedule": schedule, "trial_count": args.rounds * 2 * (3 * len(sizes) + 3),
            "create_sync": "buffered writes, close all files, fsync(directory), syncfs(filesystem)",
            "grow": "warm validated source files, append deterministic bytes from 32 B to 4096 B, then same sync",
            "cold_scope": "drop_caches=3 requested in guest after process ready; host/VMM caches uncontrolled",
            "no_kernel_changes": True}
    if args.plan_only:
        print(json.dumps(plan, indent=2)); return 0
    if platform.system() != "Linux" or os.geteuid() != 0:
        parser.error("run as root inside the isolated Linux guest; --plan-only is read-only")
    if not platform.release().startswith(args.expected_kernel):
        parser.error("unexpected kernel release: " + platform.release())
    if args.cpu not in os.sched_getaffinity(0):
        parser.error("requested CPU is unavailable")
    args.binary = args.binary.resolve(strict=True)
    if not args.binary.is_file() or not os.access(args.binary, os.X_OK):
        parser.error("binary must be an executable regular file")
    for tool in ("mke2fs", "mount", "umount", "dumpe2fs", "debugfs", "e2fsck", "findmnt", "losetup"):
        if not shutil.which(tool):
            parser.error("missing command: " + tool)
    # No existing directory is recursively removed or reused. Symlinks refused.
    if args.work_dir.resolve() == args.output.resolve():
        parser.error("work-dir and output must be distinct")
    for path in (args.work_dir, args.output):
        if path.exists() or path.is_symlink():
            parser.error("destination already exists: " + str(path))
    args.work_dir.mkdir(parents=True); args.output.mkdir(parents=True)
    args.work_dir = args.work_dir.resolve(); args.output = args.output.resolve()
    plan["binary_sha256"] = sha256(args.binary)
    plan["runner_sha256"] = sha256(Path(__file__).resolve())
    plan["kernel"] = platform.release()
    plan["utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    plan["proc_version"] = Path("/proc/version").read_text()
    plan["meminfo"] = Path("/proc/meminfo").read_text()
    plan["cpu_models"] = sorted(set(line.split(":", 1)[1].strip() for line in Path("/proc/cpuinfo").read_text().splitlines() if line.startswith("model name")))
    plan["mke2fs_conf"] = Path("/etc/mke2fs.conf").read_text() if Path("/etc/mke2fs.conf").exists() else None
    write_json(args.output / "plan.json", plan)
    run(["mke2fs", "-V"], args.output / "mke2fs-version")
    records = []; paired_headers = {}; inline_at32 = []; failure = None
    mountpoint = args.work_dir / "mount"; mountpoint.mkdir()
    mounted = False
    try:
        for entry in schedule:
            r = entry["round"]
            for label in entry["labels"]:
                tag = "r%02d_%s" % (r, label)
                image = args.work_dir / (tag + ".img")
                fd = os.open(image, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                try:
                    os.ftruncate(fd, args.image_mib * 1024 * 1024)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                image_uuid = str(uuid.uuid5(HASH_NAMESPACE, "image:%d:%s" % (r, label)))
                hash_seed = str(uuid.uuid5(HASH_NAMESPACE, "hash:%d" % r))
                features = BASE_FEATURES + (",inline_data" if label == "inline" else "")
                run(["mke2fs", "-q", "-t", "ext4", "-F", "-b", "4096", "-I", "256", "-N", str(max(8192, args.count * len(sizes) + 1024)), "-m", "0",
                     "-U", image_uuid, "-O", features, "-E", "lazy_itable_init=0,lazy_journal_init=0,nodiscard,hash_seed=" + hash_seed, image], args.output / (tag + "_mkfs"))
                header = filesystem_header(run(["dumpe2fs", "-h", image], args.output / (tag + "_header")))
                paired_headers[(r, label)] = header
                if header.get("Inode size") != "256" or header.get("Block size") != "4096":
                    raise RuntimeError("unexpected inode/block size")
                if ("inline_data" in header.get("Filesystem features", "").split()) != (label == "inline"):
                    raise RuntimeError("filesystem feature mismatch")
                run(["mount", "-t", "ext4", "-o", "loop,noatime,nodiratime,data=ordered", image, mountpoint], args.output / (tag + "_mount"))
                mounted = True
                run(["findmnt", "--json", "--target", mountpoint], args.output / (tag + "_mountinfo"))
                for size in sizes:
                    (mountpoint / ("n%d_b%d" % (args.count, size))).mkdir()
                os.sync()
                for size in entry["size_order"]:
                    directory = mountpoint / ("n%d_b%d" % (args.count, size))
                    for mode, cache, warmup in (("create", "fresh", False), ("read", "cold_guest", False), ("read", "warm", True)):
                        trial = args.output / ("%s_b%d_%s_%s" % (tag, size, mode, cache))
                        result = benchmark(args, directory, mode, size, trial, r, label,
                                           entry["order_seed"] + size, cache, warmup)
                        records.append(result)
                        if label == "inline" and size == 32 and mode == "create":
                            inline_at32.append(result["state"]["inline_files"])
                directory = mountpoint / ("n%d_b32" % args.count)
                for mode, cache, warmup in (("grow", "warm", False), ("read", "cold_guest", False), ("read", "warm", True)):
                    trial = args.output / ("%s_grow32to4096_%s_%s" % (tag, mode, cache))
                    result = benchmark(args, directory, mode, 4096, trial, r, label,
                                       entry["order_seed"] + 9001, cache, warmup, 32 if mode == "grow" else 0,
                                       fixture="grown_32_to_4096")
                    records.append(result)
                run(["umount", mountpoint], args.output / (tag + "_umount")); mounted = False
                run(["debugfs", "-R", "stat /n%d_b32/file_00000000" % args.count, image], args.output / (tag + "_grown_inode"))
                run(["e2fsck", "-f", "-n", image], args.output / (tag + "_fsck"), accepted=(0,))
                if not args.keep_images:
                    image.unlink()  # only this runner's newly created regular file
            default = set(paired_headers[(r, "default")]["Filesystem features"].split())
            inline = set(paired_headers[(r, "inline")]["Filesystem features"].split())
            if inline - default != {"inline_data"} or default - inline:
                raise RuntimeError("paired filesystems differ in extra feature flags")
            for key in ("Block count", "Inode count", "Inode size", "Block size", "Directory Hash Seed"):
                if paired_headers[(r, "default")].get(key) != paired_headers[(r, "inline")].get(key):
                    raise RuntimeError("paired filesystem geometry/hash mismatch: " + key)
        if not inline_at32 or any(x != args.count for x in inline_at32):
            raise RuntimeError("32-byte files did not consistently use inline storage; inspect raw inode flags before interpreting this comparison")
        if len(records) != plan["trial_count"]:
            raise RuntimeError("trial coverage is incomplete")
        pairs = {}
        for result in records:
            t = result["trial"]
            key = (t["round"], t["fixture"], t["mode"], t["size"], t["cache_mode"])
            pairs.setdefault(key, []).append(result)
        for key, pair in pairs.items():
            if len(pair) != 2 or {r["trial"]["label"] for r in pair} != {"default", "inline"}:
                raise RuntimeError("invalid paired trial coverage: " + str(key))
            if pair[0]["state"]["content_digest_fnv64"] != pair[1]["state"]["content_digest_fnv64"] or pair[0]["order_digest_fnv64"] != pair[1]["order_digest_fnv64"]:
                raise RuntimeError("paired content/order digest mismatch: " + str(key))
    except BaseException as exc:
        failure = "%s: %s" % (type(exc).__name__, exc)
        raise
    finally:
        if mounted:
            try:
                run(["umount", mountpoint], args.output / "cleanup-umount")
            except BaseException as exc:
                failure = (failure or "") + "; cleanup failed: " + str(exc)
        # Always preserve completed observations, even when a later trial fails.
        write_json(args.output / "run-summary.json", {"status": "FAIL" if failure else "PASS", "failure": failure,
                   "expected_trials": plan["trial_count"], "completed_trials": len(records), "inline_files_at32": inline_at32,
                   "images_retained": args.keep_images or bool(failure),
                   "notes": ["st_blocks is in 512-byte units and excludes the preallocated inode table; inline files are not physically free.",
                             "Read data buffers and expected bytes are prepared before the timer. Exact byte checks and inode flag/stat collection occur after it.",
                             "Grow first validates all original 32-byte files, so growth is a warm metadata/data operation.",
                             "Process I/O counters include a small /proc observer overhead, exclude subsequent validation, and are not direct physical device I/O.",
                             "inline_data can also affect directory representation; this screens the existing feature, not only an isolated regular-file function.",
                             "128-byte eligibility is observed through inode flags, never assumed; larger files may use ordinary extent storage."]})
        columns = ["round", "label", "fixture", "mode", "cache_mode", "size", "count", "elapsed_ns", "operations_ns", "sync_ns", "ns_per_file", "logical_bytes", "st_blocks_512_total", "inline_files", "content_digest_fnv64", "order_digest_fnv64", "validation_errors"]
        with (args.output / "trials.csv").open("x", newline="") as f:
            out = csv.DictWriter(f, fieldnames=columns); out.writeheader()
            for r in records:
                row = {**r["trial"], **r, **r["state"]}
                row["fixture"] = r["trial"].get("fixture", "original")
                out.writerow({k: row.get(k, "") for k in columns})
    print(json.dumps({"status": "PASS", "trials": len(records), "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
