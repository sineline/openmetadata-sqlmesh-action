"""SQLMesh Console adapter for native OpenMetadata execution reporting."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import quote

from .client import OpenMetadataClient
from .config import Config
from .models import ProjectMetadata
from .sync import Synchronizer, pipeline_fqn

logger = logging.getLogger(__name__)
_install_lock = threading.RLock()


@dataclass(frozen=True)
class Execution:
    snapshot: Any
    run_id: str
    timestamp: int
    status: str = "Pending"
    end_time: int | None = None


class SQLMeshObserver:
    """Delegate to an existing console while tracking every evaluation batch."""

    def __init__(
        self,
        wrapped: Any,
        config: Config,
        *,
        client: OpenMetadataClient | None = None,
        strict: bool = False,
        reporter: Callable[[Execution], None] | None = None,
    ):
        self._wrapped, self.config, self.strict = wrapped, config, strict
        self.client = client if client is not None else OpenMetadataClient(config)
        self._reporter = reporter if reporter is not None else self._publish
        self._local = threading.local()
        self._lock = threading.RLock()
        self._publish_lock = threading.RLock()
        self._pending: dict[str, Execution] = {}
        self._timestamps: dict[str, int] = {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)

    def start_snapshot_evaluation_progress(
        self, snapshot: Any, audit_only: bool = False
    ) -> None:
        self._wrapped.start_snapshot_evaluation_progress(snapshot, audit_only)
        with self._lock:
            # OM statuses are keyed by millisecond timestamps. Allocate distinct
            # timestamps for concurrent batches of the same pipeline.
            timestamp = max(
                time.time_ns() // 1_000_000, self._timestamps.get(snapshot.name, 0) + 1
            )
            self._timestamps[snapshot.name] = timestamp
            record = Execution(snapshot, str(uuid.uuid4()), timestamp)
            self._pending[record.run_id] = record
        stack = getattr(self._local, "stack", None)
        if stack is None:
            stack = self._local.stack = []
        stack.append((snapshot.name, record.run_id))

    def _pop(self, snapshot: Any) -> Execution | None:
        stack = getattr(self._local, "stack", [])
        for index in range(len(stack) - 1, -1, -1):
            name, run_id = stack[index]
            if name == snapshot.name:
                stack.pop(index)
                with self._lock:
                    record = self._pending.pop(run_id, None)
                if record is not None:
                    return record
        return None

    def update_snapshot_evaluation_progress(
        self,
        snapshot: Any,
        interval: Any,
        batch_idx: int,
        duration_ms: int | None,
        num_audits_passed: int,
        num_audits_failed: int,
        audit_only: bool = False,
        execution_stats: Any = None,
        auto_restatement_triggers: Any = None,
    ) -> None:
        # Always preserve SQLMesh's console behavior, including in strict mode.
        self._wrapped.update_snapshot_evaluation_progress(
            snapshot,
            interval,
            batch_idx,
            duration_ms,
            num_audits_passed,
            num_audits_failed,
            audit_only,
            execution_stats,
            auto_restatement_triggers,
        )
        record = self._pop(snapshot)
        if record is not None:
            status = (
                "Failed" if num_audits_failed or duration_ms is None else "Successful"
            )
            self._report(
                replace(
                    record,
                    status=status,
                    end_time=max(record.timestamp, time.time_ns() // 1_000_000),
                )
            )

    def stop_evaluation_progress(self, success: bool = True) -> None:
        self._wrapped.stop_evaluation_progress(success)
        with self._lock:
            abandoned = list(self._pending.values())
            self._pending.clear()
        errors = []
        for record in abandoned:
            try:
                self._report(
                    replace(
                        record,
                        status="Failed",
                        end_time=max(record.timestamp, time.time_ns() // 1_000_000),
                    )
                )
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise errors[0]

    def _report(self, record: Execution) -> None:
        try:
            self._reporter(record)
        except Exception as exc:
            message = str(exc)
            if self.config.token:
                message = message.replace(self.config.token, "[REDACTED]")
            logger.warning(
                "OpenMetadata reporting failed for %s: %s",
                record.snapshot.name,
                message,
            )
            if self.strict:
                raise RuntimeError(message) from None

    def _publish(self, record: Execution) -> None:
        from .extract import extract_model

        if not getattr(record.snapshot, "is_model", True):
            return
        warnings: list[str] = []
        model = extract_model(record.snapshot.model, self.config, warnings)
        # Serialize writes inside this observer; batch identity stays independent.
        with self._publish_lock:
            synchronizer = Synchronizer(self.config, self.client)
            report = synchronizer.sync(ProjectMetadata((model,), tuple(warnings)))
            for warning in report.warnings:
                logger.warning("%s", warning)
            if report.errors:
                raise RuntimeError("; ".join(report.errors))
            self.client.request(
                "PUT",
                "/pipelines/"
                + quote(pipeline_fqn(model.table, self.config), safe="")
                + "/status",
                {
                    "timestamp": record.timestamp,
                    "executionStatus": record.status,
                    "taskStatus": [
                        {
                            "name": "evaluate",
                            "executionStatus": record.status,
                            "startTime": record.timestamp,
                            "endTime": record.end_time,
                        }
                    ],
                },
            )


def install(
    config: Config, *, strict: bool = False, client: OpenMetadataClient | None = None
) -> SQLMeshObserver:
    """Wrap the current SQLMesh Console. Call before constructing a Context."""
    from sqlmesh.core.console import get_console, set_console

    with _install_lock:
        current = get_console()
        if isinstance(current, SQLMeshObserver):
            if current.config != config or current.strict != strict:
                raise ValueError(
                    "An observer with different configuration is installed; uninstall it first"
                )
            return current
        observer = SQLMeshObserver(current, config, strict=strict, client=client)
        set_console(observer)
        return observer


def uninstall() -> bool:
    """Restore the wrapped console without replacing another installed console."""
    from sqlmesh.core.console import get_console, set_console

    with _install_lock:
        current = get_console()
        if not isinstance(current, SQLMeshObserver):
            return False
        try:
            if current._pending:
                current.stop_evaluation_progress(False)
        finally:
            set_console(current._wrapped)
        return True
