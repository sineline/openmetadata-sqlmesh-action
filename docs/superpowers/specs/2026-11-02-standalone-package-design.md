# Standalone SQLMesh and OpenMetadata integration

## Goal

Turn this repository into an installable Python package, command-line tool, and reusable GitHub Action for publishing SQLMesh metadata to OpenMetadata. Consumers must not need Dionysus, Airflow, Marquez, dbt artifacts, or the OpenMetadata ingestion SDK.

## Delivery

One Python package supports local scripts, SQLMesh execution, a CLI, and a reusable GitHub Action. It includes metadata synchronization, runtime reporting, and explicit audit-result publication.

## Package and boundaries

- Distribution: `openmetadata-sqlmesh`; import: `openmetadata_sqlmesh`; executable: `openmetadata-sqlmesh`.
- Python 3.10 or newer. SQLMesh is an optional installation extra, with compatibility explicitly bounded to the interfaces exercised by integration tests. The initial target includes SQLMesh 0.231.1 and Dionysus's configured 0.236.3.
- A small native OpenMetadata REST client handles authentication, JSON, timeouts, retries, and entity resolution. The package does not require OpenLineage or route through Marquez.
- Separate modules own configuration, SQL names, SQLMesh extraction, API calls, metadata synchronization, runtime callbacks, audit reporting, and CLI parsing. Public functions accept explicit configuration and support callers supplying a loaded SQLMesh Context.
- Authentication comes from an environment variable or explicit library argument. Credentials never appear in dry-run artifacts, reprs, error messages, or logs.

## Metadata synchronization

- Read a SQLMesh project through its Context. Do not execute plans, materialize models, run audits, or query warehouse data as part of synchronization. Project configuration and macros still run as normal when SQLMesh loads a project.
- Publish model descriptions, resolved column names and types, column descriptions, rendered SQL, and model dependencies. Preserve identifier component boundaries and case, including quoted identifiers containing dots.
- Use an existing OpenMetadata database service selected by configuration; support explicit service overrides for different catalogs. Do not create warehouse service connections or invent credentials.
- Create or update databases, schemas, model tables, and SQLMesh pipeline entities. Preserve existing schemas when SQLMesh cannot resolve real columns; never replace a known schema with a placeholder column.
- Create table-to-table lineage with associated pipeline references. Include column lineage when SQLMesh can resolve source columns. Report unresolved external tables and unsupported metadata as explicit warnings or errors rather than inventing entities or silently discarding failures.
- Repeated synchronization is idempotent. Do not delete catalog entities or lineage absent from the current project.
- `sync --dry-run` produces a deterministic JSON artifact describing extracted metadata and planned relationships without contacting OpenMetadata. Live synchronization returns counts and actionable failures; an unsuccessful CLI run exits nonzero.

## Runtime integration

- `install(...)` wraps SQLMesh's current Console and delegates its normal behavior. Repeated installation must be safe and an uninstall operation restores the prior console.
- Observe model evaluation callbacks and publish terminal execution status and upstream/output lineage to OpenMetadata.
- Keep a separate run record for every concurrent evaluation batch, including multiple batches of one snapshot. Never key run state only by model name.
- Include upstream datasets on successful and failed terminal events. Clear pending runs and account for interrupted evaluations when the evaluation phase stops.
- Metadata reporting failures log useful diagnostics and do not fail SQLMesh work by default. An explicit strict mode is available for callers that require reporting to succeed.
- Use the configured database service in table and column fully qualified names. Distinguish a missing entity from a timeout, authentication error, or server failure; do not cache transient failures as missing tables.

## Audit-result reporting

- Provide a public API for publishing explicit SQLMesh audit results and a CLI command accepting a documented JSON result format. Accept supplied SQLMesh AuditResult objects through an adapter.
- Publish real audit status, query, timestamp, and failure counts to OpenMetadata test cases. Provision missing executable test suites and test cases idempotently against an existing/resolved model table.
- Do not infer individual successful audits from aggregate Console counters or claim that an audit ran when no result was supplied. This package reports audit results; it does not implement warehouse-specific contract execution.

## Configuration and GitHub Action

- Use a JSON configuration file with OpenMetadata URL, database service, pipeline service, default database/schema when needed, catalog service overrides, timeout, and retry settings. Environment variables supply credentials and optional URL overrides.
- Add a composite `action.yml` that installs this package and invokes the same synchronization CLI against the caller's project. Pass inputs through environment variables and quote shell arguments.
- Replace the inherited dbt ingestion example and artifacts with SQLMesh examples, package documentation, and CI. CI runs unit and real SQLMesh extraction tests and builds the wheel and source distribution. It does not contact a production OpenMetadata service or publish packages.

## Validation

- Unit tests exercise native REST contracts, authenticated calls, retryable versus permanent errors, identifier handling, service mapping, missing-table handling, safe schema updates, idempotent writes, audit status mapping, and CLI exit codes.
- Regression tests cover concurrency, upstream relationships on terminal events, interrupted runs, installation lifecycle, and runtime reporting failures.
- A small DuckDB SQLMesh fixture verifies real project extraction, model/column descriptions, table and column lineage, and JSON dry runs without warehouse credentials. A local fake OpenMetadata HTTP server verifies the complete CLI request sequence.
- Validate package building and installation, executable help, documented examples, and the GitHub Action's invocation.
