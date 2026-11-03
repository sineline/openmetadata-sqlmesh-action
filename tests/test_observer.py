import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from openmetadata_sqlmesh.config import Config
from openmetadata_sqlmesh.observer import SQLMeshObserver, install, uninstall


def observer(reporter=None, strict=False):
    wrapped = MagicMock()
    events = []
    instance = SQLMeshObserver(
        wrapped,
        Config("https://om.example", "warehouse"),
        reporter=reporter or events.append,
        strict=strict,
    )
    return instance, events, wrapped


def test_concurrent_batches_are_independent_and_all_complete():
    instance, events, wrapped = observer()
    snapshot = SimpleNamespace(name='"db"."schema"."orders"')
    barrier = threading.Barrier(8)

    def batch(index):
        instance.start_snapshot_evaluation_progress(snapshot)
        barrier.wait(timeout=5)
        instance.update_snapshot_evaluation_progress(snapshot, (0, 1), index, 12, 1, 0)

    threads = [threading.Thread(target=batch, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert len(events) == 8
    assert len({event.run_id for event in events}) == 8
    assert len({event.timestamp for event in events}) == 8
    assert all(event.status == "Successful" for event in events)
    assert not instance._pending
    assert wrapped.update_snapshot_evaluation_progress.call_count == 8


def test_audit_failures_and_abandoned_runs_are_failed():
    instance, events, wrapped = observer()
    snapshot = SimpleNamespace(name="db.schema.orders")
    instance.start_snapshot_evaluation_progress(snapshot)
    instance.update_snapshot_evaluation_progress(snapshot, (0, 1), 0, 1, 0, 1)
    instance.start_snapshot_evaluation_progress(snapshot)
    instance.stop_evaluation_progress(False)
    assert [event.status for event in events] == ["Failed", "Failed"]
    assert not instance._pending
    wrapped.stop_evaluation_progress.assert_called_once_with(False)


def test_query_errors_are_failed_even_when_no_audit_failed():
    instance, events, _ = observer()
    snapshot = SimpleNamespace(name="db.schema.orders")
    instance.start_snapshot_evaluation_progress(snapshot)
    # SQLMesh calls this in finally; a failed query leaves duration unset.
    instance.update_snapshot_evaluation_progress(snapshot, (0, 1), 0, None, 0, 0)
    assert events[0].status == "Failed"


def test_reporting_failure_is_visible_without_breaking_delegation(caplog):
    def broken(record):
        raise RuntimeError("reporting failed")

    instance, _, wrapped = observer(broken)
    snapshot = SimpleNamespace(name="db.schema.orders")
    instance.start_snapshot_evaluation_progress(snapshot)
    instance.update_snapshot_evaluation_progress(snapshot, (0, 1), 0, 1, 0, 0)
    assert "reporting failed" in caplog.text
    assert wrapped.update_snapshot_evaluation_progress.called
    instance.strict = True
    instance.start_snapshot_evaluation_progress(snapshot)
    with pytest.raises(RuntimeError, match="reporting failed"):
        instance.update_snapshot_evaluation_progress(snapshot, (0, 1), 0, 1, 0, 0)
    assert wrapped.update_snapshot_evaluation_progress.call_count == 2


def test_install_is_idempotent_restores_current_console_and_delegates():
    pytest.importorskip("sqlmesh")
    from sqlmesh.core.console import get_console

    original = get_console()
    config = Config("https://om.example", "warehouse")
    try:
        first = install(config)
        assert get_console() is first
        assert install(config) is first
        assert first.log_status_update == original.log_status_update
        assert uninstall() is True
        assert get_console() is original
        assert uninstall() is False
    finally:
        uninstall()


@pytest.mark.parametrize(
    "terminal", ["success", "audit_failure", "query_failure", "interrupted"]
)
def test_native_upstream_lineage_is_published_for_every_terminal_event(terminal):
    pytest.importorskip("sqlmesh")
    from pathlib import Path

    from sqlmesh import Context
    from test_sync import MemoryClient

    from openmetadata_sqlmesh import Synchronizer, extract_context

    client = MemoryClient()
    config = Config("https://om.example", "warehouse")
    context = Context(paths=str(Path(__file__).parents[1] / "examples/sqlmesh"))
    try:
        assert (
            not Synchronizer(config, client)
            .sync(extract_context(context, config))
            .errors
        )
        model = next(m for m in context.models.values() if m.fqn.endswith('."orders"'))
        snapshot = SimpleNamespace(name=model.fqn, model=model, is_model=True)
        instance = SQLMeshObserver(MagicMock(), config, client=client, strict=True)
        client.calls.clear()
        instance.start_snapshot_evaluation_progress(snapshot)
        if terminal == "interrupted":
            instance.stop_evaluation_progress(False)
        else:
            instance.update_snapshot_evaluation_progress(
                snapshot,
                (0, 1),
                0,
                None if terminal == "query_failure" else 1,
                0,
                1 if terminal == "audit_failure" else 0,
            )
        edges = [
            payload["edge"] for _, path, payload in client.calls if path == "/lineage"
        ]
        assert len(edges) == 1
        assert (
            edges[0]["fromEntity"]["id"]
            == client.entities[("tables", "warehouse.memory.raw.raw_orders")]["id"]
        )
        assert (
            edges[0]["toEntity"]["id"]
            == client.entities[("tables", "warehouse.memory.analytics.orders")]["id"]
        )
        assert edges[0]["lineageDetails"]["columnsLineage"]
        statuses = [
            payload for _, path, payload in client.calls if path.endswith("/status")
        ]
        assert statuses[0]["executionStatus"] == (
            "Successful" if terminal == "success" else "Failed"
        )
    finally:
        context.close()
