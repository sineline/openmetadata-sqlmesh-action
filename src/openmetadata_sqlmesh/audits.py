"""Publish supplied audit results; this module does not execute warehouse SQL."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from .client import APIError, OpenMetadataClient
from .config import Config
from .names import fqn, parse_table


@dataclass(frozen=True)
class AuditRecord:
    model: str
    audit: str
    timestamp: int
    query: str | None = None
    count: int | None = None
    status: str | None = None
    message: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.model, str)
            or not self.model.strip()
            or not isinstance(self.audit, str)
            or not self.audit.strip()
        ):
            raise ValueError("Audit model and name must be nonempty strings")
        if (
            isinstance(self.timestamp, bool)
            or not isinstance(self.timestamp, int)
            or self.timestamp < 0
        ):
            raise ValueError(
                "Audit timestamp must be a nonnegative integer in epoch milliseconds"
            )
        if self.count is not None and (
            isinstance(self.count, bool)
            or not isinstance(self.count, int)
            or self.count < 0
        ):
            raise ValueError("Audit count must be a nonnegative integer or null")
        if self.status not in {None, "Success", "Failed", "Aborted"}:
            raise ValueError("Audit status must be Success, Failed, or Aborted")
        if self.status == "Success" and self.count:
            raise ValueError("A successful audit cannot have failing rows")
        if self.status == "Failed" and self.count == 0:
            raise ValueError("A failed audit cannot have zero failing rows")
        if self.query is not None and (
            not isinstance(self.query, str) or not self.query.strip()
        ):
            raise ValueError("Audit query must be a nonempty SQL string or null")
        if self.message is not None and not isinstance(self.message, str):
            raise ValueError("Audit message must be a string or null")

    @classmethod
    def from_sqlmesh(cls, result: Any, *, timestamp: int) -> AuditRecord:
        if result.model is None:
            raise ValueError(
                "A model-scoped SQLMesh audit result is required; use an explicit AuditRecord for standalone audits"
            )
        audit = result.audit.name
        # Repeated audit definitions with different arguments are separate cases.
        if result.audit_args:
            arguments = json.dumps(
                {
                    str(k): v.sql() if hasattr(v, "sql") else str(v)
                    for k, v in result.audit_args.items()
                },
                sort_keys=True,
            )
            audit += "_" + hashlib.sha256(arguments.encode()).hexdigest()[:10]
        return cls(
            model=result.model.fqn,
            audit=audit,
            timestamp=timestamp,
            query=result.query.sql(dialect=result.model.dialect)
            if result.query is not None
            else None,
            count=None if result.skipped else result.count,
            status="Aborted" if result.skipped else None,
        )

    def result_payload(self) -> dict[str, Any]:
        status = self.status or (
            "Aborted" if self.count is None else "Failed" if self.count else "Success"
        )
        payload: dict[str, Any] = {
            "timestamp": self.timestamp,
            "testCaseStatus": status,
            "testResultValue": []
            if self.count is None
            else [{"name": "failedRows", "value": str(self.count)}],
            "result": self.message
            or (
                f"{self.count} failing rows"
                if self.count is not None
                else "Audit result unavailable or skipped"
            ),
        }
        if self.count is not None:
            payload["failedRows"] = self.count
        return payload


def _case_name(audit: str) -> str:
    readable = re.sub(r"[^A-Za-z0-9_-]", "_", audit)[:80]
    return f"sqlmesh_{readable}_{hashlib.sha256(audit.encode()).hexdigest()[:10]}"


def publish_audits(
    records: Iterable[AuditRecord],
    config: Config,
    *,
    client: OpenMetadataClient | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    records = list(records)
    if dry_run:
        return {
            "version": 1,
            "audit_results": [
                {
                    "model": parse_table(record.model, config).fqn(config),
                    "audit": record.audit,
                    **record.result_payload(),
                }
                for record in records
            ],
        }
    client = client if client is not None else OpenMetadataClient(config)
    report: dict[str, Any] = {"published": 0, "errors": []}
    for record in records:
        try:
            table = parse_table(record.model, config)
            table_name = table.fqn(config)
            if client.get_by_name("tables", table_name) is None:
                raise ValueError(
                    f"Model table {table_name} does not exist; synchronize metadata first"
                )
            name = _case_name(record.audit)
            case_fqn = table_name + "." + fqn(name)
            existing = client.get_by_name("dataQuality/testCases", case_fqn)
            if record.query is None:
                if existing is None:
                    raise ValueError(
                        f"Audit {record.audit} has no query and no existing test case"
                    )
            else:
                suite = client.upsert(
                    "dataQuality/testSuites/executable",
                    {
                        "name": table_name,
                        "executableEntityReference": table_name,
                    },
                )
                payload = {
                    "name": name,
                    "displayName": record.audit,
                    "description": f"SQLMesh audit {record.audit}",
                    "testDefinition": "tableCustomSQLQuery",
                    "entityLink": f"<#E::table::{table_name}>",
                    "parameterValues": [
                        {"name": "sqlExpression", "value": record.query},
                        {"name": "strategy", "value": "ROWS"},
                        {"name": "threshold", "value": "0"},
                    ],
                }
                try:
                    client.upsert("dataQuality/testCases", payload)
                except APIError as exc:
                    if exc.status != 400:
                        raise
                    # OM 1.6 requires testSuite; newer schemas remove the field.
                    client.upsert(
                        "dataQuality/testCases",
                        {
                            **payload,
                            "testSuite": suite.get("fullyQualifiedName", table_name),
                        },
                    )
            encoded = quote(case_fqn, safe="")
            result = record.result_payload()
            try:
                client.request(
                    "POST", f"/dataQuality/testCases/testCaseResults/{encoded}", result
                )
            except APIError as exc:
                if exc.status not in {404, 405}:
                    raise
                client.request(
                    "PUT", f"/dataQuality/testCases/{encoded}/testCaseResult", result
                )
            report["published"] += 1
        except Exception as exc:
            message = str(exc)
            report["errors"].append(
                message.replace(config.token, "[REDACTED]") if config.token else message
            )
    return report
