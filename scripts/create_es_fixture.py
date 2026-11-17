#!/usr/bin/env python3
"""Create a new disposable file with equal written/hole spans; never overwrite."""
import argparse
import json
import os
from pathlib import Path


def create(path, extents, span):
    if extents <= 0 or span < 4096 or span % 4096:
        raise ValueError("positive extents and a page-aligned span are required")
    block = bytes((i % 251) + 1 for i in range(4096))
    with path.open("xb", buffering=0) as handle:
        handle.truncate(extents * span * 2)
        for i in range(extents):
            handle.seek(i * span * 2)
            for _ in range(span // 4096):
                if handle.write(block) != len(block):
                    raise OSError("short fixture write")
        os.fsync(handle.fileno())
    record = {"path": str(path), "written_spans": extents, "span_bytes": span,
              "logical_bytes": extents * span * 2, "written_bytes": extents * span,
              "layout": "written span, hole span, repeated; final span is a hole"}
    path.with_suffix(path.suffix + ".json").write_text(json.dumps(record, indent=2) + "\n")
    return record


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    parser.add_argument("--extents", type=int, default=1024)
    parser.add_argument("--span", type=int, default=65536)
    args = parser.parse_args()
    print(json.dumps(create(args.file, args.extents, args.span)))
