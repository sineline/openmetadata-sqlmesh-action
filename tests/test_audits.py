import pytest
from test_sync import MemoryClient

from openmetadata_sqlmesh.audits import AuditRecord, publish_audits
from openmetadata_sqlmesh.client import APIError
from openmetadata_sqlmesh.config import Config


def record(**kwargs):
    return AuditRecord(
        model="db.analytics.orders",
        audit="not_null",
        timestamp=1793657000000,
        query="SELECT * FROM db.analytics.orders WHERE id IS NULL",
        **kwargs,
    )


@pytest.mark.parametrize(
    "count,status", [(0, "Success"), (3, "Failed"), (None, "Aborted")]
)
def test_audit_result_status_and_timestamp_are_real(count, status):
    client, config = MemoryClient(), Config("https://om.example", "warehouse")
    client.entities[("tables", "warehouse.db.analytics.orders")] = {"id": "table"}
    report = publish_audits([record(count=count)], config, client=client)
    assert not report["errors"]
    result = next(
        call[2]
        for call in client.calls
        if call[1].startswith("/dataQuality/testCases/testCaseResults/")
    )
    assert result["timestamp"] == 1793657000000
    assert result["testCaseStatus"] == status
    assert result["testResultValue"] == (
        [] if count is None else [{"name": "failedRows", "value": str(count)}]
    )


def test_missing_table_is_an_error_and_dry_run_has_no_requests():
    client, config = MemoryClient(), Config("https://om.example", "warehouse")
    assert publish_audits([record(count=0)], config, client=client)["errors"]
    client.calls.clear()
    plan = publish_audits([record(count=0)], config, client=client, dry_run=True)
    assert plan["audit_results"][0]["testCaseStatus"] == "Success"
    assert not client.calls


def test_sqlmesh_audit_queries_use_failing_rows_strategy():
    client, config = MemoryClient(), Config("https://om.example", "warehouse")
    client.entities[("tables", "warehouse.db.analytics.orders")] = {"id": "table"}
    assert not publish_audits([record(count=0)], config, client=client)["errors"]
    case = next(
        payload
        for method, path, payload in client.calls
        if method == "PUT" and path == "dataQuality/testCases"
    )
    parameters = {
        parameter["name"]: parameter["value"] for parameter in case["parameterValues"]
    }
    # SQLMesh supplies rows violating the audit, not a SELECT COUNT(*) scalar.
    assert parameters["strategy"] == "ROWS"
    assert parameters["threshold"] == "0"


def test_accepts_actual_sqlmesh_audit_results():
    pytest.importorskip("sqlmesh")
    from sqlglot import exp
    from sqlmesh.core.audit.builtin import BUILT_IN_AUDITS
    from sqlmesh.core.model import create_sql_model
    from sqlmesh.core.model.definition import AuditResult

    model = create_sql_model(
        "db.analytics.orders", exp.select("1 AS id"), dialect="duckdb"
    )
    result = AuditResult(
        audit=BUILT_IN_AUDITS["not_null"],
        audit_args={},
        model=model,
        count=0,
        query=exp.select("id").from_("db.analytics.orders"),
    )
    converted = AuditRecord.from_sqlmesh(result, timestamp=1793657000000)
    assert converted.audit == "not_null"
    assert converted.count == 0
    assert converted.query


def test_legacy_test_suite_and_result_routes_are_supported():
    client, config = MemoryClient(), Config("https://om.example", "warehouse")
    client.entities[("tables", "warehouse.db.analytics.orders")] = {"id": "table"}
    normal_upsert, normal_request = client.upsert, client.request

    def legacy_upsert(collection, payload):
        if collection == "dataQuality/testCases" and "testSuite" not in payload:
            raise APIError("testSuite required", status=400)
        return normal_upsert(collection, payload)

    def legacy_request(method, path, payload=None, **kwargs):
        if path.startswith("/dataQuality/testCases/testCaseResults/"):
            raise APIError("old route", status=404)
        return normal_request(method, path, payload, **kwargs)

    client.upsert, client.request = legacy_upsert, legacy_request
    report = publish_audits([record(count=1)], config, client=client)
    assert not report["errors"]
    assert any(
        call[0] == "PUT" and call[1].endswith("/testCaseResult")
        for call in client.calls
    )


@pytest.mark.parametrize(
    "kwargs",
    [{"count": -1}, {"count": True}, {"count": 1.5}, {"status": "Success", "count": 4}],
)
def test_invalid_or_contradictory_results_are_rejected(kwargs):
    with pytest.raises(ValueError):
        record(**kwargs)
