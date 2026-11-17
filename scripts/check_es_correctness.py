#!/usr/bin/env python3
"""Exercise concurrent readers and mapping invalidation on a disposable fixture."""
import argparse
import ctypes
import errno
import json
import os
from pathlib import Path
import subprocess

from create_es_fixture import create

PARAM = Path("/sys/module/ext4/parameters")


def seek(fd, offset, whence, expected):
    try:
        got = os.lseek(fd, offset, whence)
    except OSError as error:
        if expected is None and error.errno == errno.ENXIO:
            return
        raise
    if expected is None or got != expected:
        raise AssertionError((offset, whence, expected, got))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bench", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    path = args.directory / "es-correctness.bin"
    extents, span = 8, 16384
    create(path, extents, span)
    libc = ctypes.CDLL(None, use_errno=True)
    libc.fallocate.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_longlong, ctypes.c_longlong]
    libc.fallocate.restype = ctypes.c_int
    block = bytes((i % 251) + 1 for i in range(span))
    report = {"fixture": str(path), "tests": [], "status": "running"}
    fd = os.open(path, os.O_RDWR)

    def verify(mode, stage):
        command = [str(args.bench.resolve()), "--file", str(path.resolve()), "--extents", str(extents),
                   "--span", str(span), "--verify-only"]
        result = subprocess.run(command, text=True, capture_output=True, check=True)
        report["tests"].append({"mode": mode, "stage": stage, "result": json.loads(result.stdout)})

    try:
        (PARAM / "es_lookup_profile").write_text("0\n")
        for mode in (0, 1):
            (PARAM / "es_lookup_refresh").write_text(str(mode) + "\n")
            verify(mode, "initial_geometry")
            command = [str(args.bench.resolve()), "--file", str(path.resolve()), "--extents", str(extents),
                       "--span", str(span), "--threads", "2", "--queries", "100000",
                       "--pattern", "random", "--cache", "es-warm", "--seed", "314159"]
            result = subprocess.run(command, text=True, capture_output=True, check=True)
            report["tests"].append({"mode": mode, "stage": "two_readers_shared_inode", "result": json.loads(result.stdout)})
            target = 2 * 2 * span
            # Populate the old written mapping, then invalidate it by punching.
            seek(fd, target, os.SEEK_DATA, target)
            if libc.fallocate(fd, 0x01 | 0x02, target, span):
                raise OSError(ctypes.get_errno(), "fallocate(PUNCH_HOLE|KEEP_SIZE)")
            os.fsync(fd)
            seek(fd, target, os.SEEK_HOLE, target)
            seek(fd, target, os.SEEK_DATA, target + 2 * span)
            if os.pwrite(fd, block, target) != span:
                raise OSError("short restoration write")
            os.fsync(fd)
            seek(fd, target, os.SEEK_DATA, target)
            seek(fd, target, os.SEEK_HOLE, target + span)
            verify(mode, "punch_and_restore")
            far = (extents - 1) * 2 * span
            seek(fd, far, os.SEEK_DATA, far)
            os.ftruncate(fd, 3 * 2 * span)
            os.fsync(fd)
            seek(fd, far, os.SEEK_DATA, None)
            os.ftruncate(fd, extents * 2 * span)
            seek(fd, far, os.SEEK_DATA, None)
            seek(fd, far, os.SEEK_HOLE, far)
            for index in range(3, extents):
                if os.pwrite(fd, block, index * 2 * span) != span:
                    raise OSError("short restoration write")
            os.fsync(fd)
            verify(mode, "truncate_extend_and_restore")
        report["status"] = "PASS"
    finally:
        os.close(fd)
        (PARAM / "es_lookup_profile").write_text("0\n")
        (PARAM / "es_lookup_refresh").write_text("0\n")
        (args.directory / "es-correctness.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
