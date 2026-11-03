from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest

from openmetadata_sqlmesh.config import Config
from openmetadata_sqlmesh.models import ColumnLineage, ModelMetadata, ProjectMetadata
from openmetadata_sqlmesh.names import TableName, fqn
from openmetadata_sqlmesh.sync import Synchronizer


class MemoryClient:
    def __init__(self):
        self.entities = {("services/databaseServices", "warehouse"): {"id": "service"}}
        self.calls = []
        self.edges = {}

    def get_by_name(self, collection, name):
        self.calls.append(("GET", collection, name))
        return self.entities.get((collection, name))

    def upsert(self, collection, payload):
        parent = payload.get(
            "databaseSchema", payload.get("database", payload.get("service"))
        )
        name = f"{parent}.{fqn(payload['name'])}" if parent else fqn(payload["name"])
        entity = self.entities.setdefault((collection, name), {"id": str(uuid4())})
        entity.update(payload, fullyQualifiedName=name)
        self.calls.append(("PUT", collection, payload))
        return entity

    def request(self, method, path, payload=None, **kwargs):
        self.calls.append((method, path, payload))
        if method == "GET" and path.startswith("/lineage/table/"):
            target = path.split("/lineage/table/", 1)[1].split("?", 1)[0]
            return {
                "upstreamEdges": [
                    deepcopy(edge)
                    for (_, to_id), edge in self.edges.items()
                    if to_id == target
                ]
            }
        if method == "PUT" and path == "/lineage":
            edge = deepcopy(payload["edge"])
            source, target = edge["fromEntity"]["id"], edge["toEntity"]["id"]
            edge.update(fromEntity=source, toEntity=target)
            self.edges[(source, target)] = edge
        return {}


def metadata():
    source = TableName("db", "raw", "orders")
    target = TableName("db", "analytics", "orders")
    return ProjectMetadata(
        (
            ModelMetadata(source, ({"name": "id", "dataType": "INT"},)),
            ModelMetadata(
                target,
                ({"name": "id", "dataType": "INT"},),
                (source,),
                (ColumnLineage(source, "id", "id"),),
                sql="SELECT id FROM db.raw.orders",
            ),
        )
    )


def test_sync_creates_catalog_and_native_column_lineage():
    config, client = Config("https://om.example", "warehouse"), MemoryClient()
    report = Synchronizer(config, client).sync(metadata())
    assert not report.errors
    assert report.tables == 2
    assert report.pipelines == 2
    assert report.lineage_edges == 1
    edge = next(
        payload["edge"] for method, path, payload in client.calls if path == "/lineage"
    )
    assert edge["fromEntity"]["type"] == "table"
    assert edge["toEntity"]["type"] == "table"
    assert edge["lineageDetails"]["pipeline"]["type"] == "pipeline"
    assert edge["lineageDetails"]["columnsLineage"] == [
        {
            "fromColumns": ["warehouse.db.raw.orders.id"],
            "toColumn": "warehouse.db.analytics.orders.id",
        }
    ]


def test_sync_is_idempotent_and_has_no_colliding_pipeline_names():
    config, client = Config("https://om.example", "warehouse"), MemoryClient()
    synchronizer = Synchronizer(config, client)
    synchronizer.sync(metadata())
    keys = set(client.entities)
    report = synchronizer.sync(metadata())
    assert not report.errors
    assert set(client.entities) == keys
    assert len([key for key in keys if key[0] == "pipelines"]) == 2


def test_requires_existing_database_service_without_creating_connections():
    client = MemoryClient()
    client.entities.clear()
    report = Synchronizer(Config("https://om.example", "warehouse"), client).sync(
        metadata()
    )
    assert report.errors
    assert not any(call[0] == "PUT" for call in client.calls)


def test_unresolved_columns_never_replace_an_existing_schema():
    config, client = Config("https://om.example", "warehouse"), MemoryClient()
    source = metadata().models[0]
    existing = {"id": "old", "columns": [{"name": "real", "dataType": "BIGINT"}]}
    client.entities[("tables", source.table.fqn(config))] = existing
    report = Synchronizer(config, client).sync(
        ProjectMetadata((replace(source, columns=()),))
    )
    assert not report.errors
    assert report.warnings
    assert existing["columns"][0]["name"] == "real"
    assert not any(call[1] == "tables" and call[0] == "PUT" for call in client.calls)


def test_missing_upstream_is_reported_and_transient_errors_are_not_missing():
    config, client = Config("https://om.example", "warehouse"), MemoryClient()
    target = metadata().models[1]
    report = Synchronizer(config, client).sync(ProjectMetadata((target,)))
    assert report.warnings
    assert report.lineage_edges == 0
    normal_lookup = client.get_by_name

    def fail_lookup(collection, name):
        if collection == "tables" and name.endswith("raw.orders"):
            raise RuntimeError("temporary API outage")
        return normal_lookup(collection, name)

    client.get_by_name = fail_lookup
    report = Synchronizer(config, client).sync(ProjectMetadata((target,)))
    assert any("temporary API outage" in error for error in report.errors)


def test_serialization_keeps_service_and_column_components_separate():
    model = metadata().models[1]
    edge = model.to_dict(Config("https://om.example", "warehouse"))["column_lineage"][0]
    assert edge["fromColumn"] == "warehouse.db.raw.orders.id"


def test_service_references_use_quoted_fqns_for_dotted_service_names():
    config = Config(
        "https://om.example",
        "warehouse",
        pipeline_service="sqlmesh.runtime",
        catalog_services={"db": "warehouse.prod"},
    )
    client = MemoryClient()
    client.entities[("services/databaseServices", '"warehouse.prod"')] = {
        "id": "service"
    }
    report = Synchronizer(config, client).sync(metadata())
    assert not report.errors
    databases = [
        payload
        for method, path, payload in client.calls
        if method == "PUT" and path == "databases"
    ]
    pipelines = [
        payload
        for method, path, payload in client.calls
        if method == "PUT" and path == "pipelines"
    ]
    assert all(payload["service"] == '"warehouse.prod"' for payload in databases)
    assert all(payload["service"] == '"sqlmesh.runtime"' for payload in pipelines)


@pytest.mark.parametrize(
    "known,partial",
    [
        (
            {
                "name": "values",
                "dataType": "ARRAY",
                "arrayDataType": "INT",
                "dataTypeDisplay": "INT[]",
            },
            {
                "name": "values",
                "dataType": "ARRAY",
                "arrayDataType": "UNKNOWN",
                "dataTypeDisplay": "UNKNOWN[]",
            },
        ),
        (
            {
                "name": "values",
                "dataType": "STRUCT",
                "dataTypeDisplay": "STRUCT(id BIGINT)",
                "children": [{"name": "id", "dataType": "BIGINT"}],
            },
            {
                "name": "values",
                "dataType": "STRUCT",
                "dataTypeDisplay": "STRUCT(id UNKNOWN)",
                "children": [{"name": "id", "dataType": "UNKNOWN"}],
            },
        ),
    ],
)
def test_partially_unresolved_complex_types_preserve_existing_schema(known, partial):
    config, client = Config("https://om.example", "warehouse"), MemoryClient()
    model = replace(metadata().models[0], columns=(partial,))
    client.entities[("tables", model.table.fqn(config))] = {
        "id": "table",
        "columns": [known],
    }
    report = Synchronizer(config, client).sync(ProjectMetadata((model,)))
    assert not report.errors
    assert client.entities[("tables", model.table.fqn(config))]["columns"] == [known]
    assert report.warnings


def test_lineage_updates_preserve_known_columns_and_unrelated_details():
    config, client = Config("https://om.example", "warehouse"), MemoryClient()
    sync = Synchronizer(config, client)
    original = metadata()
    assert not sync.sync(original).errors
    edge = next(iter(client.edges.values()))
    edge["description"] = "Documented edge"
    edge["lineageDetails"]["description"] = "Catalog owner description"
    previous = deepcopy(edge["lineageDetails"]["columnsLineage"])
    incomplete = ProjectMetadata(
        (original.models[0], replace(original.models[1], column_lineage=()))
    )
    assert not sync.sync(incomplete).errors
    updated = next(iter(client.edges.values()))
    assert updated["lineageDetails"]["columnsLineage"] == previous
    assert updated["description"] == "Documented edge"
    assert updated["lineageDetails"]["description"] == "Catalog owner description"
    target = replace(
        original.models[1],
        columns=original.models[1].columns + ({"name": "other", "dataType": "INT"},),
        column_lineage=(ColumnLineage(original.models[0].table, "id", "other"),),
    )
    assert not sync.sync(ProjectMetadata((original.models[0], target))).errors
    merged = next(iter(client.edges.values()))["lineageDetails"]["columnsLineage"]
    assert {row["toColumn"] for row in merged} == {
        "warehouse.db.analytics.orders.id",
        "warehouse.db.analytics.orders.other",
    }


def test_failed_lineage_read_never_overwrites_existing_details():
    config, client = Config("https://om.example", "warehouse"), MemoryClient()
    sync = Synchronizer(config, client)
    assert not sync.sync(metadata()).errors
    old_edges = deepcopy(client.edges)
    normal = client.request
    client.calls.clear()

    def broken_read(method, path, payload=None, **kwargs):
        if method == "GET" and path.startswith("/lineage/"):
            raise RuntimeError("Lineage read unavailable")
        return normal(method, path, payload, **kwargs)

    client.request = broken_read
    assert sync.sync(metadata()).errors
    assert client.edges == old_edges
    assert not any(
        method == "PUT" and path == "/lineage" for method, path, _ in client.calls
    )
