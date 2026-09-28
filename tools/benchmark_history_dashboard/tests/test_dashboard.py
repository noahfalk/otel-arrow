#!/usr/bin/env python3
"""Tests for the local benchmark history publisher."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

from dashboard import benchmark_results_commit_message, migration_commit_message
from history import (
    DATA_PREFIX,
    CommitMetadata,
    PublisherError,
    load_config,
    update_histories,
)


def metadata() -> CommitMetadata:
    return CommitMetadata(
        sha="0123456789abcdef",
        message="test benchmark publisher",
        timestamp="2026-09-27T05:00:00Z",
        url="https://github.com/open-telemetry/otel-arrow/commit/0123456789abcdef",
        author_name="Test Author",
        author_email="author@example.com",
        author_username="test-author",
        committer_name="Test Committer",
        committer_email="committer@example.com",
        committer_username="test-committer",
    )


def write_results(path: Path, results: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(results), encoding="utf-8")


def write_history(path: Path, entries: list[dict] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "lastUpdate": 1,
        "repoUrl": "https://github.com/open-telemetry/otel-arrow",
        "entries": {"Benchmark": entries or []},
    }
    path.write_text(DATA_PREFIX + json.dumps(data, indent=2), encoding="utf-8")


def read_history(path: Path) -> dict:
    content = path.read_text(encoding="utf-8")
    return json.loads(content[len(DATA_PREFIX) :])


def copy_nightly_migrations(config: Path) -> None:
    shutil.copy2(
        TOOL_DIR / "configs/nightly_migrations.py",
        config.with_name("nightly_migrations.py"),
    )
    template = config.parent / "nightly/index.html"
    template.parent.mkdir()
    shutil.copy2(TOOL_DIR / "configs/nightly/index.html", template)
    shutil.copy2(
        TOOL_DIR / "default_index.html",
        config.with_name("default_index.html"),
    )


class BenchmarkPublisherTests(unittest.TestCase):
    # Scenario: Results arrive for a benchmark destination with no prior dashboard files.
    # Guarantees: The updater creates data.js and the benchmark-action default index.html.
    def test_new_benchmark_creates_default_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            inputs = root / "inputs"
            repo = root / "target"
            site = repo / "docs"
            history = site / "benchmarks/nightly"
            write_results(
                inputs / "new-benchmark/results.json",
                [{"name": "latency", "value": 12, "unit": "ms"}],
            )

            result = update_histories(
                inputs, history, metadata(), run_date_ms=1000
            )

            destination = history / "new-benchmark"
            data_js = destination / "data.js"
            index_html = destination / "index.html"
            self.assertEqual(set(result.changed_files), {data_js, index_html})
            self.assertEqual(
                read_history(data_js)["entries"]["Benchmark"][-1]["benches"],
                [{"name": "latency", "value": 12, "unit": "ms"}],
            )
            self.assertEqual(
                index_html.read_text(encoding="utf-8"),
                (TOOL_DIR / "default_index.html").read_text(encoding="utf-8"),
            )

    # Scenario: New result records contain explicit structured facets and descriptions.
    # Guarantees: The publisher validates and persists the new fields without dropping them.
    def test_result_facets_are_validated_and_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            inputs = root / "inputs"
            history = root / "site"
            expected = {
                "name": "cpu",
                "value": 12,
                "unit": "%",
                "description": "Normalized CPU",
                "facets": {
                    "os": "linux",
                    "scenario": "OTAP-OTAP",
                },
            }
            write_results(inputs / "filter/results.json", [expected])

            update_histories(inputs, history, metadata(), run_date_ms=1000)

            benches = read_history(
                history / "filter/data.js"
            )["entries"]["Benchmark"][-1]["benches"]
            self.assertEqual(benches, [expected])

            write_results(
                inputs / "filter/results.json",
                [{
                    "name": "cpu",
                    "value": 12,
                    "unit": "%",
                    "facets": {"os": ""},
                }],
            )
            with self.assertRaisesRegex(PublisherError, "non-empty string"):
                update_histories(inputs, history, metadata(), run_date_ms=1000)

    # Scenario: Multiple producers place result files below one conventional bucket.
    # Guarantees: Their metrics become one history entry and existing HTML is untouched.
    def test_convention_combines_recursive_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            inputs = root / "inputs"
            repo = root / "target"
            site = repo / "docs"
            history = site / "benchmarks/nightly"
            write_results(
                inputs / "filter/dfe/results.json",
                [{"name": "cpu", "value": 10, "unit": "%"}],
            )
            write_results(
                inputs / "filter/otelcol/results.json",
                [{"name": "ram", "value": 20, "unit": "MiB"}],
            )
            data_js = history / "filter/data.js"
            write_history(data_js)
            index_html = data_js.with_name("index.html")
            index_html.write_text("custom dashboard", encoding="utf-8")

            result = update_histories(
                inputs, history, metadata(), run_date_ms=1000
            )

            self.assertTrue(result.changed)
            entry = read_history(data_js)["entries"]["Benchmark"][-1]
            self.assertEqual(entry["tool"], "customSmallerIsBetter")
            self.assertEqual([bench["name"] for bench in entry["benches"]], ["cpu", "ram"])
            self.assertEqual(index_html.read_text(encoding="utf-8"), "custom dashboard")

    # Scenario: Convention-routed results update existing and new dashboards.
    # Guarantees: Existing tool direction is preserved and new dashboards default smaller-is-better.
    def test_convention_preserves_existing_tool(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            inputs = root / "inputs"
            history = root / "docs/benchmarks/nightly"
            write_results(
                inputs / "clickhouse/results.json",
                [
                    {"name": "logs_produced_rate", "value": 100, "unit": "logs/sec"},
                    {"name": "cpu", "value": 25, "unit": "%"},
                ],
            )
            write_results(
                inputs / "scaling-efficiency/results.json",
                [{"name": "efficiency", "value": 0.9, "unit": "ratio"}],
            )
            write_results(
                inputs / "passthrough/results.json",
                [{"name": "throughput", "value": 200, "unit": "logs/sec"}],
            )
            write_history(
                history / "scaling-efficiency/data.js",
                [
                    {
                        "commit": {"id": "old"},
                        "date": 1,
                        "tool": "customBiggerIsBetter",
                        "benches": [
                            {"name": "efficiency", "value": 0.8, "unit": "ratio"}
                        ],
                    }
                ],
            )

            update_histories(
                inputs,
                history,
                metadata(),
                run_date_ms=1000,
            )

            clickhouse = read_history(history / "clickhouse/data.js")
            scaling = read_history(history / "scaling-efficiency/data.js")
            passthrough = read_history(history / "passthrough/data.js")
            self.assertEqual(
                [item["name"] for item in clickhouse["entries"]["Benchmark"][-1]["benches"]],
                ["logs_produced_rate", "cpu"],
            )
            self.assertEqual(
                clickhouse["entries"]["Benchmark"][-1]["tool"],
                "customSmallerIsBetter",
            )
            self.assertEqual(
                scaling["entries"]["Benchmark"][-1]["tool"],
                "customBiggerIsBetter",
            )
            self.assertEqual(
                passthrough["entries"]["Benchmark"][-1]["tool"],
                "customSmallerIsBetter",
            )
            self.assertFalse((history / "clickhouse-throughput").exists())
            self.assertFalse((history / "clickhouse-resources").exists())

    # Scenario: A dashboard config omits the persisted site's repository location.
    # Guarantees: Config validation requires an explicit site_root value.
    def test_config_requires_site_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config.yaml"
            config.write_text("version: 1\n", encoding="utf-8")

            with self.assertRaisesRegex(
                PublisherError,
                "must specify site_root",
            ):
                load_config(config)

    # Scenario: Mock generation is requested from a config without benchmark definitions.
    # Guarantees: The error identifies that there is nothing to generate.
    def test_mock_run_requires_configured_benchmarks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "config.yaml"
            config.write_text(
                "version: 1\nsite_root: docs/benchmarks/nightly\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(TOOL_DIR / "dashboard.py"),
                    "mock-run",
                    "--config",
                    str(config),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("does not define any benchmarks", result.stderr)

    # Scenario: Mock runs use explicit and automatically allocated directories.
    # Guarantees: Both outputs persist and contain convention-routed metric-set data.
    def test_mock_run_generates_explicit_and_temporary_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            explicit_output = root / "explicit-results"
            site = root / "site"
            config = TOOL_DIR / "configs/nightly.yaml"
            command = [
                sys.executable,
                str(TOOL_DIR / "dashboard.py"),
                "mock-run",
                "--config",
                str(config),
            ]

            explicit = subprocess.run(
                [*command, "--new-results", str(explicit_output)],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn(
                f"mock metric record(s) at {explicit_output}",
                explicit.stdout,
            )
            self.assertTrue(
                (
                    explicit_output
                    / ".benchmark-history-dashboard-mock-results"
                ).is_file()
            )
            self.assertTrue(
                (explicit_output / "clickhouse/clickhouse.json").is_file()
            )
            mock_record = json.loads(
                (explicit_output / "clickhouse/clickhouse.json").read_text(
                    encoding="utf-8"
                )
            )[0]
            self.assertEqual(mock_record["facets"]["os"], "linux")
            update = update_histories(
                explicit_output,
                site,
                metadata(),
                run_date_ms=1000,
            )
            loaded = load_config(config)
            expected_records = sum(
                len(loaded.metric_sets[metric_set])
                for benchmark in loaded.benchmarks
                for metric_set in benchmark.metric_sets
            )
            self.assertEqual(update.benchmark_records, expected_records)
            self.assertEqual(update.destinations, len(loaded.benchmarks))

            temporary = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
            )
            output_line = temporary.stdout.splitlines()[-1]
            prefix = "Generated "
            self.assertTrue(output_line.startswith(prefix))
            temporary_output = Path(output_line.rsplit(" at ", 1)[1])
            try:
                self.assertTrue(temporary_output.is_dir())
                self.assertTrue(
                    (temporary_output / "syslog/standard_logs.json").is_file()
                )
            finally:
                shutil.rmtree(temporary_output)

            matrix_output = root / "matrix-results"
            subprocess.run(
                [
                    *command,
                    "filter",
                    "--new-results",
                    str(matrix_output),
                    "--facet",
                    "os=windows",
                    "--result-group",
                    "windows",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            matrix_records = json.loads(
                (
                    matrix_output
                    / "filter/windows/standard_logs.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                {record["facets"]["os"] for record in matrix_records},
                {"windows"},
            )
            matrix_site = root / "matrix-site"
            update_histories(
                matrix_output,
                matrix_site,
                metadata(),
                run_date_ms=1000,
                benchmark_facets={"filter": {"os": "linux", "engine": "dfe"}},
            )
            published_records = read_history(
                matrix_site / "filter/data.js"
            )["entries"]["Benchmark"][-1]["benches"]
            self.assertEqual(
                {record["facets"]["os"] for record in published_records},
                {"windows"},
            )

    # Scenario: The continuous and binary-size publishers are selected independently.
    # Guarantees: Each config generates convention-routed mock data with its dashboard-specific facets.
    def test_additional_dashboard_configs_generate_mock_results(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cases = (
                (
                    "continuous.yaml",
                    "continuous/continuous_logs.json",
                    {"os": "linux", "engine": "dfe"},
                ),
                (
                    "binary-size.yaml",
                    "binary-size/binary_sizes.json",
                    {
                        "os": "linux",
                        "architecture": "amd64",
                        "measurement": "binary-size",
                    },
                ),
            )
            for config_name, relative_output, expected_facets in cases:
                with self.subTest(config=config_name):
                    output = root / config_name
                    subprocess.run(
                        [
                            sys.executable,
                            str(TOOL_DIR / "dashboard.py"),
                            "mock-run",
                            "--config",
                            str(TOOL_DIR / f"configs/{config_name}"),
                            "--new-results",
                            str(output),
                        ],
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                    records = json.loads(
                        (output / relative_output).read_text(encoding="utf-8")
                    )
                    self.assertGreater(len(records), 0)
                    self.assertEqual(records[0]["facets"], expected_facets)

    # Scenario: Two orchestrators contribute results to one benchmark dashboard.
    # Guarantees: Run executes both, passes optional test filters, and collects collision-free nested files.
    def test_run_executes_grouped_orchestrators(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            orchestrator_root = repo / "tools/pipeline_perf_test"
            runner = orchestrator_root / "orchestrator/run_orchestrator.py"
            runner.parent.mkdir(parents=True)
            runner.write_text(
                """
import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--config", type=Path, required=True)
parser.add_argument("--tests")
args = parser.parse_args()
name = args.config.stem
output = Path("results") / name / "gh-actions-benchmark" / "results.json"
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps([{
    "name": name,
    "value": len(args.tests.split(",")) if args.tests else 1,
    "unit": "count",
    "extra": "Nightly - Filter/Logs-OTAP-OTLP - Count"
}]))
""".lstrip(),
                encoding="utf-8",
            )
            configs = orchestrator_root / "test_suites"
            configs.mkdir()
            (configs / "first.yaml").write_text("tests: []\n", encoding="utf-8")
            (configs / "second.yaml").write_text("tests: []\n", encoding="utf-8")
            dashboard_config = repo / "dashboard.yaml"
            dashboard_config.write_text(
                """
version: 1
site_root: docs/benchmarks/nightly
orchestrator_root: tools/pipeline_perf_test
benchmarks:
  - name: filter
    facets: {os: linux}
    orchestrators:
      - name: dfe
        path: test_suites/first.yaml
        facets: {engine: dfe}
      - name: otelcol
        path: test_suites/second.yaml
        tests: [one, two]
        facets: {engine: otelcol}
""".lstrip(),
                encoding="utf-8",
            )
            output = root / "results"

            result = subprocess.run(
                [
                    sys.executable,
                    str(TOOL_DIR / "dashboard.py"),
                    "run",
                    "filter",
                    "--config",
                    str(dashboard_config),
                    "--repo-dir",
                    str(repo),
                    "--new-results",
                    str(output),
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            first = output / "filter/dfe/first/gh-actions-benchmark/results.json"
            second = (
                output
                / "filter/otelcol/second/gh-actions-benchmark/results.json"
            )
            self.assertTrue(first.is_file())
            self.assertTrue(second.is_file())
            self.assertEqual(json.loads(first.read_text())[0]["value"], 1)
            self.assertEqual(json.loads(second.read_text())[0]["value"], 2)
            first_result = json.loads(first.read_text())[0]
            second_result = json.loads(second.read_text())[0]
            self.assertEqual(
                first_result["facets"],
                {
                    "suite": "Nightly - Filter",
                    "scenario": "Logs-OTAP-OTLP",
                    "signal": "logs",
                    "os": "linux",
                    "engine": "dfe",
                },
            )
            self.assertEqual(second_result["facets"]["engine"], "otelcol")
            self.assertIn("Collected 2 benchmark result file(s)", result.stdout)

    # Scenario: One input is malformed after another destination has validated.
    # Guarantees: Validation failure leaves every existing history file unchanged.
    def test_validation_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            inputs = root / "inputs"
            repo = root / "target"
            history = repo / "docs/benchmarks/nightly"
            first = history / "first/data.js"
            write_history(first)
            original = first.read_bytes()
            write_results(
                inputs / "first/results.json",
                [{"name": "cpu", "value": 10, "unit": "%"}],
            )
            write_results(
                inputs / "second/results.json",
                [{"name": "cpu", "value": float("nan"), "unit": "%"}],
            )

            with self.assertRaises(PublisherError):
                update_histories(
                    inputs, history, metadata(), run_date_ms=1000
                )

            self.assertEqual(first.read_bytes(), original)
            self.assertFalse((history / "second/data.js").exists())

    # Scenario: The same commit and metric payload is applied twice after reaching retention.
    # Guarantees: History stays capped at 100 entries and retrying is idempotent.
    def test_retention_and_idempotency(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            inputs = root / "inputs"
            repo = root / "target"
            history = repo / "docs/benchmarks/nightly"
            data_js = history / "syslog/data.js"
            prior = [
                {
                    "commit": {"id": f"old-{index}"},
                    "date": index,
                    "tool": "customSmallerIsBetter",
                    "benches": [{"name": "cpu", "value": index, "unit": "%"}],
                }
                for index in range(100)
            ]
            write_history(data_js, prior)
            write_results(
                inputs / "syslog/results.json",
                [{"name": "cpu", "value": 101, "unit": "%"}],
            )

            first = update_histories(
                inputs, history, metadata(), run_date_ms=1000
            )
            second = update_histories(
                inputs, history, metadata(), run_date_ms=2000
            )
            entries = read_history(data_js)["entries"]["Benchmark"]

            self.assertTrue(first.changed)
            self.assertFalse(second.changed)
            self.assertEqual(len(entries), 100)
            self.assertEqual(entries[0]["commit"]["id"], "old-1")
            self.assertEqual(entries[-1]["commit"]["id"], metadata().sha)

    # Scenario: The publisher targets a local remote branch twice with identical inputs.
    # Guarantees: It creates one aggregate commit and the retry-safe second run is a no-op.
    def test_publish_script_commits_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            remote = root / "remote.git"
            seed = root / "seed"
            source = root / "source"
            target = root / "target"
            inputs = root / "inputs"
            config = root / "config.yaml"
            script = TOOL_DIR / "dashboard.py"
            config.write_text(
                "version: 1\nsite_root: docs/benchmarks/nightly\n",
                encoding="utf-8",
            )

            subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
            subprocess.run(
                ["git", "init", "-b", "benchmarks", str(seed)],
                check=True,
                capture_output=True,
            )
            write_history(seed / "docs/benchmarks/nightly/syslog/data.js")
            (seed / "docs/benchmarks/nightly/syslog/index.html").write_text(
                "custom dashboard", encoding="utf-8"
            )
            self._commit_all(seed, "seed history")
            subprocess.run(
                ["git", "-C", str(seed), "remote", "add", "origin", str(remote)],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(seed), "push", "-u", "origin", "benchmarks"],
                check=True,
                capture_output=True,
            )

            subprocess.run(
                ["git", "init", "-b", "main", str(source)],
                check=True,
                capture_output=True,
            )
            (source / "README.txt").write_text("source", encoding="utf-8")
            self._commit_all(source, "source commit")
            subprocess.run(
                ["git", "clone", "--branch", "benchmarks", str(remote), str(target)],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(target), "checkout", "--detach"],
                check=True,
                capture_output=True,
            )
            write_results(
                inputs / "syslog/results.json",
                [{"name": "cpu", "value": 10, "unit": "%"}],
            )

            command = [
                sys.executable,
                str(script),
                "update-branch",
                "--repo-dir",
                str(target),
                "--benchmarks-branch",
                "benchmarks",
                "--push",
                "--config",
                str(config),
                "--new-results",
                str(inputs),
                "--site-root",
                "docs/benchmarks/nightly",
                "--source-repo",
                str(source),
                "--repo-url",
                "https://github.com/open-telemetry/otel-arrow",
                "--git-user-name",
                "CI Publisher",
                "--git-user-email",
                "ci-publisher@example.com",
                "--retry-delay",
                "0",
            ]
            subprocess.run(command, check=True, capture_output=True, text=True)
            subprocess.run(command, check=True, capture_output=True, text=True)

            verify = root / "verify"
            subprocess.run(
                ["git", "clone", "--branch", "benchmarks", str(remote), str(verify)],
                check=True,
                capture_output=True,
            )
            count = subprocess.run(
                ["git", "-C", str(verify), "rev-list", "--count", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            history = read_history(verify / "docs/benchmarks/nightly/syslog/data.js")
            author = subprocess.run(
                ["git", "-C", str(verify), "log", "-1", "--format=%an <%ae>"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            message = subprocess.run(
                ["git", "-C", str(verify), "log", "-1", "--format=%B"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.rstrip()
            source_sha = self._head(source)
            self.assertEqual(count, "2")
            self.assertEqual(author, "CI Publisher <ci-publisher@example.com>")
            self.assertEqual(
                message,
                (
                    "Update docs/benchmarks/nightly benchmark results for "
                    f"{source_sha[:8]}\n\n"
                    "This is an automated commit generated by the "
                    "benchmark_history_dashboard tool.\n"
                    f"Source commit: {source_sha}\n"
                    f"Configuration: {config.resolve()}"
                ),
            )
            self.assertEqual(len(history["entries"]["Benchmark"]), 1)
            self.assertEqual(
                (verify / "docs/benchmarks/nightly/syslog/index.html").read_text(
                    encoding="utf-8"
                ),
                "custom dashboard",
            )

    # Scenario: origin has no benchmarks branch while upstream has existing history.
    # Guarantees: Build falls back to upstream and publication still pushes to origin.
    def test_missing_origin_branch_falls_back_to_upstream(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            origin = root / "origin.git"
            upstream = root / "upstream.git"
            seed = root / "seed"
            source = root / "source"
            target = root / "target"
            inputs = root / "inputs"
            output = root / "output"
            script = TOOL_DIR / "dashboard.py"

            subprocess.run(
                ["git", "init", "--bare", str(origin)],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "init", "--bare", str(upstream)],
                check=True,
                capture_output=True,
            )
            self._init_repo(
                seed,
                "benchmarks",
                "docs/benchmarks/nightly/syslog/index.html",
                "custom dashboard",
            )
            write_history(seed / "docs/benchmarks/nightly/syslog/data.js")
            self._commit_all(seed, "seed history")
            upstream_head = self._head(seed)
            subprocess.run(
                ["git", "-C", str(seed), "remote", "add", "upstream", str(upstream)],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(seed), "push", "upstream", "benchmarks"],
                check=True,
                capture_output=True,
            )

            self._init_repo(source, "main", "source.txt", "source")
            self._init_repo(target, "main", "local.txt", "controller")
            subprocess.run(
                ["git", "-C", str(target), "remote", "add", "origin", str(origin)],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(target), "remote", "add", "upstream", str(upstream)],
                check=True,
            )
            config = target / "configs/nightly.yaml"
            config.parent.mkdir()
            config.write_text(
                """
version: 1
target_branch: benchmarks
site_root: docs/benchmarks/nightly
""".lstrip(),
                encoding="utf-8",
            )
            self._commit_all(target, "add dashboard config")
            write_results(
                inputs / "syslog/results.json",
                [{"name": "cpu", "value": 10, "unit": "%"}],
            )

            common_args = [
                "--config",
                str(config),
                "--new-results",
                str(inputs),
                "--source-repo",
                str(source),
                "--repo-url",
                "https://github.com/open-telemetry/otel-arrow",
            ]
            built = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "build",
                    "--output-dir",
                    str(output),
                    *common_args,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn(
                "fetched upstream/benchmarks instead",
                built.stderr,
            )
            self.assertEqual(
                len(
                    read_history(output / "syslog/data.js")["entries"][
                        "Benchmark"
                    ]
                ),
                1,
            )

            published = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "update-branch",
                    "--push",
                    "--retry-delay",
                    "0",
                    *common_args,
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            origin_head = subprocess.run(
                ["git", "--git-dir", str(origin), "rev-parse", "benchmarks"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            upstream_after = subprocess.run(
                ["git", "--git-dir", str(upstream), "rev-parse", "benchmarks"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertIn(
                "fetched upstream/benchmarks instead",
                published.stderr,
            )
            self.assertNotEqual(origin_head, upstream_head)
            self.assertEqual(upstream_after, upstream_head)

    # Scenario: Another publisher advances the remote after checkout but before push.
    # Guarantees: A rejected push is retried by re-fetching and merging both history entries.
    def test_publish_script_retries_concurrent_update(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            remote = root / "remote.git"
            seed = root / "seed"
            source = root / "source"
            target = root / "target"
            competitor = root / "competitor"
            inputs = root / "inputs"
            script = TOOL_DIR / "dashboard.py"

            subprocess.run(
                ["git", "init", "--bare", str(remote)],
                check=True,
                capture_output=True,
            )
            self._init_repo(
                seed,
                "benchmarks",
                "docs/benchmarks/nightly/syslog/index.html",
                "custom dashboard",
            )
            write_history(seed / "docs/benchmarks/nightly/syslog/data.js")
            legacy_passthrough = (
                seed / "docs/benchmarks/continuous-passthrough/index.html"
            )
            legacy_passthrough.parent.mkdir(parents=True, exist_ok=True)
            legacy_passthrough.write_text(
                "legacy passthrough", encoding="utf-8"
            )
            self._commit_all(seed, "seed history")
            subprocess.run(
                ["git", "-C", str(seed), "remote", "add", "origin", str(remote)],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(seed), "push", "-u", "origin", "benchmarks"],
                check=True,
                capture_output=True,
            )

            self._init_repo(source, "main", "source.txt", "source")
            subprocess.run(
                ["git", "clone", "--branch", "benchmarks", str(remote), str(target)],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(target), "checkout", "--detach"],
                check=True,
                capture_output=True,
            )
            config = target / "configs/nightly.yaml"
            config.parent.mkdir()
            config.write_text(
                """
version: 1
target_branch: benchmarks
site_root: docs/benchmarks/nightly
migration_script: nightly_migrations.py
""".lstrip(),
                encoding="utf-8",
            )
            copy_nightly_migrations(config)
            subprocess.run(
                [
                    "git",
                    "clone",
                    "--branch",
                    "benchmarks",
                    str(remote),
                    str(competitor),
                ],
                check=True,
                capture_output=True,
            )
            write_results(
                inputs / "syslog/results.json",
                [{"name": "cpu", "value": 10, "unit": "%"}],
            )

            paused = remote / "publisher-paused"
            paused_once = remote / "publisher-paused-once"
            release = remote / "release-publisher"
            hook = remote / "hooks/pre-receive"
            hook.write_text(
                f"""#!/bin/sh
while read old new ref; do
  message=$(git log -1 --format=%s "$new")
  if [ "$message" = "publisher update" ] && [ ! -f "{paused_once.as_posix()}" ]; then
    touch "{paused_once.as_posix()}"
    touch "{paused.as_posix()}"
    while [ ! -f "{release.as_posix()}" ]; do
      sleep 0.05
    done
  fi
done
""",
                encoding="utf-8",
                newline="\n",
            )

            command = [
                sys.executable,
                str(script),
                "update-branch",
                "--repo-dir",
                str(target),
                "--benchmarks-branch",
                "benchmarks",
                "--push",
                "--new-results",
                str(inputs),
                "--config",
                str(config),
                "--source-repo",
                str(source),
                "--repo-url",
                "https://github.com/open-telemetry/otel-arrow",
                "--commit-message",
                "publisher update",
                "--retry-delay",
                "0",
            ]
            publisher = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            deadline = time.monotonic() + 10
            while not paused.exists() and publisher.poll() is None:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
            if not paused.exists():
                stdout, stderr = publisher.communicate(timeout=5)
                self.fail(
                    "Publisher did not reach the blocked first push.\n"
                    f"stdout:\n{stdout}\nstderr:\n{stderr}"
                )

            concurrent_history_path = (
                competitor / "docs/benchmarks/nightly/syslog/data.js"
            )
            concurrent_history = read_history(concurrent_history_path)
            concurrent_history["entries"]["Benchmark"].append(
                {
                    "commit": {"id": "concurrent-update"},
                    "date": 900,
                    "tool": "customSmallerIsBetter",
                    "benches": [{"name": "memory", "value": 20, "unit": "MiB"}],
                }
            )
            concurrent_history_path.write_text(
                DATA_PREFIX + json.dumps(concurrent_history, indent=2),
                encoding="utf-8",
            )

            competitor_error = None
            try:
                self._commit_all(competitor, "concurrent update")
                subprocess.run(
                    ["git", "-C", str(competitor), "push", "origin", "benchmarks"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except Exception as exc:
                competitor_error = exc
            finally:
                release.write_text("release", encoding="utf-8")

            stdout, stderr = publisher.communicate(timeout=30)
            if competitor_error is not None:
                raise competitor_error
            self.assertEqual(publisher.returncode, 0, f"{stdout}\n{stderr}")
            self.assertIn("Push attempt 1/5 was rejected", stderr)

            verify = root / "verify-concurrent"
            subprocess.run(
                ["git", "clone", "--branch", "benchmarks", str(remote), str(verify)],
                check=True,
                capture_output=True,
            )
            entries = read_history(
                verify / "docs/benchmarks/nightly/syslog/data.js"
            )["entries"]["Benchmark"]
            self.assertEqual(
                [entry["commit"]["id"] for entry in entries],
                ["concurrent-update", self._head(source)],
            )
            self.assertFalse(
                (
                    verify / "docs/benchmarks/continuous-passthrough"
                ).exists()
            )
            self.assertEqual(
                (
                    verify
                    / "docs/benchmarks/nightly/passthrough/index.html"
                ).read_text(encoding="utf-8"),
                "legacy passthrough",
            )
            count = subprocess.run(
                ["git", "-C", str(verify), "rev-list", "--count", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(count, "5")
            subjects = subprocess.run(
                ["git", "-C", str(verify), "log", "-2", "--format=%s"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()
            self.assertEqual(
                subjects,
                [
                    "publisher update",
                    (
                        "Apply docs/benchmarks/nightly migration for "
                        f"{self._head(source)[:8]}"
                    ),
                ],
            )

    # Scenario: Config inside a Git checkout supplies branch and site defaults.
    # Guarantees: The repo root is inferred and the default advances only the local ref.
    def test_update_branch_infers_repo_and_config_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            remote = root / "remote.git"
            seed = root / "seed"
            source = root / "source"
            target = root / "target"
            inputs = root / "inputs"
            script = TOOL_DIR / "dashboard.py"

            subprocess.run(
                ["git", "init", "--bare", str(remote)],
                check=True,
                capture_output=True,
            )
            self._init_repo(
                seed,
                "benchmarks",
                "docs/benchmarks/nightly/syslog/index.html",
                "custom dashboard",
            )
            write_history(seed / "docs/benchmarks/nightly/syslog/data.js")
            self._commit_all(seed, "seed history")
            subprocess.run(
                ["git", "-C", str(seed), "remote", "add", "origin", str(remote)],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(seed), "push", "-u", "origin", "benchmarks"],
                check=True,
                capture_output=True,
            )
            remote_before = self._head(seed)

            self._init_repo(source, "main", "source.txt", "source")
            self._init_repo(target, "main", "local.txt", "unchanged checkout")
            config = target / "configs/nightly.yaml"
            config.parent.mkdir()
            config.write_text(
                """
version: 1
target_branch: benchmarks
site_root: docs/benchmarks/nightly
""".lstrip(),
                encoding="utf-8",
            )
            self._commit_all(target, "add dashboard config")
            target_head = self._head(target)
            subprocess.run(
                ["git", "-C", str(target), "remote", "add", "origin", str(remote)],
                check=True,
            )
            write_results(
                inputs / "syslog/results.json",
                [{"name": "cpu", "value": 10, "unit": "%"}],
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "update-branch",
                    "--config",
                    str(config),
                    "--new-results",
                    str(inputs),
                    "--source-repo",
                    str(source),
                    "--repo-url",
                    "https://github.com/open-telemetry/otel-arrow",
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertIn("without pushing", result.stdout)
            self.assertEqual(self._head(target), target_head)
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(target), "status", "--short"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout,
                "",
            )
            local_branch = subprocess.run(
                ["git", "-C", str(target), "rev-parse", "benchmarks"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            remote_branch = subprocess.run(
                ["git", "-C", str(target), "rev-parse", "origin/benchmarks"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertNotEqual(local_branch, remote_branch)
            self.assertEqual(remote_branch, remote_before)

            history_content = subprocess.run(
                [
                    "git",
                    "-C",
                    str(target),
                    "show",
                    "benchmarks:docs/benchmarks/nightly/syslog/data.js",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            entries = json.loads(history_content[len(DATA_PREFIX) :])["entries"][
                "Benchmark"
            ]
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["commit"]["id"], self._head(source))
            worktrees = subprocess.run(
                ["git", "-C", str(target), "worktree", "list", "--porcelain"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertEqual(worktrees.count("worktree "), 1)

    # Scenario: The local benchmark branch is behind its fetched remote branch.
    # Guarantees: The default fast-forwards it before adding the new benchmark commit.
    def test_update_branch_default_fast_forwards_local_branch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            remote = root / "remote.git"
            seed = root / "seed"
            source = root / "source"
            target = root / "target"
            inputs = root / "inputs"
            config = root / "config.yaml"
            script = TOOL_DIR / "dashboard.py"
            config.write_text(
                "version: 1\nsite_root: docs/benchmarks/nightly\n",
                encoding="utf-8",
            )

            subprocess.run(
                ["git", "init", "--bare", str(remote)],
                check=True,
                capture_output=True,
            )
            self._init_repo(
                seed,
                "benchmarks",
                "docs/benchmarks/nightly/syslog/index.html",
                "custom dashboard",
            )
            write_history(seed / "docs/benchmarks/nightly/syslog/data.js")
            self._commit_all(seed, "seed history")
            subprocess.run(
                ["git", "-C", str(seed), "remote", "add", "origin", str(remote)],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(seed), "push", "-u", "origin", "benchmarks"],
                check=True,
                capture_output=True,
            )

            subprocess.run(
                ["git", "clone", "--branch", "benchmarks", str(remote), str(target)],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(target), "checkout", "--detach"],
                check=True,
                capture_output=True,
            )
            local_before = subprocess.run(
                ["git", "-C", str(target), "rev-parse", "benchmarks"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            concurrent_history_path = seed / "docs/benchmarks/nightly/syslog/data.js"
            concurrent_history = read_history(concurrent_history_path)
            concurrent_history["entries"]["Benchmark"].append(
                {
                    "commit": {"id": "remote-update"},
                    "date": 900,
                    "tool": "customSmallerIsBetter",
                    "benches": [{"name": "memory", "value": 20, "unit": "MiB"}],
                }
            )
            concurrent_history_path.write_text(
                DATA_PREFIX + json.dumps(concurrent_history, indent=2),
                encoding="utf-8",
            )
            self._commit_all(seed, "remote update")
            subprocess.run(
                ["git", "-C", str(seed), "push", "origin", "benchmarks"],
                check=True,
                capture_output=True,
            )
            remote_before = self._head(seed)

            self._init_repo(source, "main", "source.txt", "source")
            write_results(
                inputs / "syslog/results.json",
                [{"name": "cpu", "value": 10, "unit": "%"}],
            )
            subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "update-branch",
                    "--repo-dir",
                    str(target),
                    "--benchmarks-branch",
                    "benchmarks",
                    "--config",
                    str(config),
                    "--new-results",
                    str(inputs),
                    "--source-repo",
                    str(source),
                    "--repo-url",
                    "https://github.com/open-telemetry/otel-arrow",
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            local_after = subprocess.run(
                ["git", "-C", str(target), "rev-parse", "benchmarks"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            parent = subprocess.run(
                ["git", "-C", str(target), "rev-parse", "benchmarks^"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertNotEqual(local_after, local_before)
            self.assertEqual(parent, remote_before)
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(target), "rev-parse", "origin/benchmarks"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                remote_before,
            )

    # Scenario: The local benchmark branch contains a commit absent from the remote.
    # Guarantees: The tool refuses to overwrite or push the local-only commit.
    def test_update_branch_refuses_local_only_commits(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            remote = root / "remote.git"
            seed = root / "seed"
            source = root / "source"
            target = root / "target"
            inputs = root / "inputs"
            config = root / "config.yaml"
            script = TOOL_DIR / "dashboard.py"
            config.write_text(
                "version: 1\nsite_root: docs/benchmarks/nightly\n",
                encoding="utf-8",
            )

            subprocess.run(
                ["git", "init", "--bare", str(remote)],
                check=True,
                capture_output=True,
            )
            self._init_repo(seed, "benchmarks", "site.txt", "remote")
            subprocess.run(
                ["git", "-C", str(seed), "remote", "add", "origin", str(remote)],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(seed), "push", "-u", "origin", "benchmarks"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "clone", "--branch", "benchmarks", str(remote), str(target)],
                check=True,
                capture_output=True,
            )
            (target / "local.txt").write_text("local only", encoding="utf-8")
            self._commit_all(target, "local only")
            local_before = self._head(target)
            subprocess.run(
                ["git", "-C", str(target), "checkout", "--detach"],
                check=True,
                capture_output=True,
            )

            self._init_repo(source, "main", "source.txt", "source")
            write_results(
                inputs / "syslog/results.json",
                [{"name": "cpu", "value": 10, "unit": "%"}],
            )
            command = [
                sys.executable,
                str(script),
                "update-branch",
                "--repo-dir",
                str(target),
                "--benchmarks-branch",
                "benchmarks",
                "--config",
                str(config),
                "--new-results",
                str(inputs),
                "--source-repo",
                str(source),
                "--repo-url",
                "https://github.com/open-telemetry/otel-arrow",
            ]
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn(
                "Refusing to overwrite local-only commits",
                result.stderr,
            )
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(target), "rev-parse", "benchmarks"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                local_before,
            )
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(target), "rev-parse", "origin/benchmarks"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                self._head(seed),
            )

            forced = subprocess.run(
                [*command, "--force"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("without pushing", forced.stdout)
            local_after = subprocess.run(
                ["git", "-C", str(target), "rev-parse", "benchmarks"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertNotEqual(local_after, local_before)
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(target), "rev-parse", "benchmarks^"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                self._head(seed),
            )
            self.assertEqual(
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(target),
                        "merge-base",
                        "--is-ancestor",
                        local_before,
                        local_after,
                    ],
                    check=False,
                    capture_output=True,
                ).returncode,
                1,
            )
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(target), "rev-parse", "origin/benchmarks"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                self._head(seed),
            )

    # Scenario: A local build resolves the benchmarks branch and site from its config.
    # Guarantees: It builds from a temporary worktree and standardizes benchmark HTML.
    def test_build_uses_configured_temporary_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            remote = root / "remote.git"
            seed = root / "seed"
            source_repo = root / "source-repo"
            target = root / "target"
            output = root / "output"
            inputs = root / "inputs"
            script = TOOL_DIR / "dashboard.py"

            subprocess.run(
                ["git", "init", "--bare", str(remote)],
                check=True,
                capture_output=True,
            )
            self._init_repo(
                seed,
                "benchmarks",
                "docs/benchmarks/nightly/index.html",
                "nightly landing",
            )
            source_history = (
                seed / "docs/benchmarks/nightly/syslog/data.js"
            )
            write_history(source_history)
            source_history.with_name("index.html").write_text(
                "custom dashboard", encoding="utf-8"
            )
            passthrough_history = (
                seed / "docs/benchmarks/continuous-passthrough/data.js"
            )
            write_history(passthrough_history)
            passthrough_history.with_name("index.html").write_text(
                "passthrough dashboard", encoding="utf-8"
            )
            self._commit_all(seed, "add benchmark site")
            subprocess.run(
                ["git", "-C", str(seed), "remote", "add", "origin", str(remote)],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(seed), "push", "-u", "origin", "benchmarks"],
                check=True,
                capture_output=True,
            )

            self._init_repo(source_repo, "main", "source.txt", "source")
            self._init_repo(target, "main", "local.txt", "controller")
            subprocess.run(
                ["git", "-C", str(target), "remote", "add", "origin", str(remote)],
                check=True,
            )
            config = target / "configs/nightly.yaml"
            config.parent.mkdir()
            config.write_text(
                """
version: 1
target_branch: benchmarks
site_root: docs/benchmarks/nightly
migration_script: nightly_migrations.py
""".lstrip(),
                encoding="utf-8",
            )
            copy_nightly_migrations(config)
            self._commit_all(target, "add dashboard config")

            write_results(
                inputs / "syslog/results.json",
                [{"name": "cpu", "value": 10, "unit": "%"}],
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "build",
                    "--output-dir",
                    str(output),
                    "--new-results",
                    str(inputs),
                    "--config",
                    str(config),
                    "--source-repo",
                    str(source_repo),
                    "--repo-url",
                    "https://github.com/open-telemetry/otel-arrow",
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertIn("Applied migration 0001_move_legacy_passthrough", result.stdout)
            self.assertEqual(
                len(read_history(source_history)["entries"]["Benchmark"]),
                0,
            )
            output_history = output / "syslog/data.js"
            self.assertEqual(
                read_history(output_history)["entries"]["Benchmark"][-1]["benches"],
                [{"name": "cpu", "value": 10, "unit": "%"}],
            )
            self.assertEqual(
                output_history.with_name("index.html").read_text(encoding="utf-8"),
                (TOOL_DIR / "default_index.html").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                (output / "index.html").read_text(encoding="utf-8"),
                (TOOL_DIR / "configs/nightly/index.html").read_text(
                    encoding="utf-8"
                ),
            )
            self.assertTrue(
                (output / ".benchmark-history-dashboard-output").is_file()
            )
            self.assertEqual(
                (
                    output / "passthrough/index.html"
                ).read_text(encoding="utf-8"),
                (TOOL_DIR / "default_index.html").read_text(encoding="utf-8"),
            )
            self.assertTrue(passthrough_history.exists())
            worktrees = subprocess.run(
                ["git", "-C", str(target), "worktree", "list", "--porcelain"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertEqual(worktrees.count("worktree "), 1)

    # Scenario: Legacy and migrated passthrough directories both exist.
    # Guarantees: The migration fails without merging or overwriting either directory.
    def test_passthrough_migration_rejects_destination_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            worktree = Path(temp)
            source = (
                worktree
                / "docs/benchmarks/nightly/continuous-passthrough"
            )
            destination = (
                worktree
                / "docs/benchmarks/nightly/passthrough"
            )
            source.mkdir(parents=True)
            destination.mkdir(parents=True)
            (source / "source.txt").write_text("source", encoding="utf-8")
            (destination / "destination.txt").write_text(
                "destination", encoding="utf-8"
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(TOOL_DIR / "configs/nightly_migrations.py"),
                    "--worktree-root",
                    str(worktree),
                    "--site-root",
                    "docs/benchmarks/nightly",
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("destination already exists", result.stderr)
            self.assertEqual(
                (source / "source.txt").read_text(encoding="utf-8"),
                "source",
            )
            self.assertEqual(
                (destination / "destination.txt").read_text(encoding="utf-8"),
                "destination",
            )

    # Scenario: The benchmarks branch contains separate ClickHouse throughput and resource histories.
    # Guarantees: Migration combines matching commits, adds facets, removes both sources, and installs the canonical viewer.
    def test_migration_combines_clickhouse_and_adds_facets(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            worktree = Path(temp)
            site = worktree / "docs/benchmarks/nightly"
            shared_entry = {
                "commit": {"id": "abc123"},
                "date": 1,
                "tool": "customSmallerIsBetter",
            }
            combined_directory = site / "clickhouse"
            combined_directory.mkdir(parents=True)
            (combined_directory / "index.html").write_text(
                "legacy combined viewer",
                encoding="utf-8",
            )
            write_history(
                site / "clickhouse-throughput/data.js",
                [
                    {
                        **shared_entry,
                        "benches": [{
                            "name": "logs_produced_rate",
                            "value": 10,
                            "unit": "rows/sec",
                            "extra": (
                                "ClickHouse OTAP Logs/OTAP-IN-BATCHED-100K "
                                "- logs_produced"
                            ),
                        }],
                    },
                    {
                        **shared_entry,
                        "date": 2,
                        "benches": [{
                            "name": "logs_produced_rate",
                            "value": 11,
                            "unit": "rows/sec",
                        }],
                    },
                ],
            )
            write_history(
                site / "clickhouse-resources/data.js",
                [
                    {
                        **shared_entry,
                        "benches": [{
                            "name": "df-engine_ram_mib_max",
                            "value": 20,
                            "unit": "MiB",
                            "extra": (
                                "ClickHouse OTAP Logs/OTAP-IN-BATCHED-100K "
                                "- df-engine RAM"
                            ),
                        }],
                    },
                    {
                        **shared_entry,
                        "date": 2,
                        "benches": [{
                            "name": "df-engine_ram_mib_max",
                            "value": 21,
                            "unit": "MiB",
                        }],
                    },
                ],
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(TOOL_DIR / "configs/nightly_migrations.py"),
                    "--worktree-root",
                    str(worktree),
                    "--site-root",
                    "docs/benchmarks/nightly",
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            combined = read_history(site / "clickhouse/data.js")
            entries = combined["entries"]["Benchmark"]
            self.assertEqual(len(entries), 2)
            benches = entries[0]["benches"]
            self.assertEqual(len(benches), 2)
            self.assertEqual(
                benches[0]["facets"],
                {
                    "os": "linux",
                    "suite": "ClickHouse OTAP Logs",
                    "scenario": "OTAP-IN-BATCHED-100K",
                    "engine": "dfe",
                },
            )
            self.assertFalse((site / "clickhouse-throughput").exists())
            self.assertFalse((site / "clickhouse-resources").exists())
            self.assertEqual(
                (site / "clickhouse/index.html").read_bytes(),
                (TOOL_DIR / "default_index.html").read_bytes(),
            )
            self.assertIn(
                "Applied migration 0004_merge_split_clickhouse_dashboards",
                result.stdout,
            )
            self.assertIn(
                "Applied migration 0005_add_facets_to_histories",
                result.stdout,
            )
            self.assertIn(
                "Applied migration 0006_sync_dashboard_indexes",
                result.stdout,
            )

            no_op = subprocess.run(
                [
                    sys.executable,
                    str(TOOL_DIR / "configs/nightly_migrations.py"),
                    "--worktree-root",
                    str(worktree),
                    "--site-root",
                    "docs/benchmarks/nightly",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(no_op.stdout, "")

    # Scenario: Historical scaling summaries use a synthetic cores=aggregate facet.
    # Guarantees: Migration removes that facet while retaining concrete core counts.
    def test_migration_removes_synthetic_aggregate_core_facet(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            worktree = Path(temp)
            site = worktree / "docs/benchmarks/nightly"
            data_js = site / "scaling-efficiency/data.js"
            write_history(
                data_js,
                [{
                    "commit": {"id": "abc123"},
                    "date": 1,
                    "tool": "customBiggerIsBetter",
                    "benches": [
                        {
                            "name": "otap_scaling_efficiency_avg",
                            "value": 0.9,
                            "unit": "",
                            "facets": {
                                "os": "linux",
                                "protocol": "otap",
                                "cores": "aggregate",
                            },
                        },
                        {
                            "name": "otap_scaling_efficiency_2_cores",
                            "value": 0.8,
                            "unit": "",
                            "facets": {
                                "os": "linux",
                                "protocol": "otap",
                                "cores": "2",
                            },
                        },
                    ],
                }],
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(TOOL_DIR / "configs/nightly_migrations.py"),
                    "--worktree-root",
                    str(worktree),
                    "--site-root",
                    "docs/benchmarks/nightly",
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            benches = read_history(data_js)["entries"]["Benchmark"][0]["benches"]
            self.assertNotIn("cores", benches[0]["facets"])
            self.assertEqual(benches[1]["facets"]["cores"], "2")
            self.assertIn(
                "Applied migration 0005_add_facets_to_histories",
                result.stdout,
            )
            viewer = data_js.with_name("index.html").read_text(encoding="utf-8")
            self.assertIn(
                "record.facets[key] === undefined ||",
                viewer,
            )
            self.assertIn(
                "selectedValues.has(record.facets[key])",
                viewer,
            )
            self.assertIn('checkbox.type = "checkbox"', viewer)

    # Scenario: Existing continuous and binary-size histories use their custom legacy pages.
    # Guarantees: Their migrations add useful facets and install the canonical viewer independently.
    def test_additional_dashboard_migrations(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            worktree = Path(temp)
            site = worktree / "docs/benchmarks"
            continuous = site / "continuous/data.js"
            binary_size = site / "binary-size/data.js"
            write_history(
                continuous,
                [{
                    "commit": {"id": "continuous"},
                    "date": 1,
                    "tool": "customSmallerIsBetter",
                    "benches": [{
                        "name": "cpu_percentage_normalized_avg",
                        "value": 50,
                        "unit": "%",
                        "extra": "CI 100kLRPS/OTAP-ATTR-OTLP - CPU",
                    }],
                }],
            )
            write_history(
                binary_size,
                [{
                    "commit": {"id": "binary"},
                    "date": 1,
                    "tool": "customSmallerIsBetter",
                    "benches": [{
                        "name": "linux-arm64-crate-arrow_array",
                        "value": 3.5,
                        "unit": "MB",
                    }],
                }],
            )
            continuous.with_name("index.html").write_text(
                "legacy continuous",
                encoding="utf-8",
            )
            binary_size.with_name("index.html").write_text(
                "legacy binary",
                encoding="utf-8",
            )

            for script in (
                "continuous_migrations.py",
                "binary_size_migrations.py",
            ):
                subprocess.run(
                    [
                        sys.executable,
                        str(TOOL_DIR / f"configs/{script}"),
                        "--worktree-root",
                        str(worktree),
                        "--site-root",
                        "docs/benchmarks",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )

            continuous_bench = read_history(continuous)[
                "entries"
            ]["Benchmark"][0]["benches"][0]
            self.assertEqual(
                continuous_bench["facets"],
                {
                    "os": "linux",
                    "suite": "CI 100kLRPS",
                    "scenario": "OTAP-ATTR-OTLP",
                    "engine": "dfe",
                },
            )
            binary_bench = read_history(binary_size)[
                "entries"
            ]["Benchmark"][0]["benches"][0]
            self.assertEqual(
                binary_bench["facets"],
                {
                    "os": "linux",
                    "architecture": "arm64",
                    "measurement": "crate",
                    "crate": "arrow_array",
                },
            )
            template = (TOOL_DIR / "default_index.html").read_bytes()
            self.assertEqual(
                continuous.with_name("index.html").read_bytes(),
                template,
            )
            self.assertEqual(
                binary_size.with_name("index.html").read_bytes(),
                template,
            )

    # Scenario: The benchmarks branch landing page is missing, stale, or current.
    # Guarantees: Migration creates and replaces it, then becomes a byte-for-byte no-op.
    def test_nightly_index_migration_synchronizes_template(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            worktree = Path(temp)
            site_root = worktree / "docs/benchmarks/nightly"
            destination = site_root / "index.html"
            template = TOOL_DIR / "configs/nightly/index.html"
            command = [
                sys.executable,
                str(TOOL_DIR / "configs/nightly_migrations.py"),
                "--worktree-root",
                str(worktree),
                "--site-root",
                "docs/benchmarks/nightly",
            ]

            created = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("Applied migration 0002_sync_nightly_index", created.stdout)
            self.assertEqual(destination.read_bytes(), template.read_bytes())

            destination.write_text("stale landing page", encoding="utf-8")
            replaced = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("Applied migration 0002_sync_nightly_index", replaced.stdout)
            self.assertEqual(destination.read_bytes(), template.read_bytes())

            current = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(current.stdout, "")
            content = destination.read_text(encoding="utf-8")
            self.assertIn('href="clickhouse/"', content)
            self.assertNotIn(
                "https://open-telemetry.github.io/otel-arrow/benchmarks/nightly/",
                content,
            )

    # Scenario: A build omits benchmark inputs while a migration is pending.
    # Guarantees: The output contains migrated history without adding a history entry.
    def test_build_without_results_runs_migrations_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            remote = root / "remote.git"
            seed = root / "seed"
            target = root / "target"
            output = root / "output"
            script = TOOL_DIR / "dashboard.py"

            subprocess.run(
                ["git", "init", "--bare", str(remote)],
                check=True,
                capture_output=True,
            )
            self._init_repo(
                seed,
                "benchmarks",
                "docs/benchmarks/nightly/index.html",
                "nightly landing",
            )
            legacy_history = (
                seed / "docs/benchmarks/continuous-passthrough/data.js"
            )
            write_history(legacy_history)
            self._commit_all(seed, "add benchmark site")
            subprocess.run(
                ["git", "-C", str(seed), "remote", "add", "origin", str(remote)],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(seed), "push", "-u", "origin", "benchmarks"],
                check=True,
                capture_output=True,
            )

            self._init_repo(target, "main", "local.txt", "controller")
            subprocess.run(
                ["git", "-C", str(target), "remote", "add", "origin", str(remote)],
                check=True,
            )
            config = target / "configs/nightly.yaml"
            config.parent.mkdir()
            config.write_text(
                """
version: 1
target_branch: benchmarks
site_root: docs/benchmarks/nightly
migration_script: nightly_migrations.py
""".lstrip(),
                encoding="utf-8",
            )
            copy_nightly_migrations(config)
            self._commit_all(target, "add dashboard config")

            result = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "build",
                    "--output-dir",
                    str(output),
                    "--config",
                    str(config),
                    "--source-repo",
                    str(root / "does-not-exist"),
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            migrated = output / "passthrough/data.js"
            self.assertIn(
                "Built dashboard site without merging new results",
                result.stdout,
            )
            self.assertTrue(migrated.is_file())
            self.assertEqual(
                (output / "index.html").read_bytes(),
                (TOOL_DIR / "configs/nightly/index.html").read_bytes(),
            )
            self.assertEqual(
                read_history(migrated)["entries"]["Benchmark"],
                [],
            )
            self.assertTrue(legacy_history.is_file())

    # Scenario: An update omits benchmark inputs while a migration is pending.
    # Guarantees: The local benchmarks branch gains only the migration commit.
    def test_update_branch_without_results_commits_migrations_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            remote = root / "remote.git"
            seed = root / "seed"
            target = root / "target"
            script = TOOL_DIR / "dashboard.py"

            subprocess.run(
                ["git", "init", "--bare", str(remote)],
                check=True,
                capture_output=True,
            )
            self._init_repo(
                seed,
                "benchmarks",
                "docs/benchmarks/nightly/index.html",
                "nightly landing",
            )
            legacy = seed / "docs/benchmarks/continuous-passthrough/index.html"
            legacy.parent.mkdir(parents=True)
            legacy.write_text("legacy passthrough", encoding="utf-8")
            self._commit_all(seed, "add benchmark site")
            subprocess.run(
                ["git", "-C", str(seed), "remote", "add", "origin", str(remote)],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(seed), "push", "-u", "origin", "benchmarks"],
                check=True,
                capture_output=True,
            )
            remote_head = self._head(seed)

            self._init_repo(target, "main", "local.txt", "controller")
            subprocess.run(
                ["git", "-C", str(target), "remote", "add", "origin", str(remote)],
                check=True,
            )
            config = target / "configs/nightly.yaml"
            config.parent.mkdir()
            config.write_text(
                """
version: 1
target_branch: benchmarks
site_root: docs/benchmarks/nightly
migration_script: nightly_migrations.py
""".lstrip(),
                encoding="utf-8",
            )
            copy_nightly_migrations(config)
            self._commit_all(target, "add dashboard config")
            subprocess.run(
                ["git", "-C", str(target), "config", "user.name", "Local Developer"],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(target),
                    "config",
                    "user.email",
                    "local@example.com",
                ],
                check=True,
            )
            source_sha = self._head(target)
            remarks = "Triggered by the nightly benchmark workflow."
            patch_output = root / "dashboard.patch"

            result = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "update-branch",
                    "--config",
                    str(config),
                    "--source-repo",
                    str(target),
                    "--additional-commit-remarks",
                    remarks,
                    "--patch-output",
                    str(patch_output),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            local_head = subprocess.run(
                ["git", "-C", str(target), "rev-parse", "benchmarks"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            message = subprocess.run(
                ["git", "-C", str(target), "log", "-1", "--format=%B", "benchmarks"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.rstrip()
            author = subprocess.run(
                [
                    "git",
                    "-C",
                    str(target),
                    "log",
                    "-1",
                    "--format=%an <%ae>",
                    "benchmarks",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertIn("without pushing", result.stdout)
            self.assertIn("Wrote Git patch", result.stdout)
            self.assertTrue(patch_output.is_file())
            self.assertIn(
                "Apply docs/benchmarks/nightly migration",
                patch_output.read_text(encoding="utf-8"),
            )
            self.assertNotEqual(local_head, remote_head)
            self.assertEqual(author, "Local Developer <local@example.com>")
            self.assertEqual(
                message,
                (
                    "Apply docs/benchmarks/nightly migration for "
                    f"{source_sha[:8]}\n\n"
                    "This is an automated commit generated by the "
                    "benchmark_history_dashboard tool.\n"
                    f"Source commit: {source_sha}\n"
                    "Configuration: configs/nightly.yaml\n\n"
                    f"{remarks}"
                ),
            )
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(target), "rev-parse", "origin/benchmarks"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                remote_head,
            )
            self.assertEqual(
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(target),
                        "show",
                        "benchmarks:docs/benchmarks/nightly/"
                        "passthrough/index.html",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout,
                "legacy passthrough",
            )

            patch_checkout = root / "patch-checkout"
            subprocess.run(
                [
                    "git",
                    "clone",
                    "--branch",
                    "benchmarks",
                    str(remote),
                    str(patch_checkout),
                ],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(patch_checkout), "config", "user.name", "Reviewer"],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(patch_checkout),
                    "config",
                    "user.email",
                    "reviewer@example.com",
                ],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(patch_checkout), "am", str(patch_output)],
                check=True,
                capture_output=True,
            )
            self.assertTrue(
                (
                    patch_checkout
                    / "docs/benchmarks/nightly/passthrough/index.html"
                ).is_file()
            )

            subprocess.run(
                [
                    "git",
                    "-C",
                    str(target),
                    "branch",
                    "--force",
                    "benchmarks",
                    "origin/benchmarks",
                ],
                check=True,
                capture_output=True,
            )
            push_command = [
                sys.executable,
                str(script),
                "update-branch",
                "--config",
                str(config),
                "--push",
                "--retry-delay",
                "0",
                "--source-repo",
                str(target),
            ]
            pushed = subprocess.run(
                push_command,
                check=True,
                capture_output=True,
                text=True,
            )
            pushed_head = subprocess.run(
                ["git", "--git-dir", str(remote), "rev-parse", "benchmarks"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertIn("Published dashboard migrations.", pushed.stdout)
            self.assertNotEqual(pushed_head, remote_head)

            patch_output.write_text("stale patch", encoding="utf-8")
            no_patch = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "update-branch",
                    "--config",
                    str(config),
                    "--source-repo",
                    str(target),
                    "--patch-output",
                    str(patch_output),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("No dashboard migrations to apply.", no_patch.stdout)
            self.assertFalse(patch_output.exists())

            no_op = subprocess.run(
                push_command,
                check=True,
                capture_output=True,
                text=True,
            )
            remote_after_no_op = subprocess.run(
                ["git", "--git-dir", str(remote), "rev-parse", "benchmarks"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertIn("No dashboard migrations to apply.", no_op.stdout)
            self.assertEqual(remote_after_no_op, pushed_head)

    # Scenario: A migration config is outside every Git worktree.
    # Guarantees: The generated commit body records its absolute filesystem path.
    def test_migration_commit_message_uses_absolute_external_config_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp).resolve() / "nightly.yaml"
            config.write_text("version: 1\n", encoding="utf-8")

            message = migration_commit_message(
                Path("docs/benchmarks/nightly"),
                "0123456789abcdef",
                config,
                None,
            )

            self.assertEqual(
                message,
                (
                    "Apply docs/benchmarks/nightly migration for "
                    "01234567\n\n"
                    "This is an automated commit generated by the "
                    "benchmark_history_dashboard tool.\n"
                    "Source commit: 0123456789abcdef\n"
                    f"Configuration: {config}"
                ),
            )

    # Scenario: Benchmark results produce the default automated commit message.
    # Guarantees: Its title and body identify the site, source, config, and caller remarks.
    def test_benchmark_results_commit_message_matches_migration_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp).resolve() / "nightly.yaml"
            config.write_text("version: 1\n", encoding="utf-8")
            remarks = "Triggered by the nightly benchmark workflow."

            message = benchmark_results_commit_message(
                Path("docs/benchmarks/nightly"),
                "0123456789abcdef",
                config,
                remarks,
            )

            self.assertEqual(
                message,
                (
                    "Update docs/benchmarks/nightly benchmark results for "
                    "01234567\n\n"
                    "This is an automated commit generated by the "
                    "benchmark_history_dashboard tool.\n"
                    "Source commit: 0123456789abcdef\n"
                    f"Configuration: {config}\n\n"
                    f"{remarks}"
                ),
            )

    # Scenario: A local build targets an existing directory not created by this tool.
    # Guarantees: The build refuses replacement and explains the required marker file.
    def test_build_refuses_unmarked_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_repo = root / "source-repo"
            output = root / "important-files"
            inputs = root / "inputs"
            config = root / "config.yaml"
            script = TOOL_DIR / "dashboard.py"
            config.write_text(
                "version: 1\nsite_root: docs/benchmarks/nightly\n",
                encoding="utf-8",
            )
            self._init_repo(source_repo, "main", "source.txt", "source")
            output.mkdir()
            (output / "keep.txt").write_text("important", encoding="utf-8")
            write_results(
                inputs / "syslog/results.json",
                [{"name": "cpu", "value": 10, "unit": "%"}],
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "build",
                    "--output-dir",
                    str(output),
                    "--new-results",
                    str(inputs),
                    "--config",
                    str(config),
                    "--source-repo",
                    str(source_repo),
                    "--repo-url",
                    "https://github.com/open-telemetry/otel-arrow",
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn(f"ERROR: Output directory {output}", result.stderr)
            self.assertIn(
                "For safety this tool doesn't overwrite directories unless they "
                "contain the file .benchmark-history-dashboard-output",
                result.stderr,
            )
            self.assertEqual(
                (output / "keep.txt").read_text(encoding="utf-8"),
                "important",
            )

    # Scenario: A state-changing command is invoked without dashboard configuration.
    # Guarantees: Argparse reports --config as a required command argument.
    def test_build_and_update_branch_require_config(self) -> None:
        script = TOOL_DIR / "dashboard.py"

        for command in ("build", "update-branch"):
            with self.subTest(command=command):
                result = subprocess.run(
                    [sys.executable, str(script), command],
                    check=False,
                    capture_output=True,
                    text=True,
                )

                self.assertEqual(result.returncode, 2)
                self.assertIn(
                    f"dashboard.py {command}: error: "
                    "the following arguments are required: --config",
                    result.stderr,
                )

    @staticmethod
    def _commit_all(repo: Path, message: str) -> None:
        subprocess.run(["git", "-C", str(repo), "add", "--all"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.name=Benchmark Test",
                "-c",
                "user.email=benchmark@example.com",
                "commit",
                "-m",
                message,
            ],
            check=True,
            capture_output=True,
        )

    @classmethod
    def _init_repo(
        cls,
        repo: Path,
        branch: str,
        relative_file: str,
        content: str,
    ) -> None:
        subprocess.run(
            ["git", "init", "-b", branch, str(repo)],
            check=True,
            capture_output=True,
        )
        path = repo / relative_file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        cls._commit_all(repo, "initial commit")

    @staticmethod
    def _head(repo: Path) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()


if __name__ == "__main__":
    unittest.main()
