# Standalone SQLMesh Package Implementation Plan

**Goal:** Deliver an installable SQLMesh-to-OpenMetadata package with a CLI, runtime integration, audit-result reporting, and reusable GitHub Action.

**Architecture:** SQLMesh extraction produces typed metadata records. A native REST client and synchronizer publish those records; runtime callbacks and audit adapters reuse the same identity and API rules. The CLI and composite Action call the public library.

**Tech Stack:** Python 3.10+, standard-library HTTP/JSON/argparse/dataclasses, SQLMesh optional extra, pytest, setuptools.

## Task 1: Configuration, SQL names, and REST client

Files: `src/openmetadata_sqlmesh/config.py`, `names.py`, `client.py`; tests in `tests/test_foundation.py`.

- [x] Write failing tests for validated configuration, quoted names, service-qualified FQNs, URL normalization, authenticated JSON, 404 handling, retries, and credential redaction.
- [x] Run `python -m pytest tests/test_foundation.py`; confirm expected missing-package/feature assertions.
- [x] Implement `Config`, `Config.from_file`, `TableName`, `parse_table`, `fqn`, `OpenMetadataClient`, and `APIError`. A 404 alone means missing; retry 429/5xx and transport failures; never forward credentials through HTTP redirects.
- [x] Re-run the focused tests and review request payloads. Core usage is:

```python
config = Config(url="https://metadata.example.com", database_service="warehouse")
table = parse_table('"analytics"."sales"."order.items"', config)
assert table.fqn(config) == 'warehouse.analytics.sales."order.items"'
client = OpenMetadataClient(config)
entity = client.get_by_name("tables", table.fqn(config))
```

## Task 2: Real SQLMesh extraction

Files: `models.py`, `extract.py`, `tests/test_extract.py`, and `examples/sqlmesh/`.

- [x] Write a DuckDB SQLMesh project containing a source, two dependent models, column descriptions, and an audit. Write tests asserting model names, schemas, rendered SQL, dependencies, and column lineage.
- [x] Run the tests to establish missing extraction behavior.
- [x] Implement typed column/model/column-lineage records and `extract_context(context, config)` / `load_project(path, config, gateway=None)`. Preserve SQL identifier parts and derive only metadata actually supplied by SQLMesh. Use SQLGlot type expressions to map OpenMetadata column types.
- [x] Run extraction tests with installed SQLMesh 0.231.1, then a clean environment with 0.236.3. Assert deterministic JSON with no OpenMetadata calls.

```python
metadata = extract_context(context, config)
orders = next(model for model in metadata.models if model.table.table == "orders")
assert orders.columns[0]["name"] == "id"
assert orders.dependencies
assert orders.column_lineage
```

## Task 3: Catalog and lineage synchronization

Files: `sync.py`, `tests/test_sync.py`, and `tests/test_integration.py`.

- [x] Write failing tests using an in-memory REST fake for create/update order, service existence, full FQNs, repeat synchronization, unavailable upstreams, and protection of existing schemas.
- [x] Implement `Synchronizer(config, client)` and `sync_context(context, config, dry_run=False)`. Require existing database services. Ensure pipeline services, databases, schemas, tables, and pipelines; then publish table-to-table lineage with column details and pipeline references.
- [x] Do not update existing table schemas from unknown columns. Do not delete entities. Surface missing upstreams as report warnings, and API failures as errors.
- [x] Verify both in-memory behavior and an actual local HTTP request sequence.

```python
report = Synchronizer(config, client).sync(metadata)
assert report.tables == 3
assert report.lineage_edges == 2
assert not report.errors
```

## Task 4: Runtime observer and audit reporting

Files: `observer.py`, `audits.py`, `tests/test_observer.py`, `tests/test_audits.py`.

- [x] Write regression tests for concurrent batches of one model, preserved console delegation, terminal upstream edges, audit failures, interrupted runs, install/uninstall, and reporting failures.
- [x] Implement `install(config, strict=False)` and a delegating console wrapper. Each thread has independent evaluation records; each pending record has a unique execution ID. Publish native pipeline status and metadata/lineage on terminal callbacks. Clear interrupted records safely.
- [x] Write failing audit tests for Success/Failed/Aborted mapping, exact millisecond timestamps, idempotent test-case provisioning, missing model tables, and the real SQLMesh AuditResult adapter.
- [x] Implement typed `AuditRecord`, `publish_audits`, and a SQLMesh AuditResult adapter. Require real query/count/status inputs; do not invent successful audit results from aggregate callback counts.
- [x] Run observer and audit tests, including a real SQLMesh model evaluation with DuckDB and a local API fake.

## Task 5: Package, CLI, GitHub Action, and documentation

Files: `pyproject.toml`, `__init__.py`, `__main__.py`, `cli.py`, `action.yml`, `.github/workflows/ci.yml`, `.gitignore`, `README.md`, `examples/openmetadata-sqlmesh.json`, and `tests/test_cli.py`.

- [x] Add failing CLI tests for version/help, dry-run JSON, configuration errors, audit JSON, unsuccessful HTTP operations, and direct installed execution.
- [x] Implement `sync` and `audit-results` commands with `--config`; support project/gateway selection, `--dry-run`, JSON output, environment credentials, and clear nonzero failures. Ensure stdout remains parseable JSON while diagnostics use stderr.
- [x] Add setuptools metadata and the `sqlmesh` extra with bounded compatibility; export the documented public API. Add a composite Action using the exact CLI and environment variables for all caller inputs.
- [x] Replace the inherited dbt workflow/config/artifacts with the SQLMesh example and CI.
- [x] Document local installation, CLI dry run/live sync, runtime installation/uninstallation, SQLMesh audit adapters and JSON results, existing service requirements, naming rules, error policy, supported versions, and Action usage.

```bash
openmetadata-sqlmesh sync --project examples/sqlmesh --config examples/openmetadata-sqlmesh.json --dry-run
openmetadata-sqlmesh audit-results examples/audit-results.json --config examples/openmetadata-sqlmesh.json --dry-run
```

## Task 6: Validation and independent review

- [x] Run the complete pytest suite, real SQLMesh extraction/evaluation checks, and clean-environment package installation.
- [x] Build wheel/source distribution; inspect contents and verify installed CLI execution outside the repository.
- [x] Review spec coverage, public exports, dependencies, secret handling, API contracts, and clean git diff formatting. Request independent review and fix material findings with regression tests.
- [x] Re-run affected tests after fixes and record actual results.

## Verified results

- Full suite on Python 3.12 with SQLMesh 0.236.3: 197 passed.
- Full suite on Python 3.12 with SQLMesh 0.231.1: 197 passed.
- Core-only environment without SQLMesh or Dionysus: 163 passed, 8 optional-integration checks skipped.
- Ruff lint and formatting checks passed; git diff whitespace check passed.
- Wheel and source distribution built; packaged source matches the final working tree.
- Installed core wheel CLI and actual SQLMesh extraction from the built wheel passed outside the repository.
- Independent spec and code-quality reviews approved after preservation regression fixes.
- OpenMetadata tests use local HTTP/in-memory API fakes; no live catalog synchronization was performed.
