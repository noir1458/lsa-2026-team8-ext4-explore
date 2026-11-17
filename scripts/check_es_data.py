#!/usr/bin/env python3
"""Check complete file data after mapping changes; this is not a benchmark.

Every written 4 KiB page has a distinct deterministic payload. Every hole byte
must read as zero. Buffered checks follow fsync + POSIX_FADV_DONTNEED; aligned
O_DIRECT checks independently bypass the file page cache (default required).
Only a newly created disposable fixture is modified. No concurrent writer.
"""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import platform
import struct

PARAM = Path("/sys/module/ext4/parameters")
PAGE = 4096


def payload(page_number, seed):
    identity = struct.pack("<QQ", seed, page_number)
    digest = hashlib.sha256(identity).digest()
    return identity + (digest * ((PAGE - len(identity) + len(digest) - 1) // len(digest)))[:PAGE-len(identity)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--extents", type=int, default=64)
    parser.add_argument("--span", type=int, default=16384)
    parser.add_argument("--seed", type=int, default=20261118)
    parser.add_argument("--no-direct", action="store_true", help="explicit fallback: only buffered fadvise checks")
    args = parser.parse_args()
    if not 4 <= args.extents <= 1024 or not PAGE <= args.span <= 1048576 or args.span % PAGE:
        parser.error("extents 4..1024; page-aligned span 4096..1048576")
    args.directory.mkdir(parents=True, exist_ok=True)
    path = args.directory / "es-data-correctness.bin"
    report_path = args.directory / "es-data-correctness.json"
    if report_path.exists():
        parser.error("correctness report already exists; choose a new directory")
    logical_bytes = args.extents * 2 * args.span
    original = bytearray(logical_bytes)
    for extent in range(args.extents):
        start = extent * 2 * args.span
        for relative in range(0, args.span, PAGE):
            offset = start + relative
            original[offset:offset+PAGE] = payload(offset // PAGE, args.seed)
    libc = ctypes.CDLL(None, use_errno=True)
    libc.fallocate.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_longlong, ctypes.c_longlong]
    libc.fallocate.restype = ctypes.c_int
    libc.posix_memalign.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_size_t, ctypes.c_size_t]
    libc.posix_memalign.restype = ctypes.c_int
    libc.pread.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_longlong]
    libc.pread.restype = ctypes.c_ssize_t
    libc.free.argtypes = [ctypes.c_void_p]
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    direct_fd = None
    aligned = ctypes.c_void_p()
    chunk = 65536
    report = {"fixture": path.name, "kernel": platform.release(), "written_spans": args.extents,
              "span_bytes": args.span, "logical_bytes": logical_bytes, "seed": args.seed,
              "page_payload": "unique logical-page identity plus SHA256-derived bytes",
              "profiling": 0, "direct_reads_requested": not args.no_direct,
              "tests": [], "status": "running"}

    def write_at(data, offset):
        done = 0
        while done < len(data):
            count = os.pwrite(fd, data[done:], offset + done)
            if count <= 0:
                raise OSError("short restoration write")
            done += count

    def reset_sparse():
        os.ftruncate(fd, 0)
        os.ftruncate(fd, logical_bytes)
        for extent in range(args.extents):
            offset = extent * 2 * args.span
            write_at(original[offset:offset+args.span], offset)
        os.fsync(fd)

    def compare_bytes(actual, wanted, offset, method):
        if actual != wanted:
            first = next((i for i, (a, b) in enumerate(zip(actual, wanted)) if a != b), min(len(actual), len(wanted)))
            raise AssertionError("%s data mismatch at byte %d: got %d bytes, expected %d" %
                                 (method, offset + first, len(actual), len(wanted)))

    def verify(mode, stage, expected):
        os.fsync(fd)
        if os.fstat(fd).st_size != len(expected):
            raise AssertionError("file size mismatch")
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        counts = {"buffered_bytes_checked": 0, "direct_bytes_checked": 0}
        for offset in range(0, len(expected), chunk):
            amount = min(chunk, len(expected) - offset)
            wanted = bytes(expected[offset:offset+amount])
            actual = os.pread(fd, amount, offset)
            compare_bytes(actual, wanted, offset, "buffered-after-fadvise")
            counts["buffered_bytes_checked"] += amount
            if direct_fd is not None:
                got = libc.pread(direct_fd, aligned, amount, offset)
                if got < 0:
                    raise OSError(ctypes.get_errno(), "aligned O_DIRECT pread")
                actual = ctypes.string_at(aligned, got)
                compare_bytes(actual, wanted, offset, "O_DIRECT")
                counts["direct_bytes_checked"] += amount
        report["tests"].append({"mode": mode, "stage": stage, "size_bytes": len(expected),
                                "expected_sha256": hashlib.sha256(expected).hexdigest(),
                                "fadvise_dontneed": "completed (advisory)", "errors": 0, **counts})

    try:
        (PARAM / "es_lookup_profile").write_text("0\n")
        if not args.no_direct:
            direct_fd = os.open(path, os.O_RDONLY | os.O_DIRECT)
            error = libc.posix_memalign(ctypes.byref(aligned), PAGE, chunk)
            if error:
                raise OSError(error, "posix_memalign")
        for mode in (0, 1):
            (PARAM / "es_lookup_refresh").write_text(str(mode) + "\n")
            reset_sparse()
            expected = bytearray(original)
            verify(mode, "unique_payload_and_holes", expected)
            target = (args.extents // 3) * 2 * args.span
            # Warm the exact old mapping before changing its allocation.
            if os.lseek(fd, target, os.SEEK_DATA) != target:
                raise AssertionError("written target not found")
            if libc.fallocate(fd, 0x01 | 0x02, target, args.span):
                raise OSError(ctypes.get_errno(), "punch hole")
            expected[target:target+args.span] = bytes(args.span)
            verify(mode, "punch_hole_returns_zero", expected)
            write_at(original[target:target+args.span], target)
            expected[target:target+args.span] = original[target:target+args.span]
            verify(mode, "rewritten_extent_restores_unique_bytes", expected)
            far = (args.extents - 1) * 2 * args.span
            if os.lseek(fd, far, os.SEEK_DATA) != far:
                raise AssertionError("far written target not found")
            # Cut inside a written span when it has more than one page.
            cut = (args.extents // 2) * 2 * args.span + max(PAGE, args.span // 2 // PAGE * PAGE)
            os.ftruncate(fd, cut)
            expected = expected[:cut]
            verify(mode, "truncate_preserves_prefix", expected)
            os.ftruncate(fd, logical_bytes)
            expected.extend(bytes(logical_bytes - cut))
            verify(mode, "extension_reads_zero_not_old_data", expected)
            reset_sparse()
            verify(mode, "sparse_layout_restored", original)
        report["status"] = "PASS"
    except Exception as error:
        report["status"] = "FAIL"
        report["failure"] = type(error).__name__ + ": " + str(error)
        raise
    finally:
        if aligned.value:
            libc.free(aligned)
        if direct_fd is not None:
            os.close(direct_fd)
        os.close(fd)
        (PARAM / "es_lookup_profile").write_text("0\n")
        (PARAM / "es_lookup_refresh").write_text("0\n")
        report_path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
