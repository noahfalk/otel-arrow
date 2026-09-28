# Benchmark History Dashboard

Repository-owned tooling for building and publishing the existing
`github-action-benchmark` history dashboard format.

The public entrypoint has four commands:

```text
dashboard.py run
dashboard.py mock-run
dashboard.py build
dashboard.py update-branch
```

All commands require `--config`.

## Routing

Every result JSON file must be below a first-level benchmark directory. All
descendant JSON arrays are combined into one history entry:

```text
<new-results>/<bucket>/**/*.json
    -> <site-root>/<bucket>/data.js
```

JSON files directly under `--new-results` are rejected because they do not name
a benchmark. The outermost directory is the routing convention; there is no
separate source-to-destination mapping.

When appending to an existing dashboard, the updater preserves the `tool` value
from its most recent history entry. A newly created dashboard starts with
`customSmallerIsBetter`.

The config provides branch defaults, benchmark execution inventory, and mock
metric definitions:

```yaml
version: 1
target_branch: benchmarks
site_root: docs/benchmarks/nightly
migration_script: nightly_migrations.py
orchestrator_root: tools/pipeline_perf_test

metric_sets:
  standard_logs:
    - name: cpu_percentage_normalized_avg
      unit: "%"
      mock: {min: 20, max: 70}

benchmarks:
  - name: filter
    facets: {os: linux}
    metric_sets: [standard_logs]
    orchestrators:
      - name: dfe
        path: test_suites/integration/nightly/filter-docker.yaml
        facets: {engine: dfe}
      - name: otelcol
        path: test_suites/integration/nightly/otelcol-docker.yaml
        facets: {engine: otelcol}
```

`site_root` is required. `target_branch` supplies the default benchmarks branch
for both `build` and `update-branch`. Explicit `--benchmarks-branch` and
`--site-root` arguments override the configured values.

`migration_script` is optional and resolved relative to the config file. When
present, the dashboard invokes it as an isolated Python subprocess:

```text
python <migration-script> \
  --worktree-root <temporary-target-worktree> \
  --site-root <configured-relative-site-root>
```

Migrations run in the temporary target worktree before new results are merged.
They must be idempotent because push retries discard the rejected worktree and
run migrations again against the latest remote branch. A nonzero exit code
aborts the operation.

Three production configurations mirror the existing historical publishers:

| Config | Bucket | Real result producer |
| --- | --- | --- |
| `configs/nightly.yaml` | `docs/benchmarks/nightly/*` | Nightly pipeline performance orchestrators and derived scaling scripts |
| `configs/continuous.yaml` | `docs/benchmarks/continuous` | `continuous/100klrps-docker.yaml` orchestrator |
| `configs/binary-size.yaml` | `docs/benchmarks/binary-size` | Multi-architecture Docker builds and `cargo bloat` in `dataflow-engine-binary-size.yml` |

The continuous and binary-size configurations use `site_root: docs/benchmarks`
because their bucket name is already the final directory below that shared
root. Their migrations modify only their named dashboard.

## Running benchmarks

`run` executes configured orchestrators from `orchestrator_root`. With no
positional benchmark names it runs every configured orchestrator; names can be
provided to run a subset:

```text
python dashboard.py run filter clickhouse --config <config>
```

For example, the continuous dashboard has one directly runnable producer:

```text
python dashboard.py run --config configs/continuous.yaml
```

Binary-size does not declare an orchestrator because its real producer is a
multi-runner Docker and Rust build matrix. Use `mock-run` for representative
fixed binary and text-size metrics, or place the workflow's consolidated JSON
under `<new-results>/binary-size/` before `build` or `update-branch`. Dynamic
top-crate measurements from `cargo bloat` are accepted and facetized from their
metric names, but are intentionally not guessed by `mock-run`.

Each orchestrator may have an optional name and `tests` list. A configured list
is passed as the orchestrator's comma-separated `--tests` argument; when
omitted, no `--tests` argument is supplied.

Benchmarks and orchestrators may also declare string-valued `facets`. The
collector merges benchmark facets, orchestrator facets, explicit result facets,
and reliable dimensions inferred from the existing
`<suite>/<scenario> - <description>` report label. More-specific configured
facets override inferred values, and explicit record facets override configured
defaults. The production configuration assigns at least `os` and `engine` to
every execution.

The command detects new or updated
`results/**/gh-actions-benchmark/*.json` files after each orchestrator and
copies them without collisions beneath:

```text
<new-results>/<benchmark-name>/<orchestrator-name>/<original-results-path>
```

Collected records use the additive result shape:

```json
{
  "name": "cpu_percentage_normalized_avg",
  "value": 42,
  "unit": "%",
  "extra": "Nightly - Filter/Logs-OTAP-OTLP - CPU % (Normalized)",
  "facets": {
    "os": "linux",
    "engine": "dfe",
    "signal": "logs",
    "suite": "Nightly - Filter",
    "scenario": "Logs-OTAP-OTLP"
  }
}
```

`extra` remains supported for compatibility with the existing
`github-action-benchmark` publication path. The repository-owned viewer groups
and filters by `facets`, not by parsing `extra`.

`orchestrator_root` is relative to the repository root. The config file's Git
worktree supplies that root unless `run --repo-dir` overrides it.

## Generating mock results

`mock-run` generates fake result values from each benchmark's referenced
`metric_sets`:

```text
python dashboard.py mock-run filter clickhouse --config <config>
```

With no positional names, all configured benchmark metric sets are generated.
Values vary by default; optional `--seed` makes a run reproducible.
`--facet KEY=VALUE` overrides a facet on every generated record and may be
repeated. `--result-group NAME` nests files beneath `<benchmark>/<name>/`,
allowing independently generated matrix results to merge without filename
collisions. Both `run` and `mock-run` create a persistent temporary output
directory when `--new-results` is omitted and print its path.

For example:

```text
python dashboard.py mock-run --config configs/nightly.yaml \
  --new-results benchmark-results \
  --result-group windows \
  --facet os=windows
```

`--new-results` is optional for `build` and `update-branch`. When omitted, the
command runs configured migrations but skips source commit metadata lookup and
history merging.

When a destination does not already have `index.html`, the updater creates the
canonical aggregate dashboard page. Existing HTML is never overwritten by a
history update; migrations synchronize existing benchmark pages to the
canonical viewer.

## Build locally

`build` fetches and safely synchronizes the configured benchmarks branch, creates
a temporary detached worktree, runs migrations there, and copies the migrated
nightly site into the output before applying benchmark inputs. It does not
commit or push. The output directory defaults to `.site`, whose root contains
the nightly landing `index.html`. Existing output is replaced only when it was
created by this tool.

The preferred fetch remote defaults to `origin`. If the benchmarks branch does
not exist there, the command falls back to `upstream/<benchmarks-branch>`.

```powershell
python -m pip install -r tools\benchmark_history_dashboard\requirements.txt

python tools\benchmark_history_dashboard\dashboard.py mock-run `
  --new-results benchmark-input `
  --config tools\benchmark_history_dashboard\configs\nightly.yaml

python tools\benchmark_history_dashboard\dashboard.py build `
  --new-results benchmark-input `
  --config tools\benchmark_history_dashboard\configs\nightly.yaml

# Build the existing site after applying migrations only.
python tools\benchmark_history_dashboard\dashboard.py build `
  --config tools\benchmark_history_dashboard\configs\nightly.yaml

python -m http.server 8000 --directory .site
```

The updater validates every input and existing history before replacing any
`data.js` file. Reapplying identical metrics for the same commit is a no-op.

## Updating and publishing a branch

`update-branch` fetches the latest benchmarks branch into a temporary worktree,
runs migrations, merges new results, and advances the named local branch. If
migrations change files, they are committed separately before the
benchmark-history commit with a title such as:

```text
Apply docs/benchmarks/nightly migration for <8-character-source-sha>
```

The benchmark-history commit uses a matching title and body:

```text
Update docs/benchmarks/nightly benchmark results for <8-character-source-sha>
```

Both commit bodies identify `benchmark_history_dashboard` as the generator and
record the full source SHA and config path. The path is relative when the config
is inside a Git worktree and absolute otherwise.
`--additional-commit-remarks` appends optional caller-supplied context after a
blank line to each generated commit. The SHA is resolved from `--source-ref` in
`--source-repo`, including migration-only updates. `--commit-message` remains
an exact override for the benchmark-history commit.

Generated commits use Git's normal repository or global `user.name` and
`user.email` configuration. CI callers can explicitly set
`--git-user-name` and `--git-user-email`; the tool does not assume a bot
identity.

It does not push unless `--push` is specified. With `--push`, a rejected
non-fast-forward update is retried from the latest branch head. Pushes always
target `origin/<benchmarks-branch>`, even when the initial branch state was
fetched from `upstream`. `--site-root` is relative to the benchmarks branch
checkout and defaults to the required config value.

The nightly migration moves legacy passthrough history to:

```text
docs/benchmarks/continuous-passthrough
    -> docs/benchmarks/nightly/passthrough
```

It also renames a transitional
`docs/benchmarks/nightly/continuous-passthrough` directory to `passthrough` and
combines the historical `clickhouse-throughput` and `clickhouse-resources`
entries into one `clickhouse` dashboard before deleting the split directories.
It then adds reliable facets to every historical measurement and synchronizes
every individual benchmark page to the canonical aggregate viewer. These
migrations are idempotent and fail rather than merge conflicting passthrough
directories.

The canonical viewer loads relative `data.js`, renders one aggregate chart per
metric, groups lines by facet combination, and creates a filter for every facet
present in the history. Filters appear as checkbox groups in a left-hand panel.
With no values checked, a facet is unrestricted. With one or more values
checked, the viewer includes matching measurements and measurements where that
facet is not applicable. For example, selecting `cores=1` retains scaling
summary metrics that do not define `cores`. Scenario-specific duplicate charts
are not generated.

The canonical nightly landing page is stored at
`configs/nightly/index.html`. Every migration run synchronizes that file to
`<site_root>/index.html`, replacing a stale copy and doing nothing when the
files are byte-for-byte identical. Links to dashboards within the nightly site
are relative so they resolve against both GitHub Pages and a locally hosted
static site. Links outside the nightly site remain absolute.

When `--repo-dir` is omitted, the command asks Git for the worktree root
containing the config file. This supports normal repositories, linked
worktrees, and subdirectories because Git handles both `.git` directories and
`.git` metadata files. If the config is outside a Git worktree, `--repo-dir`
remains required.

After fetching, the local branch must either match the remote branch or be
strictly behind it. A behind branch is fast-forwarded. The command refuses to
continue when the local branch has commits absent from the remote, including
diverged history, so it never overwrites or unexpectedly pushes local work.
Use `--force` to intentionally reset such local history to the fetched remote
branch before generating the new commit.

Without `--push`, `--patch-output <path>` writes an mbox patch containing all
generated migration and history commits. The patch is generated with
`git format-patch --binary` against the exact fetched branch head. If the
operation is a no-op, no patch file is retained. `--patch-output` cannot be
combined with `--push`.

```powershell
python tools\benchmark_history_dashboard\dashboard.py update-branch `
  --push `
  --new-results benchmark-input `
  --config tools\benchmark_history_dashboard\configs\nightly.yaml

# Commit migrations locally without merging new benchmark results.
python tools\benchmark_history_dashboard\dashboard.py update-branch `
  --config tools\benchmark_history_dashboard\configs\nightly.yaml
```

Add `--push` to publish the generated commit. Without it, the named local
branch points to the new commit while the remote branch remains unchanged.
The named branch must not be checked out in another worktree because Git will
not allow its ref to be advanced.

## Experimental pull-request evaluation

`.github/workflows/benchmark-dashboard-experimental.yml` runs automatically
when a pull request is opened, reopened, or updated. It uses only read
permissions and standard GitHub-hosted runners.

Windows and Linux matrix jobs generate nightly mock results with distinct OS
facets and collision-free result groups. A final Linux job merges those
artifacts, builds the candidate site, creates the non-pushing benchmarks-branch
patch, and uploads both outputs. Its job summary links directly to the
artifacts and documents local static-site serving and `git am` inspection.
