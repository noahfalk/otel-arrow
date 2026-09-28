#!/usr/bin/env python3
"""Migrate persisted nightly benchmark dashboard data."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from collections.abc import Callable
from pathlib import Path


Migration = tuple[str, Callable[[Path, Path], bool]]
NIGHTLY_INDEX_TEMPLATE = Path(__file__).with_name("nightly") / "index.html"
DATA_PREFIX = "window.BENCHMARK_DATA = "


def _dashboard_index_template() -> bytes:
    candidates = (
        Path(__file__).with_name("default_index.html"),
        Path(__file__).parent.parent / "default_index.html",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.read_bytes()
    raise RuntimeError("Cannot locate the canonical benchmark index.html template")


def _read_history(path: Path) -> dict:
    content = path.read_text(encoding="utf-8")
    if not content.startswith(DATA_PREFIX):
        raise RuntimeError(f"History does not start with {DATA_PREFIX!r}: {path}")
    try:
        data = json.loads(content[len(DATA_PREFIX) :].rstrip().removesuffix(";"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"History contains invalid JSON: {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
        raise RuntimeError(f"History does not contain an entries object: {path}")
    return data


def _write_history(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        DATA_PREFIX + json.dumps(data, indent=2, ensure_ascii=True),
        encoding="utf-8",
    )


def move_legacy_passthrough(worktree_root: Path, site_root: Path) -> bool:
    source = worktree_root / "docs/benchmarks/continuous-passthrough"
    destination = site_root / "passthrough"
    if not source.exists():
        return False
    if destination.exists():
        raise RuntimeError(
            f"Cannot move {source}: destination already exists: {destination}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(destination))
    return True


def rename_nightly_passthrough(_worktree_root: Path, site_root: Path) -> bool:
    source = site_root / "continuous-passthrough"
    destination = site_root / "passthrough"
    if not source.exists():
        return False
    if destination.exists():
        raise RuntimeError(
            f"Cannot move {source}: destination already exists: {destination}"
        )
    shutil.move(str(source), str(destination))
    return True


def merge_split_clickhouse_dashboards(
    _worktree_root: Path,
    site_root: Path,
) -> bool:
    sources = [
        site_root / "clickhouse-throughput",
        site_root / "clickhouse-resources",
    ]
    existing = [path for path in sources if path.exists()]
    if not existing:
        return False
    for path in existing:
        if not path.is_dir():
            raise RuntimeError(
                f"Cannot migrate split ClickHouse dashboard: "
                f"path is not a directory: {path}"
            )

    destination = site_root / "clickhouse"
    destination_data = destination / "data.js"
    if destination.exists() and not destination.is_dir():
        raise RuntimeError(
            f"Cannot migrate split ClickHouse dashboard: "
            f"destination is not a directory: {destination}"
        )
    if not destination_data.is_file():
        histories = [
            _read_history(path / "data.js")
            for path in sources
            if (path / "data.js").is_file()
        ]
        if not histories:
            raise RuntimeError("Split ClickHouse dashboards contain no data.js history")

        merged_entries: dict[str, list[dict]] = {}
        merged_runs: dict[str, dict[tuple[str | None, int], dict]] = {}
        for history in histories:
            for entry_name, entries in history["entries"].items():
                if not isinstance(entries, list):
                    raise RuntimeError(
                        f"ClickHouse history entry {entry_name!r} is not a list"
                    )
                merged = merged_entries.setdefault(entry_name, [])
                by_run = merged_runs.setdefault(entry_name, {})
                occurrences: dict[str | None, int] = {}
                for entry in entries:
                    commit_id = entry.get("commit", {}).get("id")
                    ordinal = occurrences.get(commit_id, 0)
                    occurrences[commit_id] = ordinal + 1
                    run_key = (commit_id, ordinal)
                    current = by_run.get(run_key)
                    if current is None:
                        copied = dict(entry)
                        copied["benches"] = list(entry.get("benches", []))
                        merged.append(copied)
                        by_run[run_key] = copied
                    else:
                        current.setdefault("benches", []).extend(
                            entry.get("benches", [])
                        )

        combined = {
            "lastUpdate": max(
                history.get("lastUpdate", 0)
                for history in histories
            ),
            "repoUrl": histories[0].get("repoUrl", ""),
            "entries": merged_entries,
        }
        _write_history(destination_data, combined)

    for path in existing:
        shutil.rmtree(path)
    return True


def _legacy_facets(benchmark_name: str, bench: dict) -> dict[str, str]:
    facets = dict(bench.get("facets", {}))
    if facets.get("cores") == "aggregate":
        del facets["cores"]
    facets.setdefault("os", "linux")
    extra = bench.get("extra")
    suite = None
    scenario = None
    if isinstance(extra, str) and "/" in extra:
        suite, remainder = extra.split("/", 1)
        if " - " in remainder:
            scenario, _description = remainder.rsplit(" - ", 1)
            if suite and scenario:
                facets.setdefault("suite", suite)
                facets.setdefault("scenario", scenario)
                signal_match = re.match(r"^(Logs|Metrics|Traces)(?:-|$)", scenario)
                if signal_match:
                    facets.setdefault("signal", signal_match.group(1).lower())

    name = bench.get("name", "")
    binary_match = re.match(
        r"^(linux|windows)-(amd64|arm64)-(binary-size|text-size|crate-(.+))$",
        name,
    )
    if binary_match:
        facets.setdefault("os", binary_match.group(1))
        facets.setdefault("architecture", binary_match.group(2))
        measurement = binary_match.group(3)
        if measurement.startswith("crate-"):
            facets.setdefault("measurement", "crate")
            facets.setdefault("crate", binary_match.group(4))
        else:
            facets.setdefault("measurement", measurement)
    protocol_match = re.match(r"^(otap|otlp)_", name)
    if protocol_match:
        facets.setdefault("protocol", protocol_match.group(1))
    cores_match = re.search(
        r"(\d+)\s*core(?:s|\(s\))?",
        " ".join(
            value
            for value in (name, suite, scenario, extra)
            if isinstance(value, str)
        ),
        re.IGNORECASE,
    )
    if cores_match:
        facets.setdefault("cores", cores_match.group(1))
    if "engine" not in facets and benchmark_name != "binary-size":
        if benchmark_name == "syslog-tcp-otelcol" or (
            isinstance(suite, str) and "OTel Collector" in suite
        ):
            facets["engine"] = "otelcol"
        elif benchmark_name != "filter" or suite:
            facets["engine"] = "dfe"
    return facets


def add_facets_to_history(data_js: Path) -> bool:
    data = _read_history(data_js)
    benchmark_name = data_js.parent.name
    changed = False
    for entries in data["entries"].values():
        if not isinstance(entries, list):
            raise RuntimeError(f"History entry is not a list: {data_js}")
        for entry in entries:
            for bench in entry.get("benches", []):
                facets = _legacy_facets(benchmark_name, bench)
                if bench.get("facets") != facets:
                    bench["facets"] = facets
                    changed = True
    if changed:
        _write_history(data_js, data)
    return changed


def add_facets_to_histories(_worktree_root: Path, site_root: Path) -> bool:
    changes = [
        add_facets_to_history(data_js)
        for data_js in sorted(site_root.glob("*/data.js"))
    ]
    return any(changes)


def sync_dashboard_index(data_js: Path) -> bool:
    template = _dashboard_index_template()
    destination = data_js.with_name("index.html")
    if destination.is_file() and destination.read_bytes() == template:
        return False
    if destination.exists() and not destination.is_file():
        raise RuntimeError(
            f"Cannot synchronize benchmark index: destination is not a file: "
            f"{destination}"
        )
    destination.write_bytes(template)
    return True


def sync_dashboard_indexes(_worktree_root: Path, site_root: Path) -> bool:
    changes = [
        sync_dashboard_index(data_js)
        for data_js in sorted(site_root.glob("*/data.js"))
    ]
    return any(changes)


def sync_nightly_index(_worktree_root: Path, site_root: Path) -> bool:
    destination = site_root / "index.html"
    template = NIGHTLY_INDEX_TEMPLATE.read_bytes()
    if destination.is_file() and destination.read_bytes() == template:
        return False
    if destination.exists() and not destination.is_file():
        raise RuntimeError(
            f"Cannot synchronize nightly index: destination is not a file: "
            f"{destination}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(template)
    return True


MIGRATIONS: tuple[Migration, ...] = (
    ("0001_move_legacy_passthrough", move_legacy_passthrough),
    ("0002_sync_nightly_index", sync_nightly_index),
    ("0003_rename_nightly_passthrough", rename_nightly_passthrough),
    ("0004_merge_split_clickhouse_dashboards", merge_split_clickhouse_dashboards),
    ("0005_add_facets_to_histories", add_facets_to_histories),
    ("0006_sync_dashboard_indexes", sync_dashboard_indexes),
)


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

    try:
        for migration_id, migration in MIGRATIONS:
            if migration(worktree_root, site_root):
                print(f"Applied migration {migration_id}")
    except (OSError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
