"""Read model metadata through SQLMesh's public Context and model interfaces."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import Config
from .models import ColumnLineage, ModelMetadata, ProjectMetadata
from .names import parse_table

_OM_TYPES = frozenset(
    {
        "NUMBER",
        "TINYINT",
        "SMALLINT",
        "INT",
        "BIGINT",
        "BYTEINT",
        "BYTES",
        "FLOAT",
        "DOUBLE",
        "DECIMAL",
        "NUMERIC",
        "TIMESTAMP",
        "TIMESTAMPZ",
        "TIME",
        "DATE",
        "DATETIME",
        "INTERVAL",
        "STRING",
        "TEXT",
        "CHAR",
        "LONG",
        "VARCHAR",
        "BOOLEAN",
        "BINARY",
        "VARBINARY",
        "ARRAY",
        "BLOB",
        "MAP",
        "STRUCT",
        "UNION",
        "GEOGRAPHY",
        "ENUM",
        "JSON",
        "UUID",
        "VARIANT",
        "GEOMETRY",
        "BYTEA",
        "NULL",
        "SUPER",
        "XML",
        "UNKNOWN",
        "RECORD",
        "SET",
        "YEAR",
        "INET",
        "CIDR",
        "MONEY",
        "BIT",
    }
)
_TYPE_ALIASES = {
    "INTEGER": "INT",
    "INT128": "NUMBER",
    "HUGEINT": "NUMBER",
    "UINT": "BIGINT",
    "UBIGINT": "NUMBER",
    "UTINYINT": "SMALLINT",
    "USMALLINT": "INT",
    "UINTEGER": "BIGINT",
    "TIMESTAMPTZ": "TIMESTAMPZ",
    "TIMESTAMPLTZ": "TIMESTAMPZ",
    "TIMESTAMPNTZ": "TIMESTAMP",
    "TIMESTAMP_S": "TIMESTAMP",
    "TIMESTAMP_MS": "TIMESTAMP",
    "TIMESTAMP_NS": "TIMESTAMP",
    "BOOL": "BOOLEAN",
    "NCHAR": "CHAR",
    "NVARCHAR": "VARCHAR",
    "OBJECT": "JSON",
    "JSONB": "JSON",
    "ROW": "STRUCT",
}


def column_payload(
    name: str, dtype: Any, description: str | None, dialect: str = ""
) -> dict[str, Any]:
    """Map a SQLGlot type, retaining the original type for human display."""
    from sqlglot import exp

    type_name = str(getattr(dtype.this, "value", dtype.this)).upper()
    mapped = _TYPE_ALIASES.get(type_name, type_name)
    result: dict[str, Any] = {
        "name": name,
        "dataType": mapped if mapped in _OM_TYPES else "UNKNOWN",
        "dataTypeDisplay": dtype.sql(dialect=dialect or None),
    }
    if description:
        result["description"] = description
    if mapped == "ARRAY":
        inner = (
            dtype.expressions[0] if dtype.expressions else exp.DataType.build("UNKNOWN")
        )
        child = column_payload("element", inner, None, dialect)
        result["arrayDataType"] = child["dataType"]
        if child.get("children"):
            result["children"] = child["children"]
    elif mapped in {"STRUCT", "UNION"}:
        result["children"] = [
            column_payload(field.name, field.args["kind"], None, dialect)
            for field in dtype.expressions
            if isinstance(field, exp.ColumnDef)
        ]
    elif mapped == "MAP":
        result["children"] = [
            column_payload(label, inner, None, dialect)
            for label, inner in zip(("key", "value"), dtype.expressions)
        ]
    elif mapped in {"VARCHAR", "CHAR", "BINARY", "VARBINARY", "DECIMAL", "NUMERIC"}:
        try:
            params = [int(item.this.name) for item in dtype.expressions]
        except (AttributeError, ValueError, TypeError):
            params = []
        if params:
            if mapped in {"DECIMAL", "NUMERIC"}:
                result["precision"] = params[0]
                if len(params) > 1:
                    result["scale"] = params[1]
            else:
                result["dataLength"] = params[0]
    return result


def _has_unknown_type(dtype: Any) -> bool:
    # OpenMetadata represents nested arrays less precisely than SQLGlot.
    # Inspect the original type tree so no unsupported inner type is lost.
    from sqlglot import exp

    for node in dtype.find_all(exp.DataType):
        name = str(getattr(node.this, "value", node.this)).upper()
        mapped = _TYPE_ALIASES.get(name, name)
        if mapped == "UNKNOWN" or mapped not in _OM_TYPES:
            return True
    return False


def extract_model(
    model: Any, config: Config, warnings: list[str] | None = None
) -> ModelMetadata:
    """Extract a single SQLMesh model, also usable from execution snapshots."""
    from sqlglot import exp
    from sqlmesh.core.lineage import lineage

    warnings = warnings if warnings is not None else []
    dialect = getattr(model, "dialect", "")
    table = parse_table(model.fqn, config, dialect)
    columns = getattr(model, "columns_to_types", None) or {}
    descriptions = getattr(model, "column_descriptions", None) or {}
    dependencies = tuple(
        sorted(
            {
                parse_table(name, config, dialect)
                for name in model.depends_on
                if parse_table(name, config, dialect) != table
            },
            key=lambda name: name.sql,
        )
    )
    is_sql = bool(getattr(model, "is_sql", False))
    query = model.render_query_or_raise() if is_sql else None
    edges: set[tuple[Any, str, str]] = set()
    if is_sql:
        for column in columns:
            try:
                for node in lineage(column, model, trim_selects=False).walk():
                    if node.downstream or not isinstance(node.expression, exp.Table):
                        continue
                    # SQLMesh's leaf table includes its query alias. Only the
                    # structured identifier parts belong to the catalog name.
                    source = parse_table(
                        ".".join(
                            part.sql(dialect=dialect) for part in node.expression.parts
                        ),
                        config,
                        dialect,
                    )
                    source_column = exp.to_column(node.name, dialect=dialect).name
                    if source in dependencies and source_column != "*":
                        edges.add((source, source_column, column))
            except Exception as exc:
                warnings.append(
                    f"Column lineage unavailable for {table.sql}.{column}: {type(exc).__name__}"
                )
    kind = getattr(model, "kind", None)
    table_type = (
        "External"
        if getattr(kind, "is_external", False)
        else "View"
        if getattr(kind, "is_view", False)
        else "Regular"
    )
    extracted_columns = tuple(
        column_payload(name, dtype, descriptions.get(name), dialect)
        for name, dtype in columns.items()
    )
    unresolved_columns = tuple(
        name for name, dtype in columns.items() if _has_unknown_type(dtype)
    )
    for column in extracted_columns:
        if column["name"] in unresolved_columns:
            warnings.append(
                f"Unresolved or unsupported column type for {table.sql}.{column['name']}: "
                f"{column['dataTypeDisplay']}"
            )
    return ModelMetadata(
        table=table,
        columns=extracted_columns,
        dependencies=dependencies,
        column_lineage=tuple(
            ColumnLineage(*edge)
            for edge in sorted(edges, key=lambda e: (e[0].sql, e[1], e[2]))
        ),
        description=getattr(model, "description", None),
        sql=query.sql(dialect=dialect, pretty=True) if query is not None else None,
        table_type=table_type,
        unresolved_columns=unresolved_columns,
    )


def extract_context(context: Any, config: Config) -> ProjectMetadata:
    """Extract local model definitions without planning or executing a project."""
    warnings: list[str] = []
    models = []
    for model in context.models.values():
        if getattr(getattr(model, "kind", None), "is_embedded", False):
            warnings.append(
                f"Embedded model {model.name} has no catalog table and was skipped"
            )
            continue
        models.append(extract_model(model, config, warnings))
    return ProjectMetadata(
        tuple(sorted(models, key=lambda model: model.table.sql)),
        tuple(sorted(warnings)),
    )


def load_project(
    path: str | Path, config: Config, gateway: str | None = None
) -> ProjectMetadata:
    """Load a SQLMesh project and close resources after extracting its metadata."""
    try:
        from sqlmesh import Context
    except ImportError as exc:
        raise RuntimeError(
            'Install SQLMesh support with pip install "openmetadata-sqlmesh[sqlmesh]"'
        ) from exc
    context = Context(paths=str(Path(path).resolve()), gateway=gateway)
    try:
        return extract_context(context, config)
    finally:
        context.close()
