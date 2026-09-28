#!/usr/bin/env python3
"""Migrate the binary-size benchmark dashboard."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from nightly_migrations import add_facets_to_history, sync_dashboard_index


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worktree-root", type=Path, required=True)
    parser.add_argument("--site-root", type=Path, required=True)
    args = parser.parse_args()

    worktree_root = args.worktree_root.resolve()
    site_root = (worktree_root / args.site_root).resolve()
    if not site_root.is_relative_to(worktree_root):
        print("ERROR: --site-root escapes --worktree-root", file=sys.stderr)
        return 1

    data_js = site_root / "binary-size/data.js"
    try:
        if add_facets_to_history(data_js):
            print("Applied migration 0001_add_binary_size_facets")
        if sync_dashboard_index(data_js):
            print("Applied migration 0002_sync_binary_size_index")
    except (OSError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
