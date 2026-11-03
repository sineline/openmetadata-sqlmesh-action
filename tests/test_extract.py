from pathlib import Path

import pytest

from openmetadata_sqlmesh.config import Config
from openmetadata_sqlmesh.extract import column_payload, extract_context, load_project

PROJECT = Path(__file__).parents[1] / "examples" / "sqlmesh"


@pytest.fixture(scope="module")
def context():
    pytest.importorskip("sqlmesh")
    from sqlmesh import Context

    ctx = Context(paths=str(PROJECT))
    yield ctx
    ctx.close()


def test_extracts_real_models_descriptions_and_dependencies(context):
    metadata = extract_context(context, Config("https://om.example", "warehouse"))
    assert len(metadata.models) == 3
    orders = next(m for m in metadata.models if m.table.table == "orders")
    assert orders.table.database == "memory"
    assert orders.description == "Curated orders."
    assert orders.columns[0]["name"] == "id"
    assert orders.columns[0]["description"] == "Order identifier"
    assert orders.columns[0]["dataType"] == "INT"
    assert [t.table for t in orders.dependencies] == ["raw_orders"]
    assert "SELECT" in orders.sql.upper()
    assert not metadata.warnings


def test_extracts_real_column_lineage(context):
    metadata = extract_context(context, Config("https://om.example", "warehouse"))
    revenue = next(m for m in metadata.models if m.table.table == "revenue")
    assert any(
        edge.source_table.table == "orders"
        and edge.source_column == "amount"
        and edge.target_column == "total"
        for edge in revenue.column_lineage
    )


def test_deterministic_serialization_and_project_loader(context):
    config = Config("https://om.example", "warehouse")
    first = extract_context(context, config).to_dict(config)
    second = load_project(PROJECT, config).to_dict(config)
    assert first == second
    assert "warehouse.memory.analytics.orders" in str(first)
    assert "token" not in first


@pytest.mark.parametrize(
    "sql_type,expected",
    [
        ("INT", "INT"),
        ("BIGINT", "BIGINT"),
        ("HUGEINT", "NUMBER"),
        ("UBIGINT", "NUMBER"),
        ("DECIMAL(10,2)", "DECIMAL"),
        ("VARCHAR(20)", "VARCHAR"),
        ("TIMESTAMPTZ", "TIMESTAMPZ"),
        ("BOOLEAN", "BOOLEAN"),
        ("JSON", "JSON"),
        ("INT[]", "ARRAY"),
        ("STRUCT(x INT, y VARCHAR)", "STRUCT"),
        ("UNKNOWN", "UNKNOWN"),
    ],
)
def test_type_mapping(sql_type, expected):
    from sqlglot import exp

    # Use the neutral parser here: DuckDB deliberately aliases VARCHAR to TEXT.
    dtype = exp.DataType.build(
        sql_type, dialect="duckdb" if sql_type == "UBIGINT" else None
    )
    column = column_payload("value", dtype, None)
    assert column["dataType"] == expected
    assert column["name"] == "value"
    assert column["dataTypeDisplay"]
    if expected == "STRUCT":
        assert [c["name"] for c in column["children"]] == ["x", "y"]
    if expected == "ARRAY":
        assert column["arrayDataType"] == "INT"


@pytest.mark.parametrize(
    "sql_type",
    [
        "INT256",
        "UINT256",
        "VECTOR",
        "ARRAY<VECTOR>",
        "ARRAY<ARRAY<VECTOR>>",
        "MAP<VARCHAR, ARRAY<ARRAY<VECTOR>>>",
    ],
)
def test_unsupported_types_are_reported_in_extraction_warnings(sql_type):
    from sqlglot import exp
    from sqlmesh.core.model import create_sql_model

    from openmetadata_sqlmesh.extract import extract_model

    model = create_sql_model(
        "db.analytics.unsupported",
        exp.select("1 AS value"),
        columns={"value": exp.DataType.build(sql_type)},
        dialect="duckdb",
    )
    warnings = []
    extracted = extract_model(
        model, Config("https://om.example", "warehouse"), warnings
    )
    assert extracted.columns
    assert any("type" in warning.lower() and "value" in warning for warning in warnings)


def test_nested_unsupported_types_do_not_replace_known_catalog_schema():
    from sqlglot import exp
    from sqlmesh.core.model import create_sql_model
    from test_sync import MemoryClient

    from openmetadata_sqlmesh import ProjectMetadata, Synchronizer
    from openmetadata_sqlmesh.extract import extract_model

    config, client = Config("https://om.example", "warehouse"), MemoryClient()
    model = create_sql_model(
        "db.analytics.deep_array",
        exp.select("1 AS value"),
        columns={"value": exp.DataType.build("ARRAY<ARRAY<VECTOR>>")},
        dialect="duckdb",
    )
    extracted = extract_model(model, config)
    known = {
        "name": "value",
        "dataType": "ARRAY",
        "arrayDataType": "ARRAY",
        "dataTypeDisplay": "INT[][]",
    }
    client.entities[("tables", extracted.table.fqn(config))] = {
        "id": "table",
        "columns": [known],
    }
    report = Synchronizer(config, client).sync(ProjectMetadata((extracted,)))
    assert not report.errors
    assert client.entities[("tables", extracted.table.fqn(config))]["columns"] == [
        known
    ]
