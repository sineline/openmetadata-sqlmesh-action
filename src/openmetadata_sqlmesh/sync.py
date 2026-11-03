"""Idempotent catalog and lineage publishing using OpenMetadata's native API."""

from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import quote

from .client import OpenMetadataClient
from .config import Config
from .models import ModelMetadata, ProjectMetadata
from .names import TableName, fqn


@dataclass
class SyncReport:
    tables: int = 0
    pipelines: int = 0
    lineage_edges: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def pipeline_name(table: TableName, config: Config) -> str:
    identity = table.fqn(config)
    readable = re.sub(
        r"[^a-zA-Z0-9_-]", "_", f"{table.database}_{table.schema}_{table.table}"
    )[:80]
    return f"{readable}_{hashlib.sha256(identity.encode()).hexdigest()[:12]}"


def pipeline_fqn(table: TableName, config: Config) -> str:
    return fqn(config.pipeline_service, pipeline_name(table, config))


def _unresolved_type(column: dict[str, Any]) -> bool:
    return (
        column.get("dataType") == "UNKNOWN"
        or column.get("arrayDataType") == "UNKNOWN"
        or any(_unresolved_type(child) for child in column.get("children", []))
    )


def _entity_id(reference: Any) -> str | None:
    return reference.get("id") if isinstance(reference, dict) else reference


def _merge_columns(
    previous: list[dict[str, Any]], incoming: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for row in [*previous, *incoming]:
        target = row["toColumn"]
        old = merged.get(target, {})
        sources = sorted(
            set(old.get("fromColumns", [])) | set(row.get("fromColumns", []))
        )
        merged[target] = {**old, **deepcopy(row), "fromColumns": sources}
    return [merged[target] for target in sorted(merged)]


class Synchronizer:
    def __init__(self, config: Config, client: OpenMetadataClient | None = None):
        self.config = config
        self.client = client if client is not None else OpenMetadataClient(config)

    def _pipeline_service(self) -> None:
        collection = "services/pipelineServices"
        if (
            self.client.get_by_name(collection, fqn(self.config.pipeline_service))
            is None
        ):
            self.client.upsert(
                collection,
                {
                    "name": self.config.pipeline_service,
                    "serviceType": "CustomPipeline",
                    "connection": {"config": {"type": "CustomPipeline"}},
                },
            )

    def ensure_pipeline(self, model: ModelMetadata) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": pipeline_name(model.table, self.config),
            "displayName": model.table.sql,
            "service": fqn(self.config.pipeline_service),
            "tasks": [{"name": "evaluate", "taskType": "SQLMesh"}],
        }
        if model.sql:
            payload["tasks"][0]["taskSQL"] = model.sql
        if model.description:
            payload["description"] = model.description
        return self.client.upsert("pipelines", payload)

    def _table(self, model: ModelMetadata, report: SyncReport) -> dict[str, Any] | None:
        config, table = self.config, model.table
        existing = self.client.get_by_name("tables", table.fqn(config))
        if not model.columns:
            if existing:
                report.warnings.append(
                    f"Preserved existing schema for {table.fqn(config)}: columns unresolved"
                )
                return existing
            report.warnings.append(
                f"Skipped {table.fqn(config)}: no resolved columns and no existing table"
            )
            return None
        self.client.upsert(
            "databases",
            {
                "name": table.database,
                "service": fqn(config.service_for(table.database)),
            },
        )
        self.client.upsert(
            "databaseSchemas",
            {"name": table.schema, "database": table.database_fqn(config)},
        )
        old_columns = {
            column["name"]: column for column in (existing or {}).get("columns", [])
        }
        columns = []
        for column in model.columns:
            value = dict(column)
            if (
                _unresolved_type(value) or column["name"] in model.unresolved_columns
            ) and column["name"] in old_columns:
                # Preserve the whole known complex type when extraction is
                # partial. This also retains nested-array structure that the
                # OpenMetadata payload cannot represent at every depth.
                for key in (
                    "dataType",
                    "dataTypeDisplay",
                    "arrayDataType",
                    "children",
                    "precision",
                    "scale",
                    "dataLength",
                ):
                    value.pop(key, None)
                    if key in old_columns[column["name"]]:
                        value[key] = deepcopy(old_columns[column["name"]][key])
                report.warnings.append(
                    f"Preserved existing type for {table.fqn(config)}.{column['name']}: type resolution incomplete"
                )
            columns.append(value)
        payload: dict[str, Any] = {
            "name": table.table,
            "databaseSchema": table.schema_fqn(config),
            "columns": columns,
            "tableType": model.table_type,
        }
        if model.description:
            payload["description"] = model.description
        if model.sql:
            payload["schemaDefinition"] = model.sql
        entity = self.client.upsert("tables", payload)
        report.tables += 1
        return entity

    def sync(self, metadata: ProjectMetadata) -> SyncReport:
        report = SyncReport(warnings=list(metadata.warnings))
        config = self.config
        tables: dict[TableName, dict[str, Any]] = {}
        pipelines: dict[TableName, dict[str, Any]] = {}
        try:
            services = {
                config.service_for(model.table.database) for model in metadata.models
            }
            for service in sorted(services):
                if (
                    self.client.get_by_name("services/databaseServices", fqn(service))
                    is None
                ):
                    report.errors.append(
                        f"Database service {service!r} does not exist in OpenMetadata"
                    )
            if report.errors:
                return report
            self._pipeline_service()
        except Exception as exc:
            report.errors.append(self._error(exc))
            return report
        for model in metadata.models:
            try:
                entity = self._table(model, report)
                if entity:
                    tables[model.table] = entity
                pipelines[model.table] = self.ensure_pipeline(model)
                report.pipelines += 1
            except Exception as exc:
                report.errors.append(f"{model.table.fqn(config)}: {self._error(exc)}")
        for model in metadata.models:
            target = tables.get(model.table)
            pipeline = pipelines.get(model.table)
            if target is None or pipeline is None:
                continue
            previous_edges = {}
            if model.dependencies:
                try:
                    graph = self.client.request(
                        "GET",
                        "/lineage/table/"
                        + quote(target["id"], safe="")
                        + "?upstreamDepth=1&downstreamDepth=0",
                    )
                    previous_edges = {
                        _entity_id(edge["fromEntity"]): edge
                        for edge in (graph.get("upstreamEdges") or [])
                        if _entity_id(edge["toEntity"]) == target["id"]
                    }
                except Exception as exc:
                    report.errors.append(
                        f"Read existing lineage for {model.table.sql}: {self._error(exc)}"
                    )
                    continue
            for parent in model.dependencies:
                try:
                    source = tables.get(parent)
                    if source is None:
                        source = self.client.get_by_name("tables", parent.fqn(config))
                    if source is None:
                        report.warnings.append(
                            f"Upstream table {parent.fqn(config)} not found; lineage to {model.table.fqn(config)} skipped"
                        )
                        continue
                    previous = previous_edges.get(source["id"], {})
                    details: dict[str, Any] = {
                        **deepcopy(previous.get("lineageDetails") or {}),
                        "pipeline": {"id": pipeline["id"], "type": "pipeline"},
                        "source": "PipelineLineage",
                    }
                    if model.sql:
                        details["sqlQuery"] = model.sql
                    column_edges = [
                        {
                            "fromColumns": [
                                parent.fqn(config) + "." + fqn(edge.source_column)
                            ],
                            "toColumn": model.table.fqn(config)
                            + "."
                            + fqn(edge.target_column),
                        }
                        for edge in model.column_lineage
                        if edge.source_table == parent
                    ]
                    merged_columns = _merge_columns(
                        details.get("columnsLineage") or [], column_edges
                    )
                    if merged_columns:
                        details["columnsLineage"] = merged_columns
                    self.client.request(
                        "PUT",
                        "/lineage",
                        {
                            "edge": {
                                **(
                                    {"description": previous["description"]}
                                    if previous.get("description") is not None
                                    else {}
                                ),
                                "fromEntity": {"id": source["id"], "type": "table"},
                                "toEntity": {"id": target["id"], "type": "table"},
                                "lineageDetails": details,
                            }
                        },
                    )
                    report.lineage_edges += 1
                except Exception as exc:
                    report.errors.append(
                        f"Lineage {parent.sql} -> {model.table.sql}: {self._error(exc)}"
                    )
        return report

    def _error(self, error: Exception) -> str:
        message = str(error)
        return (
            message.replace(self.config.token, "[REDACTED]")
            if self.config.token
            else message
        )


def sync_context(
    context: Any,
    config: Config,
    *,
    dry_run: bool = False,
    client: OpenMetadataClient | None = None,
) -> SyncReport | dict[str, Any]:
    from .extract import extract_context

    metadata = extract_context(context, config)
    if dry_run:
        return metadata.to_dict(config)
    return Synchronizer(config, client).sync(metadata)
