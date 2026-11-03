"""Metadata records independent of SQLMesh and HTTP implementations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import Config
from .names import TableName, fqn


@dataclass(frozen=True)
class ColumnLineage:
    source_table: TableName
    source_column: str
    target_column: str


@dataclass(frozen=True)
class ModelMetadata:
    table: TableName
    columns: tuple[dict[str, Any], ...] = ()
    dependencies: tuple[TableName, ...] = ()
    column_lineage: tuple[ColumnLineage, ...] = ()
    description: str | None = None
    sql: str | None = None
    table_type: str = "Regular"
    unresolved_columns: tuple[str, ...] = ()

    def to_dict(self, config: Config) -> dict[str, Any]:
        return {
            "name": self.table.fqn(config),
            "database": self.table.database,
            "schema": self.table.schema,
            "table": self.table.table,
            "columns": list(self.columns),
            "description": self.description,
            "sql": self.sql,
            "table_type": self.table_type,
            "unresolved_columns": list(self.unresolved_columns),
            "dependencies": [t.fqn(config) for t in self.dependencies],
            "column_lineage": [
                {
                    "fromColumn": edge.source_table.fqn(config)
                    + "."
                    + fqn(edge.source_column),
                    "sourceTable": edge.source_table.fqn(config),
                    "sourceColumn": edge.source_column,
                    "targetColumn": edge.target_column,
                }
                for edge in self.column_lineage
            ],
        }


@dataclass(frozen=True)
class ProjectMetadata:
    models: tuple[ModelMetadata, ...]
    warnings: tuple[str, ...] = ()

    def to_dict(self, config: Config) -> dict[str, Any]:
        return {
            "version": 1,
            "models": [m.to_dict(config) for m in self.models],
            "warnings": list(self.warnings),
        }
