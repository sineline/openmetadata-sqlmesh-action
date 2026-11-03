# SQLMesh to OpenMetadata

An installable Python package and reusable GitHub Action for SQLMesh metadata, table and column lineage, model execution status, and audit results in OpenMetadata.

The package calls OpenMetadata's native REST API. It runs independently of Dionysus, Airflow, Marquez, dbt artifacts, and the OpenMetadata ingestion SDK. The core library has no third-party dependencies; SQLMesh extraction and runtime observation use an optional extra.

## Install and try it

From this repository:

```bash
python -m pip install '.[sqlmesh]'
openmetadata-sqlmesh --help
openmetadata-sqlmesh sync --project examples/sqlmesh --config examples/openmetadata-sqlmesh.json --dry-run
```

The example is a three-model DuckDB project. A dry run loads definitions and emits deterministic JSON without contacting OpenMetadata, running a plan, or querying warehouse data. Project configuration and macros execute normally during SQLMesh project loading; use projects you trust.

To install from Git in another project:

```bash
python -m pip install 'openmetadata-sqlmesh[sqlmesh] @ git+https://github.com/sineline/openmetadata-sqlmesh-action.git@<ref>'
```

Replace `<ref>` with the branch or commit containing this implementation. Pin an immutable commit for reproducibility. The package is not published to PyPI.

## Configuration and synchronization

Create `openmetadata-sqlmesh.json` in your project:

```json
{
  "url": "https://metadata.example.com",
  "database_service": "warehouse",
  "pipeline_service": "sqlmesh",
  "default_database": "analytics",
  "default_schema": "public",
  "catalog_services": {"other_catalog": "other_warehouse"},
  "token_env": "OPENMETADATA_TOKEN",
  "timeout": 10,
  "retries": 2
}
```

Database services must already exist in OpenMetadata. The package creates databases, schemas, model tables, and a `CustomPipeline` service as needed; it does not configure warehouse connections. The API identity needs read access to services, catalog entities, and lineage, plus permission to update the relevant entities, lineage, and test results.

Defaults fill missing database/schema components; fully qualified model names do not need them. `catalog_services` maps SQL catalog names to existing services for cross-service dependencies. Case and quoted component boundaries are preserved: `db.schema."order.items"` maps to `warehouse.db.schema."order.items"`.

Keep tokens out of configuration files. Export the variable named by `token_env`; `OPENMETADATA_AUTH_TOKEN` is a fallback. `OPENMETADATA_URL` overrides the configured URL. Root URLs, `/api`, `/api/v1`, and legacy `/api/v1/lineage` URLs are accepted.

```bash
export OPENMETADATA_TOKEN='your-jwt'
openmetadata-sqlmesh sync --project . --config openmetadata-sqlmesh.json --dry-run --output metadata.json
openmetadata-sqlmesh sync --project . --config openmetadata-sqlmesh.json
```

Use `--gateway name` for projects with multiple gateways. Install project-specific connection drivers and macro dependencies in the same environment.

Sync publishes model/column descriptions, column types, SQL, pipelines, table dependencies, and resolvable column lineage. Repeated runs update metadata without deleting entities or pruning lineage. Existing edge details and column mappings are read and merged before updates; a failed lineage read prevents replacement. Missing external tables appear in `warnings`. Unresolved model columns reuse existing catalog schemas. Partially unresolved complex types retain the matching known type as a whole, with a warning. No placeholder column overwrites an existing schema.

Output is JSON. Exit codes: `0` success (possibly with warnings), `1` synchronization/publication errors, `2` invalid configuration/input or invocation failure. `--output` writes JSON to a file. A dry run describes extracted metadata and relationships, not warehouse execution results.

## Python API and runtime observer

```python
from sqlmesh import Context
from openmetadata_sqlmesh import Config, sync_context

config = Config.from_file("openmetadata-sqlmesh.json")
context = Context(paths="my_project")
try:
    report = sync_context(context, config)
    if report.errors:
        raise RuntimeError("; ".join(report.errors))
finally:
    context.close()
```

`extract_context(context, config)` returns `ProjectMetadata` / `ModelMetadata` records. `load_project(path, config, gateway=None)` loads, extracts, and closes a Context. `Synchronizer(config, client=None).sync(metadata)` accepts pre-extracted records. `sync_context(..., dry_run=True)` returns a JSON-compatible dictionary without API calls.

To report runtime status, install the observer **before constructing the SQLMesh Context**, for example at the start of the consumer's `config.py`:

```python
from openmetadata_sqlmesh import Config, install

install(Config.from_file("openmetadata-sqlmesh.json"))
```

The observer wraps the current Console and delegates ordinary output. It publishes pipeline status and upstream/output lineage on terminal callbacks. Concurrent batches retain separate execution records and distinct status timestamps. Query errors, audit failures, and interrupted evaluations are marked failed. Aggregate audit counters do not fabricate individual audit results.

Reporting failures log warnings and SQLMesh work continues by default. `install(config, strict=True)` raises reporting errors after console delegation. Reinstalling the same configuration is safe; use `uninstall()` before changing configuration. `uninstall()` restores the wrapped console. Synchronize metadata first so upstream tables exist for runtime lineage.

## Audit results

This package reports supplied results; it does not execute warehouse-specific contracts or assume a declared audit ran.

```python
from openmetadata_sqlmesh import AuditRecord, Config, publish_audits

config = Config.from_file("openmetadata-sqlmesh.json")
records = [AuditRecord(
    model="analytics.public.orders",
    audit="not_null_id",
    timestamp=1793657000000,  # Actual execution time in epoch milliseconds.
    query="SELECT * FROM analytics.public.orders WHERE id IS NULL",
    count=0,  # Failing rows from that execution.
)]
report = publish_audits(records, config)
```

For real model-scoped SQLMesh `AuditResult` objects returned by the snapshot evaluator, use `AuditRecord.from_sqlmesh(result, timestamp=execution_timestamp_ms)`. This preserves query, count, skipped status, and audit-argument identity. SQLMesh's `Context.audit()` is an execution command, not a source of returned result objects.

The CLI accepts a JSON array of the same record fields:

```bash
openmetadata-sqlmesh audit-results examples/audit-results.json --config openmetadata-sqlmesh.json --dry-run
openmetadata-sqlmesh audit-results results.json --config openmetadata-sqlmesh.json
```

Zero failing rows maps to `Success`, positive counts to `Failed`, and unavailable counts to `Aborted`. Explicit statuses are accepted when consistent with supplied counts. Model tables must exist. Executable suites and query-backed test cases are provisioned idempotently. A skipped audit without a query can update an existing case, but cannot invent a new query-backed case.

## GitHub Action

Check out the consumer project and install its additional drivers/macros, then use a ref containing this implementation:

```yaml
permissions:
  contents: read

steps:
  - uses: actions/checkout@v4
  - uses: actions/setup-python@v5
    with:
      python-version: '3.11'
  - uses: sineline/openmetadata-sqlmesh-action@<ref>
    id: metadata
    with:
      project-path: .
      openmetadata-url: https://metadata.example.com
      database-service: warehouse
      openmetadata-token: ${{ secrets.OPENMETADATA_TOKEN }}
      sqlmesh-version: '0.236.3'
      dry-run: 'true'
```

Set `dry-run` to `'false'` to publish. Optional inputs: `pipeline-service`, `default-database`, `default-schema`, `catalog-services` (JSON), `gateway`, `python-version`. `report-path` points to the generated JSON report. Inputs pass through environment variables and quoted arguments; tokens are not written to configuration artifacts.

## Compatibility and development

- Python 3.10+; CI covers 3.10 and 3.12.
- SQLMesh `>=0.231.1,<0.237`; real extraction/evaluation checks cover 0.231.1 and 0.236.3. The core API/configuration/audit JSON functions work without SQLMesh.
- Native API contracts are checked against OpenMetadata 1.6.6 and 1.12.0. Audit publication handles the older required `testSuite` field and result route, plus the newer endpoint. Tests use local API fakes, not a live OpenMetadata deployment.
- Embedded models and dynamic/Python-only transformations can limit metadata resolution. Warnings report unavailable information.
- TLS verification stays enabled; redirects are rejected. Only 404 means missing. Transport errors, 429, and 5xx have bounded retries (`retries` means additional attempts); 401/403 are permanent failures.

```bash
python -m pip install '.[dev,sqlmesh]'
python -m pytest -q
python -m build
```

CI checks the dependency-free core, CLI, local HTTP contracts, concurrent callbacks, and real DuckDB extraction/execution. It builds wheel/source distributions without publishing or accessing production catalogs.

References: [SQLMesh Context](https://sqlmesh.readthedocs.io/en/latest/_readthedocs/html/sqlmesh/core/context.html), [SQLMesh lineage](https://sqlmesh.readthedocs.io/en/latest/_readthedocs/html/sqlmesh/core/lineage.html), [OpenMetadata lineage](https://docs.open-metadata.org/v1.12.x/api-reference/lineage/add), [OpenMetadata schemas](https://github.com/open-metadata/OpenMetadata/tree/1.12.0-release/openmetadata-spec/src/main/resources/json/schema).
