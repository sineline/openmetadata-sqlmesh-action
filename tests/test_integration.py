"""Real SQLMesh loading/evaluation and a local OpenMetadata HTTP contract fake."""

import io
import json
import shutil
import threading
from contextlib import contextmanager, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote
from uuid import uuid4

import pytest

pytest.importorskip("sqlmesh")

from test_sync import MemoryClient

from openmetadata_sqlmesh import (
    AuditRecord,
    Config,
    Synchronizer,
    extract_context,
    install,
    publish_audits,
    uninstall,
)
from openmetadata_sqlmesh.cli import main

PROJECT = Path(__file__).parents[1] / "examples/sqlmesh"


@contextmanager
def catalog_server(legacy=False):
    entities = {("services/databaseServices", "warehouse"): {"id": "warehouse"}}
    calls, errors = [], []
    edges = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, code, payload):
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        def do_GET(self):
            path = unquote(self.path.removeprefix("/api/v1/"))
            if path.startswith("lineage/table/"):
                target = path.split("lineage/table/", 1)[1].split("?", 1)[0]
                self.respond(
                    200,
                    {
                        "upstreamEdges": [
                            edge
                            for (_, to_id), edge in edges.items()
                            if to_id == target
                        ]
                    },
                )
                return
            collection, name = path.split("/name/", 1)
            entity = entities.get((collection, name))
            self.respond(
                404 if entity is None else 200, entity or {"message": "missing"}
            )

        def do_PUT(self):
            path = unquote(self.path.removeprefix("/api/v1/"))
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((path, payload, self.headers.get("Authorization")))
            if path.startswith("dataQuality/testCases/") and path.endswith(
                "/testCaseResult"
            ):
                self.respond(200 if legacy else 404, {})
                return
            if path == "lineage":
                edge = payload["edge"]
                known_ids = {entity["id"] for entity in entities.values()}
                if (
                    edge["fromEntity"]["id"] not in known_ids
                    or edge["toEntity"]["id"] not in known_ids
                ):
                    errors.append("Lineage references an unknown entity")
                for column in edge["lineageDetails"].get("columnsLineage", []):
                    if not all(
                        name.startswith("warehouse.memory.")
                        for name in column["fromColumns"] + [column["toColumn"]]
                    ):
                        errors.append("Column FQN lacks database service")
                source, target = edge["fromEntity"]["id"], edge["toEntity"]["id"]
                edges[(source, target)] = {
                    **edge,
                    "fromEntity": source,
                    "toEntity": target,
                }
                self.respond(200, {})
                return
            if path == "dataQuality/testSuites/executable":
                name = payload["executableEntityReference"]
            elif path == "dataQuality/testCases":
                if legacy and "testSuite" not in payload:
                    self.respond(400, {"message": "testSuite required"})
                    return
                if not legacy and "testSuite" in payload:
                    self.respond(400, {"message": "unknown property testSuite"})
                    return
                table_fqn = payload["entityLink"][len("<#E::table::") : -1]
                name = table_fqn + "." + payload["name"]
            else:
                parent = payload.get(
                    "databaseSchema", payload.get("database", payload.get("service"))
                )
                name = f"{parent}.{payload['name']}" if parent else payload["name"]
            entity = entities.setdefault((path, name), {"id": str(uuid4())})
            entity.update(payload, fullyQualifiedName=name)
            if path == "tables" and not payload.get("columns"):
                errors.append("Table schema is absent")
            self.respond(200, entity)

        def do_POST(self):
            path = unquote(self.path.removeprefix("/api/v1/"))
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((path, payload, self.headers.get("Authorization")))
            if legacy:
                self.respond(404, {})
                return
            if not {"timestamp", "testCaseStatus", "testResultValue"} <= payload.keys():
                errors.append("Missing required CreateTestCaseResult fields")
            name = path.removeprefix("dataQuality/testCases/testCaseResults/")
            if ("dataQuality/testCases", name) not in entities:
                errors.append("Result targets an unknown test case")
            self.respond(200, {})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls, errors
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_real_cli_sync_over_http_is_authenticated_and_idempotent(
    tmp_path, capsys, monkeypatch
):
    with catalog_server() as (url, calls, errors):
        config = tmp_path / "config.json"
        config.write_text(
            json.dumps({"url": url, "database_service": "warehouse", "retries": 0})
        )
        monkeypatch.setenv("OPENMETADATA_TOKEN", "local-test-token")
        args = ["sync", "--project", str(PROJECT), "--config", str(config)]
        assert main(args) == 0
        first = json.loads(capsys.readouterr().out)
        assert first["tables"] == 3
        assert first["lineage_edges"] == 2
        assert not first["errors"]
        assert not first["warnings"]
        assert main(args) == 0
        second = json.loads(capsys.readouterr().out)
        assert first == second
        assert all(auth == "Bearer local-test-token" for _, _, auth in calls)
        assert not errors


def test_real_duckdb_evaluation_reports_all_terminal_models(tmp_path):
    from sqlmesh import Context

    project = tmp_path / "project"
    shutil.copytree(
        PROJECT, project, ignore=shutil.ignore_patterns(".cache", "logs", "__pycache__")
    )
    client = MemoryClient()
    config = Config("https://om.example", "warehouse")
    context = None
    try:
        install(config, client=client, strict=True)
        with redirect_stdout(io.StringIO()):
            context = Context(paths=str(project))
            assert (
                not Synchronizer(config, client)
                .sync(extract_context(context, config))
                .errors
            )
            context.plan(
                environment="standalone_test",
                auto_apply=True,
                no_prompts=True,
                skip_tests=True,
                start="2024-01-01",
                end="2024-01-02",
            )
        statuses = [
            payload
            for method, path, payload in client.calls
            if path.endswith("/status")
        ]
        assert len(statuses) == 3
        assert all(status["executionStatus"] == "Successful" for status in statuses)
        assert all(
            status["taskStatus"][0]["endTime"] >= status["timestamp"]
            for status in statuses
        )
    finally:
        uninstall()
        if context is not None:
            context.close()


@pytest.mark.parametrize("legacy", [False, True])
def test_audit_publication_over_http_uses_verified_contracts(legacy):
    from sqlmesh import Context

    with catalog_server(legacy=legacy) as (url, calls, errors):
        config = Config(url, "warehouse", retries=0)
        context = Context(paths=str(PROJECT))
        try:
            assert (
                not Synchronizer(config).sync(extract_context(context, config)).errors
            )
        finally:
            context.close()
        record = AuditRecord(
            "memory.analytics.orders",
            "not_null",
            1793657000000,
            "SELECT * FROM memory.analytics.orders WHERE id IS NULL",
            count=0,
        )
        assert publish_audits([record], config) == {"published": 1, "errors": []}
        assert publish_audits([record], config) == {"published": 1, "errors": []}
        assert not errors
        results = [
            call
            for call in calls
            if call[0].endswith("testCaseResult") or "testCaseResults/" in call[0]
        ]
        assert results
