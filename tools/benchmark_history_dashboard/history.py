#!/usr/bin/env python3
"""Merge custom benchmark JSON artifacts into github-action-benchmark data.js files."""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


DATA_PREFIX = "window.BENCHMARK_DATA = "
DEFAULT_INDEX_HTML_PATH = Path(__file__).with_name("default_index.html")
DEFAULT_BENCHMARK_NAME = "Benchmark"
DEFAULT_MAX_ITEMS = 100
DEFAULT_TOOL = "customSmallerIsBetter"
RESULT_KEYS = {"name", "value", "unit", "range", "extra", "description", "facets"}
METRIC_KEYS = {"name", "unit", "mock", "extra", "description", "facets"}
MOCK_KEYS = {"min", "max"}
ORCHESTRATOR_KEYS = {"name", "path", "tests", "facets"}
BENCHMARK_KEYS = {"name", "orchestrators", "metric_sets", "facets"}


class PublisherError(RuntimeError):
    """Raised when benchmark inputs or history are invalid."""


@dataclass(frozen=True)
class CommitMetadata:
    """Commit metadata stored in a benchmark history entry."""

    sha: str
    message: str
    timestamp: str
    url: str
    author_name: str
    author_email: str
    author_username: str | None
    committer_name: str
    committer_email: str
    committer_username: str | None

    def as_data_js_commit(self) -> dict[str, Any]:
        author = {"name": self.author_name, "email": self.author_email}
        committer = {"name": self.committer_name, "email": self.committer_email}
        if self.author_username:
            author["username"] = self.author_username
        if self.committer_username:
            committer["username"] = self.committer_username
        return {
            "author": author,
            "committer": committer,
            "id": self.sha,
            "message": self.message,
            "timestamp": self.timestamp,
            "url": self.url,
        }


@dataclass(frozen=True)
class MockMetric:
    """One metric produced by mock-run."""

    name: str
    unit: str
    minimum: float
    maximum: float
    extra: str | None = None
    description: str | None = None
    facets: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Orchestrator:
    """One runnable orchestrator configuration."""

    name: str
    path: Path
    tests: tuple[str, ...] = ()
    facets: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Benchmark:
    """A dashboard bucket and the executions and metric sets that feed it."""

    name: str
    orchestrators: tuple[Orchestrator, ...] = ()
    metric_sets: tuple[str, ...] = ()
    facets: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DashboardConfig:
    """Configuration shared by local builds and branch updates."""

    target_branch: str | None = None
    site_root: Path | None = None
    migration_script: Path | None = None
    orchestrator_root: Path | None = None
    metric_sets: dict[str, tuple[MockMetric, ...]] = field(default_factory=dict)
    benchmarks: tuple[Benchmark, ...] = ()


@dataclass
class DestinationUpdate:
    """All benchmark records that become one history entry."""

    data_js_path: Path
    benches: list[dict[str, Any]] = field(default_factory=list)
    sources: list[Path] = field(default_factory=list)


@dataclass(frozen=True)
class UpdateResult:
    """Summary of a completed history update."""

    changed_files: tuple[Path, ...]
    destinations: int
    benchmark_records: int

    @property
    def changed(self) -> bool:
        return bool(self.changed_files)


def _run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise PublisherError(
            f"git {' '.join(args)} failed:\n{result.stdout}{result.stderr}".rstrip()
        )
    return result.stdout


def normalize_repo_url(url: str) -> str:
    """Normalize a Git remote URL into the HTTPS URL stored by the action."""
    url = url.strip()
    if url.startswith("git@github.com:"):
        url = "https://github.com/" + url[len("git@github.com:") :]
    elif url.startswith("ssh://git@github.com/"):
        url = "https://github.com/" + url[len("ssh://git@github.com/") :]
    if url.endswith(".git"):
        url = url[:-4]
    return url.rstrip("/")


def commit_metadata_from_git(
    source_repo: Path,
    ref: str = "HEAD",
    repo_url: str | None = None,
    github_actor: str | None = None,
) -> CommitMetadata:
    """Read action-compatible commit metadata from a source Git checkout."""
    fmt = "%H%x00%an%x00%ae%x00%cn%x00%ce%x00%aI%x00%B"
    raw = _run_git(source_repo, "show", "-s", f"--format={fmt}", ref)
    parts = raw.split("\0", 6)
    if len(parts) != 7:
        raise PublisherError(f"Could not parse commit metadata for {ref}")

    sha, author_name, author_email, committer_name, committer_email, timestamp, message = parts
    message = message.rstrip("\r\n")

    if repo_url is None:
        repository = os.environ.get("GITHUB_REPOSITORY")
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
        if repository:
            repo_url = f"{server.rstrip('/')}/{repository}"
        else:
            repo_url = _run_git(source_repo, "remote", "get-url", "origin").strip()
    repo_url = normalize_repo_url(repo_url)

    actor = github_actor or os.environ.get("GITHUB_ACTOR")
    return CommitMetadata(
        sha=sha,
        message=message,
        timestamp=timestamp,
        url=f"{repo_url}/commit/{sha}",
        author_name=author_name,
        author_email=author_email,
        author_username=actor,
        committer_name=committer_name,
        committer_email=committer_email,
        committer_username=actor,
    )


def load_config(path: Path | None) -> DashboardConfig:
    """Load and validate dashboard configuration."""
    if path is None:
        return DashboardConfig()
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise PublisherError(f"Could not load config from {path}: {exc}") from exc

    if not isinstance(document, dict):
        raise PublisherError(f"Config file {path} must contain a YAML object")
    unknown_top = set(document) - {
        "version",
        "target_branch",
        "site_root",
        "migration_script",
        "orchestrator_root",
        "metric_sets",
        "benchmarks",
    }
    if unknown_top:
        raise PublisherError(f"Unknown config file keys: {sorted(unknown_top)}")
    if document.get("version") != 1:
        raise PublisherError(f"Config file {path} must declare version: 1")

    target_branch = document.get("target_branch")
    if target_branch is not None and (
        not isinstance(target_branch, str) or not target_branch
    ):
        raise PublisherError("target_branch must be a non-empty string")

    raw_site_root = document.get("site_root")
    if raw_site_root is None:
        raise PublisherError(f"Config file {path} must specify site_root")
    if not isinstance(raw_site_root, str) or not raw_site_root:
        raise PublisherError("site_root must be a non-empty string")
    site_root = Path(raw_site_root)
    if site_root.is_absolute() or ".." in site_root.parts:
        raise PublisherError("site_root must be relative without '..'")

    raw_migration_script = document.get("migration_script")
    migration_script = None
    if raw_migration_script is not None:
        if not isinstance(raw_migration_script, str) or not raw_migration_script:
            raise PublisherError("migration_script must be a non-empty string")
        migration_script = Path(raw_migration_script)
        if migration_script.is_absolute() or ".." in migration_script.parts:
            raise PublisherError(
                "migration_script must be relative to the config without '..'"
            )

    raw_orchestrator_root = document.get("orchestrator_root")
    orchestrator_root = None
    if raw_orchestrator_root is not None:
        orchestrator_root = _config_relative_path(
            raw_orchestrator_root,
            "orchestrator_root",
        )

    raw_metric_sets = document.get("metric_sets", {})
    if not isinstance(raw_metric_sets, dict):
        raise PublisherError("metric_sets must be an object")
    metric_sets: dict[str, tuple[MockMetric, ...]] = {}
    for set_name, raw_metrics in raw_metric_sets.items():
        label = f"metric_sets.{set_name}"
        if not isinstance(set_name, str) or not set_name:
            raise PublisherError("metric_sets keys must be non-empty strings")
        if not isinstance(raw_metrics, list) or not raw_metrics:
            raise PublisherError(f"{label} must be a non-empty list")
        metrics = []
        seen_metric_names: set[str] = set()
        for index, raw_metric in enumerate(raw_metrics):
            metric_label = f"{label}[{index}]"
            if not isinstance(raw_metric, dict):
                raise PublisherError(f"{metric_label} must be an object")
            unknown = set(raw_metric) - METRIC_KEYS
            if unknown:
                raise PublisherError(
                    f"{metric_label} has unknown keys: {sorted(unknown)}"
                )
            metric_name = raw_metric.get("name")
            unit = raw_metric.get("unit")
            extra = raw_metric.get("extra")
            description = raw_metric.get("description")
            if not isinstance(metric_name, str) or not metric_name:
                raise PublisherError(f"{metric_label}.name must be a non-empty string")
            if metric_name in seen_metric_names:
                raise PublisherError(f"{label} contains duplicate metric {metric_name!r}")
            seen_metric_names.add(metric_name)
            if not isinstance(unit, str):
                raise PublisherError(f"{metric_label}.unit must be a string")
            if extra is not None and not isinstance(extra, str):
                raise PublisherError(f"{metric_label}.extra must be a string")
            if description is not None and not isinstance(description, str):
                raise PublisherError(f"{metric_label}.description must be a string")
            facets = _facet_dict(
                raw_metric.get("facets", {}),
                f"{metric_label}.facets",
            )
            raw_mock = raw_metric.get("mock")
            if not isinstance(raw_mock, dict):
                raise PublisherError(f"{metric_label}.mock must be an object")
            unknown_mock = set(raw_mock) - MOCK_KEYS
            if unknown_mock:
                raise PublisherError(
                    f"{metric_label}.mock has unknown keys: {sorted(unknown_mock)}"
                )
            minimum = raw_mock.get("min")
            maximum = raw_mock.get("max")
            if not _finite_number(minimum) or not _finite_number(maximum):
                raise PublisherError(
                    f"{metric_label}.mock min and max must be finite numbers"
                )
            if minimum > maximum:
                raise PublisherError(
                    f"{metric_label}.mock min must not exceed max"
                )
            metrics.append(
                MockMetric(
                    name=metric_name,
                    unit=unit,
                    minimum=float(minimum),
                    maximum=float(maximum),
                    extra=extra,
                    description=description,
                    facets=facets,
                )
            )
        metric_sets[set_name] = tuple(metrics)

    raw_benchmarks = document.get("benchmarks", [])
    if not isinstance(raw_benchmarks, list):
        raise PublisherError("benchmarks must be a list")
    benchmarks = []
    benchmark_names: set[str] = set()
    for index, raw_benchmark in enumerate(raw_benchmarks):
        label = f"benchmarks[{index}]"
        if not isinstance(raw_benchmark, dict):
            raise PublisherError(f"{label} must be an object")
        unknown = set(raw_benchmark) - BENCHMARK_KEYS
        if unknown:
            raise PublisherError(f"{label} has unknown keys: {sorted(unknown)}")
        name = raw_benchmark.get("name")
        _validate_bucket_name(name, f"{label}.name")
        if name in benchmark_names:
            raise PublisherError(f"Duplicate benchmark name: {name}")
        benchmark_names.add(name)

        raw_orchestrators = raw_benchmark.get("orchestrators", [])
        if not isinstance(raw_orchestrators, list):
            raise PublisherError(f"{label}.orchestrators must be a list")
        orchestrators = []
        orchestrator_names: set[str] = set()
        for orchestrator_index, raw_orchestrator in enumerate(raw_orchestrators):
            orchestrator_label = (
                f"{label}.orchestrators[{orchestrator_index}]"
            )
            if isinstance(raw_orchestrator, str):
                raw_orchestrator = {"path": raw_orchestrator}
            if not isinstance(raw_orchestrator, dict):
                raise PublisherError(f"{orchestrator_label} must be an object")
            unknown = set(raw_orchestrator) - ORCHESTRATOR_KEYS
            if unknown:
                raise PublisherError(
                    f"{orchestrator_label} has unknown keys: {sorted(unknown)}"
                )
            path_value = _config_relative_path(
                raw_orchestrator.get("path"),
                f"{orchestrator_label}.path",
            )
            orchestrator_name = raw_orchestrator.get("name", path_value.stem)
            _validate_bucket_name(
                orchestrator_name,
                f"{orchestrator_label}.name",
            )
            if orchestrator_name in orchestrator_names:
                raise PublisherError(
                    f"{label} contains duplicate orchestrator name "
                    f"{orchestrator_name!r}"
                )
            orchestrator_names.add(orchestrator_name)
            tests = _string_tuple(
                raw_orchestrator.get("tests", []),
                f"{orchestrator_label}.tests",
            )
            facets = _facet_dict(
                raw_orchestrator.get("facets", {}),
                f"{orchestrator_label}.facets",
            )
            orchestrators.append(
                Orchestrator(
                    name=orchestrator_name,
                    path=path_value,
                    tests=tests,
                    facets=facets,
                )
            )

        configured_metric_sets = _string_tuple(
            raw_benchmark.get("metric_sets", []),
            f"{label}.metric_sets",
        )
        unknown_metric_sets = set(configured_metric_sets) - set(metric_sets)
        if unknown_metric_sets:
            raise PublisherError(
                f"{label}.metric_sets references unknown sets: "
                f"{sorted(unknown_metric_sets)}"
            )
        facets = _facet_dict(
            raw_benchmark.get("facets", {}),
            f"{label}.facets",
        )
        benchmarks.append(
            Benchmark(
                name=name,
                orchestrators=tuple(orchestrators),
                metric_sets=configured_metric_sets,
                facets=facets,
            )
        )
    return DashboardConfig(
        target_branch=target_branch,
        site_root=site_root,
        migration_script=migration_script,
        orchestrator_root=orchestrator_root,
        metric_sets=metric_sets,
        benchmarks=tuple(benchmarks),
    )


def _config_relative_path(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise PublisherError(f"{label} must be a non-empty string")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise PublisherError(f"{label} must be relative without '..'")
    return path


def _validate_bucket_name(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value:
        raise PublisherError(f"{label} must be a non-empty string")
    path = Path(value)
    if len(path.parts) != 1 or value in {".", ".."}:
        raise PublisherError(f"{label} must be one directory name")


def _string_tuple(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise PublisherError(f"{label} must be a list of non-empty strings")
    if len(value) != len(set(value)):
        raise PublisherError(f"{label} must not contain duplicates")
    return tuple(value)


def _finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _facet_dict(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise PublisherError(f"{label} must be an object")
    facets: dict[str, str] = {}
    for key, facet_value in value.items():
        if not isinstance(key, str) or not key:
            raise PublisherError(f"{label} keys must be non-empty strings")
        if not isinstance(facet_value, str) or not facet_value:
            raise PublisherError(f"{label}.{key} must be a non-empty string")
        facets[key] = facet_value
    return facets


def infer_facets_from_extra(extra: str | None) -> dict[str, str]:
    """Extract reliable grouping dimensions from legacy report labels."""
    if not extra or "/" not in extra:
        return {}
    suite, remainder = extra.split("/", 1)
    if " - " not in remainder:
        return {}
    scenario, _description = remainder.rsplit(" - ", 1)
    if not suite or not scenario:
        return {}

    facets = {"suite": suite, "scenario": scenario}
    signal_match = re.match(r"^(Logs|Metrics|Traces)(?:-|$)", scenario)
    if signal_match:
        facets["signal"] = signal_match.group(1).lower()
    cores_match = re.search(
        r"(\d+)\s+Core(?:\(s\)|s)?",
        f"{suite} {scenario}",
        re.IGNORECASE,
    )
    if cores_match:
        facets["cores"] = cores_match.group(1)
    return facets


def enrich_result_facets(
    result: dict[str, Any],
    static_facets: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return a result with inferred, explicit, and execution facets merged."""
    enriched = dict(result)
    facets = infer_facets_from_extra(enriched.get("extra"))
    binary_match = re.match(
        r"^(linux|windows)-(amd64|arm64)-(binary-size|text-size|crate-(.+))$",
        enriched.get("name", ""),
    )
    if binary_match:
        facets.update(
            {
                "os": binary_match.group(1),
                "architecture": binary_match.group(2),
            }
        )
        measurement = binary_match.group(3)
        if measurement.startswith("crate-"):
            facets["measurement"] = "crate"
            facets["crate"] = binary_match.group(4)
        else:
            facets["measurement"] = measurement
    if static_facets:
        facets.update(static_facets)
    explicit = enriched.get("facets", {})
    if explicit:
        facets.update(_facet_dict(explicit, "result.facets"))
    if facets:
        enriched["facets"] = facets
    return enriched


def _safe_destination(root: Path, relative: str, label: str) -> Path:
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise PublisherError(f"{label} must be a relative path without '..': {relative}")
    resolved_root = root.resolve()
    resolved = (resolved_root / relative_path).resolve()
    if not resolved.is_relative_to(resolved_root):
        raise PublisherError(f"{label} escapes its root: {relative}")
    return resolved


def _read_results(
    path: Path,
    static_facets: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublisherError(f"Could not read benchmark results from {path}: {exc}") from exc
    if not isinstance(value, list):
        raise PublisherError(f"Benchmark result file {path} must contain a JSON array")

    results = []
    for index, raw in enumerate(value):
        label = f"{path}[{index}]"
        if not isinstance(raw, dict):
            raise PublisherError(f"{label} must be an object")
        unknown = set(raw) - RESULT_KEYS
        if unknown:
            raise PublisherError(f"{label} has unknown keys: {sorted(unknown)}")
        if not isinstance(raw.get("name"), str) or not raw["name"]:
            raise PublisherError(f"{label}.name must be a non-empty string")
        number = raw.get("value")
        if (
            not isinstance(number, (int, float))
            or isinstance(number, bool)
            or not math.isfinite(number)
        ):
            raise PublisherError(f"{label}.value must be a finite number")
        if not isinstance(raw.get("unit"), str):
            raise PublisherError(f"{label}.unit must be a string")
        for optional in ("range", "extra", "description"):
            if optional in raw and not isinstance(raw[optional], str):
                raise PublisherError(f"{label}.{optional} must be a string")
        if "facets" in raw:
            _facet_dict(raw["facets"], f"{label}.facets")
        results.append(enrich_result_facets(dict(raw), static_facets))
    return results


def _load_data_js(path: Path, repo_url: str) -> dict[str, Any]:
    if not path.exists():
        return {"lastUpdate": 0, "repoUrl": repo_url, "entries": {}}
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PublisherError(f"Could not read history {path}: {exc}") from exc
    if not content.startswith(DATA_PREFIX):
        raise PublisherError(f"History {path} does not start with {DATA_PREFIX!r}")
    try:
        data = json.loads(content[len(DATA_PREFIX) :].rstrip().removesuffix(";"))
    except json.JSONDecodeError as exc:
        raise PublisherError(f"History {path} contains invalid JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
        raise PublisherError(f"History {path} does not contain an entries object")
    return data


def _add_destination(
    updates: dict[Path, DestinationUpdate],
    data_js_path: Path,
    benches: list[dict[str, Any]],
    sources: list[Path],
) -> None:
    if not benches:
        source_list = ", ".join(str(source) for source in sources)
        raise PublisherError(f"No benchmark metrics selected from {source_list}")
    current = updates.get(data_js_path)
    if current is None:
        updates[data_js_path] = DestinationUpdate(
            data_js_path=data_js_path,
            benches=list(benches),
            sources=list(sources),
        )
        return
    current.benches.extend(benches)
    current.sources.extend(sources)


def plan_updates(
    input_root: Path,
    site_root: Path,
    benchmark_facets: dict[str, dict[str, str]] | None = None,
) -> dict[Path, DestinationUpdate]:
    """Discover, validate, and route all input JSON files."""
    input_root = input_root.resolve()
    site_root = site_root.resolve()
    if not input_root.is_dir():
        raise PublisherError(f"Input root does not exist or is not a directory: {input_root}")

    all_files = sorted(path.resolve() for path in input_root.rglob("*.json") if path.is_file())
    if any(not path.is_relative_to(input_root) for path in all_files):
        raise PublisherError(f"Input JSON symlink escapes the input root: {input_root}")
    if not all_files:
        raise PublisherError(f"No JSON benchmark results found under {input_root}")

    updates: dict[Path, DestinationUpdate] = {}
    automatic: dict[str, list[Path]] = {}
    for source in all_files:
        relative = source.relative_to(input_root)
        if len(relative.parts) < 2:
            raise PublisherError(
                f"Benchmark result file must be below a benchmark directory: {source}"
            )
        bucket = relative.parts[0]
        automatic.setdefault(bucket, []).append(source)

    for bucket, sources in sorted(automatic.items()):
        benches = []
        for source in sources:
            benches.extend(
                _read_results(
                    source,
                    (benchmark_facets or {}).get(bucket),
                )
            )
        destination = _safe_destination(site_root, bucket, "automatic destination")
        _add_destination(
            updates,
            destination / "data.js",
            benches,
            sources,
        )

    return updates


def _same_benchmark_entry(existing: dict[str, Any], candidate: dict[str, Any]) -> bool:
    return (
        existing.get("commit", {}).get("id") == candidate["commit"]["id"]
        and existing.get("tool") == candidate["tool"]
        and existing.get("benches") == candidate["benches"]
    )


def update_histories(
    input_root: Path,
    site_root: Path,
    metadata: CommitMetadata,
    run_date_ms: int | None = None,
    benchmark_facets: dict[str, dict[str, str]] | None = None,
) -> UpdateResult:
    """Apply all benchmark results atomically after complete validation."""
    updates = plan_updates(input_root, site_root, benchmark_facets)
    repo_url = metadata.url.rsplit("/commit/", 1)[0]
    date_ms = run_date_ms if run_date_ms is not None else int(time.time() * 1000)
    default_index_html = DEFAULT_INDEX_HTML_PATH.read_text(encoding="utf-8")

    rendered: dict[Path, str] = {}
    total_records = 0
    for path, update in sorted(updates.items(), key=lambda item: str(item[0])):
        data = _load_data_js(path, repo_url)
        data["repoUrl"] = repo_url
        entries = data["entries"].setdefault(DEFAULT_BENCHMARK_NAME, [])
        if not isinstance(entries, list):
            raise PublisherError(
                f"History {path} entry {DEFAULT_BENCHMARK_NAME!r} is not a list"
            )

        tool = DEFAULT_TOOL
        for existing in reversed(entries):
            existing_tool = existing.get("tool")
            if isinstance(existing_tool, str) and existing_tool:
                tool = existing_tool
                break
        candidate = {
            "commit": metadata.as_data_js_commit(),
            "date": date_ms,
            "tool": tool,
            "benches": update.benches,
        }
        total_records += len(update.benches)
        if not any(_same_benchmark_entry(existing, candidate) for existing in entries):
            entries.append(candidate)
            if len(entries) > DEFAULT_MAX_ITEMS:
                del entries[: len(entries) - DEFAULT_MAX_ITEMS]
            data["lastUpdate"] = date_ms
            rendered[path] = DATA_PREFIX + json.dumps(data, indent=2, ensure_ascii=False)

        index_html_path = path.with_name("index.html")
        if not index_html_path.exists():
            rendered[index_html_path] = default_index_html

    temp_files: list[tuple[Path, Path]] = []
    try:
        for path, content in rendered.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
            temp_path = Path(temp_name)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
            temp_files.append((temp_path, path))
        for temp_path, path in temp_files:
            os.replace(temp_path, path)
    finally:
        for temp_path, _ in temp_files:
            temp_path.unlink(missing_ok=True)

    return UpdateResult(
        changed_files=tuple(rendered),
        destinations=len(updates),
        benchmark_records=total_records,
    )
