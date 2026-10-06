"""Explicit, bounded semantic-index backfill for the existing SQLite memory."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from friday.memory import MemoryManager  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=32, help="active rows per bounded scan (1-256)")
    args = parser.parse_args()
    memory = MemoryManager()
    try:
        if not memory.semantic_index.semantic_enabled:
            print("Semantic backfill unavailable: install requirements-memory.txt and the local model.")
            return 2

        def report(progress: dict[str, int]) -> None:
            # Progress contains counts only; private memory content is never printed.
            print(" ".join(f"{key}={value}" for key, value in sorted(progress.items())), flush=True)

        result = memory.backfill_semantic_index(batch_size=args.batch_size, progress=report)
        print(" ".join(f"{key}={value}" for key, value in sorted(result.items())))
        return 0 if result["failed"] == 0 and result["pending"] == 0 else 1
    finally:
        memory.close()


if __name__ == "__main__":
    raise SystemExit(main())
