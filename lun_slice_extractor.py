"""Extract one byte range from a raw LUN image with progress output."""

from __future__ import annotations

import argparse
import os
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("offset", type=int)
    parser.add_argument("size", type=int)
    args = parser.parse_args()

    if args.offset < 0 or args.size <= 0:
        raise ValueError("Offset must be non-negative and size must be positive")
    if not args.source.is_file():
        raise FileNotFoundError(args.source)
    if args.source.stat().st_size < args.offset + args.size:
        raise ValueError("Selected partition range is not fully present in the LUN")

    partial = Path(str(args.destination) + ".part")
    args.destination.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    copy_buffer = 32 * 1024 * 1024
    next_progress = copy_buffer
    try:
        with args.source.open("rb") as source, partial.open("wb") as destination:
            source.seek(args.offset)
            remaining = args.size
            while remaining:
                data = source.read(min(copy_buffer, remaining))
                if not data:
                    raise EOFError("LUN ended during partition extraction")
                destination.write(data)
                written += len(data)
                remaining -= len(data)
                if written >= next_progress or not remaining:
                    print(f"EXTRACT_PROGRESS:{written}", flush=True)
                    next_progress = written + copy_buffer
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(partial, args.destination)
        print(f"EXTRACTED:{args.destination}", flush=True)
        return 0
    except Exception:
        try:
            partial.unlink()
        except OSError:
            pass
        raise


if __name__ == "__main__":
    raise SystemExit(main())
