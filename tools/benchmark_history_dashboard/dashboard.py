#!/usr/bin/env python3
"""Build, publish, or serve the benchmark history dashboard."""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

from history import (
    CommitMetadata,
    DashboardConfig,
    PublisherError,
    commit_metadata_from_git,
    enrich_result_facets,
    load_config,
    update_histories,
)


DEFAULT_OUTPUT_DIR = Path(".site")
OUTPUT_MARKER = ".benchmark-history-dashboard-output"
RUN_RESULTS_MARKER = ".benchmark-history-dashboard-run-results"
MOCK_RESULTS_MARKER = ".benchmark-history-dashboard-mock-results"
FALLBACK_FETCH_REMOTE = "upstream"
PUSH_REMOTE = "origin"


def run_git(
    repo: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise PublisherError(
            f"git {' '.join(args)} failed:\n{result.stdout}{result.stderr}".rstrip()
        )
    return result


def run_git_bytes(
    repo: Path,
    *args: str,
) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
    )
    if result.returncode != 0:
        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")
        raise PublisherError(
            f"git {' '.join(args)} failed:\n{stdout}{stderr}".rstrip()
        )
    return result.stdout


def is_retryable_push_error(result: subprocess.CompletedProcess[str]) -> bool:
    output = (result.stdout + result.stderr).lower()
    return result.returncode != 0 and any(
        marker in output
        for marker in ("non-fast-forward", "[rejected]", "fetch first", "remote rejected")
    )


def fetch_benchmarks_branch(repo: Path, remote: str, branch: str) -> str:
    result = run_git(repo, "fetch", remote, branch, check=False)
    if result.returncode == 0:
        return remote

    output = (result.stdout + result.stderr).lower()
    if (
        remote != FALLBACK_FETCH_REMOTE
        and "couldn't find remote ref" in output
    ):
        fallback = run_git(
            repo,
            "fetch",
            FALLBACK_FETCH_REMOTE,
            branch,
            check=False,
        )
        if fallback.returncode == 0:
            print(
                f"Benchmarks branch {branch} was not found on {remote}; "
                f"fetched {FALLBACK_FETCH_REMOTE}/{branch} instead.",
                file=sys.stderr,
            )
            return FALLBACK_FETCH_REMOTE
        raise PublisherError(
            f"git fetch {remote} {branch} failed:\n"
            f"{result.stdout}{result.stderr}"
            f"Fallback git fetch {FALLBACK_FETCH_REMOTE} {branch} failed:\n"
            f"{fallback.stdout}{fallback.stderr}".rstrip()
        )

    raise PublisherError(
        f"git fetch {remote} {branch} failed:\n"
        f"{result.stdout}{result.stderr}".rstrip()
    )


def commit_updates(
    repo: Path,
    changed_files: tuple[Path, ...],
    message: str,
    user_name: str | None,
    user_email: str | None,
) -> None:
    relative_paths = [
        str(path.resolve().relative_to(repo.resolve())) for path in changed_files
    ]
    run_git(repo, "add", "--", *relative_paths)
    identity_args = []
    if user_name is not None:
        identity_args.extend(["-c", f"user.name={user_name}"])
    if user_email is not None:
        identity_args.extend(["-c", f"user.email={user_email}"])
    run_git(repo, *identity_args, "commit", "-m", message)


def commit_all_changes(
    repo: Path,
    message: str,
    user_name: str | None,
    user_email: str | None,
) -> None:
    run_git(repo, "add", "--all")
    identity_args = []
    if user_name is not None:
        identity_args.extend(["-c", f"user.name={user_name}"])
    if user_email is not None:
        identity_args.extend(["-c", f"user.email={user_email}"])
    run_git(repo, *identity_args, "commit", "-m", message)


def sync_local_branch(repo: Path, remote: str, branch: str, force: bool) -> None:
    remote = fetch_benchmarks_branch(repo, remote, branch)
    local_ref = f"refs/heads/{branch}"
    remote_ref = f"{remote}/{branch}"
    local_exists = run_git(
        repo,
        "show-ref",
        "--verify",
        "--quiet",
        local_ref,
        check=False,
    )
    if local_exists.returncode == 1:
        run_git(repo, "branch", branch, remote_ref)
        return
    if local_exists.returncode != 0:
        raise PublisherError(f"Could not inspect local branch {branch}")

    local_commit = run_git(repo, "rev-parse", branch).stdout.strip()
    remote_commit = run_git(repo, "rev-parse", remote_ref).stdout.strip()
    if local_commit == remote_commit:
        return

    ancestor = run_git(
        repo,
        "merge-base",
        "--is-ancestor",
        local_commit,
        remote_commit,
        check=False,
    )
    if ancestor.returncode == 0:
        run_git(repo, "branch", "--force", branch, remote_ref)
        return
    if ancestor.returncode != 1:
        raise PublisherError(
            f"Could not compare local {branch} with {remote_ref}"
        )
    if force:
        run_git(repo, "branch", "--force", branch, remote_ref)
        return
    raise PublisherError(
        f"Local branch {branch} contains commits that are not in {remote_ref}. "
        "Refusing to overwrite local-only commits. Use --force to reset the "
        "local branch to the fetched remote branch."
    )


def _relative_path(path: Path, label: str) -> Path:
    if path.is_absolute() or ".." in path.parts:
        raise PublisherError(f"{label} must be relative without '..': {path}")
    return path


def _metadata_from_args(args: argparse.Namespace) -> CommitMetadata:
    return commit_metadata_from_git(
        args.source_repo.resolve(),
        ref=args.source_ref,
        repo_url=args.repo_url,
        github_actor=args.github_actor,
    )


def _source_sha_from_args(args: argparse.Namespace) -> str:
    return run_git(
        args.source_repo.resolve(),
        "rev-parse",
        args.source_ref,
    ).stdout.strip()


def display_config_path(config_path: Path) -> str:
    config_path = config_path.resolve()
    result = run_git(
        config_path.parent,
        "rev-parse",
        "--show-toplevel",
        check=False,
    )
    if result.returncode == 0:
        repo_root = Path(result.stdout.strip()).resolve()
        if config_path.is_relative_to(repo_root):
            return config_path.relative_to(repo_root).as_posix()
    return str(config_path)


def migration_commit_message(
    site_path: Path,
    source_sha: str,
    config_path: Path,
    additional_remarks: str | None,
) -> str:
    message = (
        f"Apply {site_path.as_posix()} migration for {source_sha[:8]}\n\n"
        "This is an automated commit generated by the "
        "benchmark_history_dashboard tool.\n"
        f"Source commit: {source_sha}\n"
        f"Configuration: {display_config_path(config_path)}"
    )
    if additional_remarks:
        message += f"\n\n{additional_remarks}"
    return message


def benchmark_results_commit_message(
    site_path: Path,
    source_sha: str,
    config_path: Path,
    additional_remarks: str | None,
) -> str:
    message = (
        f"Update {site_path.as_posix()} benchmark results for {source_sha[:8]}\n\n"
        "This is an automated commit generated by the "
        "benchmark_history_dashboard tool.\n"
        f"Source commit: {source_sha}\n"
        f"Configuration: {display_config_path(config_path)}"
    )
    if additional_remarks:
        message += f"\n\n{additional_remarks}"
    return message


def infer_repo_root(config_path: Path) -> Path:
    result = subprocess.run(
        ["git", "-C", str(config_path.parent), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise PublisherError(
            "--repo-dir was not provided and the config file is not inside "
            f"a Git worktree: {config_path}"
        )
    return Path(result.stdout.strip()).resolve()


def resolve_target(
    args: argparse.Namespace,
) -> tuple[Path, str, Path, DashboardConfig, Path | None]:
    config_path = args.config.resolve() if args.config else None
    config = load_config(config_path)
    branch = args.benchmarks_branch or config.target_branch
    if not branch:
        raise PublisherError(
            "Benchmarks branch is required through --benchmarks-branch or "
            "config target_branch"
        )
    if config.site_root is None:
        raise PublisherError("Config must specify site_root")
    site_path = args.site_root or config.site_root
    site_path = _relative_path(site_path, "--site-root")
    if args.repo_dir:
        repo_dir = args.repo_dir.resolve()
    elif config_path:
        repo_dir = infer_repo_root(config_path)
    else:
        raise PublisherError(
            "--repo-dir is required when --config is not provided"
        )
    return repo_dir, branch, site_path, config, config_path


def resolve_config_script(
    config_path: Path,
    script_path: Path,
    setting_name: str,
) -> Path:
    script = (config_path.parent / script_path).resolve()
    config_dir = config_path.parent.resolve()
    if not script.is_relative_to(config_dir):
        raise PublisherError(
            f"{setting_name} escapes the config directory: {script}"
        )
    if not script.is_file():
        raise PublisherError(f"{setting_name} does not exist: {script}")
    return script


def run_migrations(
    config: DashboardConfig,
    config_path: Path | None,
    worktree_root: Path,
    site_path: Path,
) -> bool:
    if config.migration_script is None:
        return False
    if config_path is None:
        raise PublisherError(
            "migration_script requires a config file path"
        )

    script = resolve_config_script(
        config_path,
        config.migration_script,
        "Migration script",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--worktree-root",
            str(worktree_root),
            "--site-root",
            str(site_path),
        ],
        capture_output=True,
        text=True,
    )
    if result.stdout:
        print(result.stdout.rstrip())
    if result.returncode != 0:
        raise PublisherError(
            f"Migration script failed:\n{result.stdout}{result.stderr}".rstrip()
        )
    return bool(run_git(worktree_root, "status", "--porcelain").stdout)


def _select_benchmarks(
    config: DashboardConfig,
    requested: list[str],
) -> list:
    by_name = {benchmark.name: benchmark for benchmark in config.benchmarks}
    if not requested:
        return list(config.benchmarks)
    unknown = sorted(set(requested) - set(by_name))
    if unknown:
        raise PublisherError(
            f"Unknown benchmark name(s): {', '.join(unknown)}"
        )
    return [by_name[name] for name in requested]


def _prepare_results_output(output_root: Path, marker: str) -> None:
    if output_root.exists() and any(output_root.iterdir()):
        if not (output_root / marker).is_file():
            raise PublisherError(
                f"Output directory {output_root} is not empty. For safety this "
                f"command only replaces directories containing {marker}."
            )
        shutil.rmtree(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / marker).write_text("", encoding="utf-8")


def _results_output(args: argparse.Namespace, prefix: str) -> tuple[Path, bool]:
    temporary = args.new_results is None
    output_root = (
        Path(tempfile.mkdtemp(prefix=prefix))
        if temporary
        else args.new_results.resolve()
    )
    return output_root, temporary


def _facet_overrides(values: list[str]) -> dict[str, str]:
    overrides = {}
    for value in values:
        key, separator, facet_value = value.partition("=")
        if not separator or not key or not facet_value:
            raise PublisherError(
                f"Facet override must use non-empty KEY=VALUE syntax: {value!r}"
            )
        overrides[key] = facet_value
    return overrides


def _result_group(value: str | None) -> Path | None:
    if value is None:
        return None
    path = Path(value)
    if (
        not value
        or path.is_absolute()
        or len(path.parts) != 1
        or path.name in {".", ".."}
    ):
        raise PublisherError(
            "--result-group must be one relative directory name"
        )
    return path


def cmd_mock_run(args: argparse.Namespace) -> int:
    config_path = args.config.resolve()
    config = load_config(config_path)
    benchmarks = _select_benchmarks(config, args.benchmarks)
    if not benchmarks:
        raise PublisherError(
            f"Config file {config_path} does not define any benchmarks"
        )
    output_root, _ = _results_output(args, "benchmark-mock-results-")
    _prepare_results_output(output_root, MOCK_RESULTS_MARKER)
    rng = random.Random(args.seed)
    facet_overrides = _facet_overrides(args.facet)
    result_group = _result_group(args.result_group)
    generated = 0
    for benchmark in benchmarks:
        if not benchmark.metric_sets:
            print(
                f"Skipping {benchmark.name}: no metric_sets configured.",
                file=sys.stderr,
            )
            continue
        for metric_set_name in benchmark.metric_sets:
            results = []
            for metric in config.metric_sets[metric_set_name]:
                value = round(rng.uniform(metric.minimum, metric.maximum), 4)
                result = {
                    "name": metric.name,
                    "value": value,
                    "unit": metric.unit,
                    "extra": metric.extra
                    or f"{benchmark.name} mock {metric.name}",
                }
                if metric.description is not None:
                    result["description"] = metric.description
                result = enrich_result_facets(
                    result,
                    {
                        **benchmark.facets,
                        **metric.facets,
                        **facet_overrides,
                    },
                )
                results.append(result)
            path = output_root / benchmark.name
            if result_group is not None:
                path /= result_group
            path /= f"{metric_set_name}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(results, indent=2) + "\n",
                encoding="utf-8",
            )
            generated += len(results)
    if generated == 0:
        raise PublisherError("Selected benchmarks do not define any metric sets")
    print(
        f"Generated {generated} mock metric record(s) at {output_root}"
    )
    return 0


def _dashboard_result_snapshot(results_root: Path) -> dict[Path, tuple[int, int]]:
    if not results_root.is_dir():
        return {}
    return {
        path.resolve(): (path.stat().st_mtime_ns, path.stat().st_size)
        for path in results_root.rglob("*.json")
        if path.is_file() and path.parent.name == "gh-actions-benchmark"
    }


def cmd_run(args: argparse.Namespace) -> int:
    config_path = args.config.resolve()
    config = load_config(config_path)
    if config.orchestrator_root is None:
        raise PublisherError(
            f"Config file {config_path} must specify orchestrator_root"
        )
    benchmarks = _select_benchmarks(config, args.benchmarks)
    if not benchmarks:
        raise PublisherError(
            f"Config file {config_path} does not define any benchmarks"
        )
    repo_root = (
        args.repo_dir.resolve()
        if args.repo_dir is not None
        else infer_repo_root(config_path)
    )
    orchestrator_root = (repo_root / config.orchestrator_root).resolve()
    if not orchestrator_root.is_relative_to(repo_root):
        raise PublisherError("orchestrator_root escapes the repository")
    runner = orchestrator_root / "orchestrator/run_orchestrator.py"
    if not runner.is_file():
        raise PublisherError(f"Orchestrator runner does not exist: {runner}")
    results_root = orchestrator_root / "results"

    selected = [
        (benchmark, orchestrator)
        for benchmark in benchmarks
        for orchestrator in benchmark.orchestrators
    ]
    if not selected:
        raise PublisherError(
            "Selected benchmarks do not define any orchestrators"
        )

    output_root, _ = _results_output(args, "benchmark-run-results-")
    _prepare_results_output(output_root, RUN_RESULTS_MARKER)
    copied = 0
    for benchmark, orchestrator in selected:
        config_file = (orchestrator_root / orchestrator.path).resolve()
        if not config_file.is_relative_to(orchestrator_root):
            raise PublisherError(
                f"Orchestrator path escapes orchestrator_root: {orchestrator.path}"
            )
        if not config_file.is_file():
            raise PublisherError(
                f"Orchestrator config does not exist: {config_file}"
            )
        before = _dashboard_result_snapshot(results_root)
        command = [
            sys.executable,
            str(runner),
            "--config",
            str(config_file),
        ]
        if orchestrator.tests:
            command.extend(["--tests", ",".join(orchestrator.tests)])
        print(
            f"Running {benchmark.name}/{orchestrator.name}: "
            f"{orchestrator.path.as_posix()}"
        )
        result = subprocess.run(command, cwd=orchestrator_root)
        if result.returncode != 0:
            raise PublisherError(
                f"Orchestrator {benchmark.name}/{orchestrator.name} "
                f"failed with exit code {result.returncode}"
            )
        after = _dashboard_result_snapshot(results_root)
        changed = [
            path
            for path, state in after.items()
            if before.get(path) != state
        ]
        if not changed:
            raise PublisherError(
                f"Orchestrator {benchmark.name}/{orchestrator.name} produced "
                "no new or updated gh-actions-benchmark JSON files"
            )
        for source in sorted(changed):
            relative = source.relative_to(results_root.resolve())
            destination = (
                output_root
                / benchmark.name
                / orchestrator.name
                / relative
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                source_results = json.loads(source.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise PublisherError(
                    f"Could not read orchestrator results from {source}: {exc}"
                ) from exc
            if not isinstance(source_results, list):
                raise PublisherError(
                    f"Orchestrator result file {source} must contain a JSON array"
                )
            static_facets = {**benchmark.facets, **orchestrator.facets}
            enriched_results = []
            for index, source_result in enumerate(source_results):
                if not isinstance(source_result, dict):
                    raise PublisherError(
                        f"Orchestrator result {source}[{index}] must be an object"
                    )
                enriched_results.append(
                    enrich_result_facets(source_result, static_facets)
                )
            destination.write_text(
                json.dumps(enriched_results, indent=2) + "\n",
                encoding="utf-8",
            )
            copied += 1

    print(f"Collected {copied} benchmark result file(s) at {output_root}")
    return 0


def _update_site(
    args: argparse.Namespace,
    site_root: Path,
    config: DashboardConfig,
    metadata: CommitMetadata,
    run_date_ms: int,
):
    return update_histories(
        input_root=args.new_results.resolve(),
        site_root=site_root,
        metadata=metadata,
        run_date_ms=run_date_ms,
        benchmark_facets={
            benchmark.name: benchmark.facets
            for benchmark in config.benchmarks
        },
    )


def cmd_build(args: argparse.Namespace) -> int:
    output_dir = args.output_dir.resolve()
    if output_dir.exists() and not (output_dir / OUTPUT_MARKER).is_file():
        raise PublisherError(
            f"Output directory {output_dir} already exists. For safety this tool "
            f"doesn't overwrite directories unless they contain the file {OUTPUT_MARKER}"
        )

    repo_dir, branch, site_path, config, config_path = resolve_target(args)
    metadata = _metadata_from_args(args) if args.new_results is not None else None
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    worktree_path: Path | None = None
    result = None
    try:
        sync_local_branch(repo_dir, args.remote, branch, force=args.force)
        with tempfile.TemporaryDirectory(prefix="benchmark-build-") as worktree_temp:
            worktree_path = Path(worktree_temp) / "worktree"
            run_git(
                repo_dir,
                "worktree",
                "add",
                "--detach",
                str(worktree_path),
                branch,
            )
            run_migrations(
                config,
                config_path,
                worktree_path,
                site_path,
            )
            source_site = worktree_path / site_path
            if not source_site.is_dir():
                raise PublisherError(
                    f"Configured site root does not exist on {branch}: {site_path}"
                )
            with tempfile.TemporaryDirectory(
                prefix=f".{output_dir.name}-", dir=output_dir.parent
            ) as output_temp:
                staged_site = Path(output_temp) / "site"
                shutil.copytree(source_site, staged_site)
                if metadata is not None:
                    result = _update_site(
                        args,
                        staged_site,
                        config,
                        metadata,
                        int(time.time() * 1000),
                    )
                (staged_site / OUTPUT_MARKER).write_text("", encoding="utf-8")
                if output_dir.exists():
                    shutil.rmtree(output_dir)
                shutil.move(str(staged_site), output_dir)
    except OSError as exc:
        raise PublisherError(f"Could not build dashboard site: {exc}") from exc
    finally:
        if worktree_path is not None:
            run_git(
                repo_dir,
                "worktree",
                "remove",
                "--force",
                str(worktree_path),
                check=False,
            )
            run_git(repo_dir, "worktree", "prune", check=False)

    if result is None:
        print(
            f"Built dashboard site without merging new results at {output_dir}."
        )
    else:
        print(
            f"Built {result.benchmark_records} metric record(s) in "
            f"{result.destinations} destination(s) at {output_dir}."
        )
    return 0


def cmd_update_branch(args: argparse.Namespace) -> int:
    if args.push and args.max_attempts <= 0:
        raise PublisherError("--max-attempts must be positive")
    if args.push and args.patch_output is not None:
        raise PublisherError("--patch-output cannot be combined with --push")

    repo_dir, branch, site_path, config, config_path = resolve_target(args)
    patch_output = (
        args.patch_output.resolve()
        if args.patch_output is not None
        else None
    )

    metadata = _metadata_from_args(args) if args.new_results is not None else None
    source_sha = metadata.sha if metadata is not None else _source_sha_from_args(args)
    if config_path is None:
        raise PublisherError("update-branch requires a config file path")
    migration_message = migration_commit_message(
        site_path,
        source_sha,
        config_path,
        args.additional_commit_remarks,
    )
    run_date_ms = int(time.time() * 1000)
    commit_message = None
    if metadata is not None:
        commit_message = (
            args.commit_message
            or benchmark_results_commit_message(
                site_path,
                metadata.sha,
                config_path,
                args.additional_commit_remarks,
            )
        )

    attempts = args.max_attempts if args.push else 1
    for attempt in range(1, attempts + 1):
        worktree_path: Path | None = None
        try:
            sync_local_branch(
                repo_dir,
                args.remote,
                branch,
                force=args.force,
            )
            base_commit = run_git(
                repo_dir,
                "rev-parse",
                branch,
            ).stdout.strip()
            with tempfile.TemporaryDirectory(prefix="benchmark-publish-") as temp_dir:
                worktree_path = Path(temp_dir) / "worktree"
                run_git(
                    repo_dir,
                    "worktree",
                    "add",
                    "--detach",
                    str(worktree_path),
                    branch,
                )
                migration_changed = run_migrations(
                    config,
                    config_path,
                    worktree_path,
                    site_path,
                )
                if migration_changed:
                    commit_all_changes(
                        worktree_path,
                        migration_message,
                        args.git_user_name,
                        args.git_user_email,
                    )
                result = None
                if metadata is not None:
                    result = _update_site(
                        args,
                        worktree_path / site_path,
                        config,
                        metadata,
                        run_date_ms,
                    )
                if result is not None and result.changed:
                    commit_updates(
                        worktree_path,
                        result.changed_files,
                        commit_message,
                        args.git_user_name,
                        args.git_user_email,
                    )
                history_changed = result is not None and result.changed
                if not migration_changed and not history_changed:
                    if patch_output is not None:
                        patch_output.unlink(missing_ok=True)
                    if result is None:
                        print("No dashboard migrations to apply.")
                    else:
                        print("Benchmark history is already up to date.")
                    return 0

                if not args.push:
                    commit = run_git(worktree_path, "rev-parse", "HEAD").stdout.strip()
                    if patch_output is not None:
                        patch = run_git_bytes(
                            worktree_path,
                            "format-patch",
                            "--stdout",
                            "--binary",
                            f"{base_commit}..{commit}",
                        )
                        patch_output.parent.mkdir(parents=True, exist_ok=True)
                        with tempfile.NamedTemporaryFile(
                            mode="wb",
                            prefix=f".{patch_output.name}.",
                            dir=patch_output.parent,
                            delete=False,
                        ) as temp_patch:
                            temp_patch.write(patch)
                            temp_patch_path = Path(temp_patch.name)
                        os.replace(temp_patch_path, patch_output)
                    run_git(
                        repo_dir,
                        "branch",
                        "--force",
                        branch,
                        commit,
                    )
                    print(
                        f"Committed dashboard changes to local branch {branch} "
                        "without pushing."
                    )
                    if patch_output is not None:
                        print(f"Wrote Git patch to {patch_output}.")
                    return 0

                push = run_git(
                    worktree_path,
                    "push",
                    PUSH_REMOTE,
                    f"HEAD:refs/heads/{branch}",
                    check=False,
                )
                if push.returncode == 0:
                    commit = run_git(worktree_path, "rev-parse", "HEAD").stdout.strip()
                    run_git(repo_dir, "branch", "--force", branch, commit)
                    if result is None:
                        print("Published dashboard migrations.")
                    else:
                        print(
                            f"Published {result.benchmark_records} metric record(s) "
                            f"to {result.destinations} destination(s)."
                        )
                    return 0
                if not is_retryable_push_error(push):
                    raise PublisherError(
                        f"git push failed:\n{push.stdout}{push.stderr}".rstrip()
                    )
                print(
                    f"Push attempt {attempt}/{args.max_attempts} was rejected; "
                    "retrying from the latest branch head.",
                    file=sys.stderr,
                )
        finally:
            if worktree_path is not None:
                run_git(
                    repo_dir,
                    "worktree",
                    "remove",
                    "--force",
                    str(worktree_path),
                    check=False,
                )
                run_git(repo_dir, "worktree", "prune", check=False)

        if attempt < args.max_attempts and args.retry_delay > 0:
            delay = args.retry_delay * (2 ** (attempt - 1))
            delay += random.random() * args.retry_delay
            time.sleep(delay)

    raise PublisherError(f"Push was rejected after {args.max_attempts} attempts")


class DashboardHandler(SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        if self.path.endswith("data.js"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()


def cmd_serve(args: argparse.Namespace) -> int:
    output_dir = args.output_dir.resolve()
    if not output_dir.is_dir():
        raise PublisherError(
            f"Output directory not found: {output_dir}. Run `dashboard.py build` first."
        )

    handler = partial(DashboardHandler, directory=str(output_dir))
    server = HTTPServer(("127.0.0.1", args.port), handler)
    print(f"Serving {output_dir} at http://localhost:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.server_close()
    return 0


def _add_update_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--new-results",
        type=Path,
        help=(
            "Directory containing new benchmark JSON results. Omit to run "
            "configured migrations without merging history."
        ),
    )
    parser.add_argument(
        "--source-repo",
        type=Path,
        default=Path.cwd(),
        help=(
            "Git repository used to resolve the source commit SHA and, when "
            "--new-results is supplied, its history metadata "
            "(default: current directory)."
        ),
    )
    parser.add_argument(
        "--source-ref",
        default="HEAD",
        help=(
            "Commit in --source-repo named in migration commits and recorded "
            "in benchmark history (default: HEAD)."
        ),
    )
    parser.add_argument(
        "--repo-url",
        help=(
            "Repository URL stored in history. Defaults to GITHUB_REPOSITORY "
            "or the source repository's origin URL."
        ),
    )
    parser.add_argument(
        "--github-actor",
        help=(
            "GitHub username stored for the history author and committer "
            "(default: GITHUB_ACTOR)."
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help=(
            "Dashboard configuration file. Must specify site_root and may "
            "provide the benchmarks branch, migrations, benchmark inventory, "
            "and mock metric sets."
        ),
    )


def _add_target_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--repo-dir",
        type=Path,
        help=(
            "Git repository containing the benchmarks branch. Defaults to "
            "the Git worktree containing --config."
        ),
    )
    parser.add_argument(
        "--benchmarks-branch",
        help=(
            "Benchmarks branch to fetch and use "
            "(default: config target_branch)."
        ),
    )
    parser.add_argument(
        "--site-root",
        type=Path,
        help=(
            "Dashboard directory relative to the benchmarks branch root "
            "(default: config site_root)."
        ),
    )
    parser.add_argument(
        "--remote",
        default="origin",
        help=(
            "Preferred Git remote from which to fetch the benchmarks branch "
            "(default: origin). If that remote lacks the branch, the command "
            "falls back to upstream."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Reset a local-only or diverged benchmarks branch to the fetched "
            "remote branch before creating the temporary worktree."
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dashboard.py",
        description=__doc__,
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    mock_run = subcommands.add_parser(
        "mock-run",
        help="Generate mock benchmark inputs from configured metric sets.",
        description=(
            "Generate benchmark JSON for configured benchmarks and metric "
            "sets. When --new-results is omitted, a persistent "
            "temporary directory is created."
        ),
    )
    mock_run.add_argument(
        "benchmarks",
        nargs="*",
        help="Benchmark names to generate (default: all configured benchmarks).",
    )
    mock_run.add_argument(
        "--new-results",
        type=Path,
        help=(
            "Output directory for generated benchmark inputs. If omitted, "
            "the command creates and reports a persistent temporary directory."
        ),
    )
    mock_run.add_argument(
        "--config",
        type=Path,
        required=True,
        help="Dashboard configuration containing benchmarks and metric_sets.",
    )
    mock_run.add_argument(
        "--seed",
        type=int,
        help="Optional random seed for reproducible mock values.",
    )
    mock_run.add_argument(
        "--facet",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help=(
            "Override a facet on every generated record. Repeat for multiple "
            "facets."
        ),
    )
    mock_run.add_argument(
        "--result-group",
        help=(
            "Nest each metric-set JSON below this directory within its "
            "benchmark bucket, allowing multiple mock runs to merge safely."
        ),
    )
    mock_run.set_defaults(func=cmd_mock_run)

    run = subcommands.add_parser(
        "run",
        help="Run configured benchmark orchestrators and collect their results.",
        description=(
            "Execute each selected benchmark's orchestrator configurations and "
            "collect new gh-actions-benchmark JSON beneath the benchmark name."
        ),
    )
    run.add_argument(
        "benchmarks",
        nargs="*",
        help="Benchmark names to run (default: all with orchestrators).",
    )
    run.add_argument("--config", type=Path, required=True)
    run.add_argument(
        "--repo-dir",
        type=Path,
        help=(
            "Repository root containing orchestrator_root. Defaults to the Git "
            "worktree containing --config."
        ),
    )
    run.add_argument(
        "--new-results",
        type=Path,
        help=(
            "Directory for collected benchmark inputs. If omitted, the command "
            "creates and reports a persistent temporary directory."
        ),
    )
    run.set_defaults(func=cmd_run)

    build = subcommands.add_parser(
        "build",
        help="Build a local site from the configured benchmarks branch.",
        description=(
            "Fetch and synchronize the benchmarks branch, run configured "
            "migrations in a temporary worktree, optionally merge new results, "
            "and write a local dashboard site without committing or pushing."
        ),
    )
    _add_target_arguments(build)
    build.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=(
            f"Generated site location (default: {DEFAULT_OUTPUT_DIR}). An "
            "existing directory is replaced only when it contains "
            f"{OUTPUT_MARKER}."
        ),
    )
    _add_update_arguments(build)
    build.set_defaults(func=cmd_build)

    update_branch = subcommands.add_parser(
        "update-branch",
        help="Update and commit a benchmark branch, optionally pushing it.",
    )
    _add_target_arguments(update_branch)
    update_branch.add_argument(
        "--push",
        action="store_true",
        help=(
            "Push the generated commit to origin and retry concurrent update "
            "rejections."
        ),
    )
    update_branch.add_argument("--max-attempts", type=int, default=5)
    update_branch.add_argument("--retry-delay", type=float, default=1.0)
    update_branch.add_argument(
        "--git-user-name",
        help=(
            "Override Git user.name for generated commits. By default Git "
            "uses the repository or global configuration."
        ),
    )
    update_branch.add_argument(
        "--git-user-email",
        help=(
            "Override Git user.email for generated commits. By default Git "
            "uses the repository or global configuration."
        ),
    )
    update_branch.add_argument("--commit-message")
    update_branch.add_argument(
        "--additional-commit-remarks",
        help=(
            "Arbitrary text appended after a blank line to the automated "
            "migration commit message."
        ),
    )
    update_branch.add_argument(
        "--patch-output",
        type=Path,
        help=(
            "Without --push, write an mbox patch containing every generated "
            "commit. No file is left behind when the update is a no-op."
        ),
    )
    _add_update_arguments(update_branch)
    update_branch.set_defaults(func=cmd_update_branch)

    serve = subcommands.add_parser(
        "serve",
        help="Serve a generated dashboard site over HTTP.",
    )
    serve.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Generated site location (default: {DEFAULT_OUTPUT_DIR}).",
    )
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(func=cmd_serve)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.func(args) or 0
    except PublisherError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
