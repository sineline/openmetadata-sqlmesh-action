"""Validated configuration without a dependency on SQLMesh or its SDKs."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def _require_text(name: str, value: object) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError(f"{name} must be a nonempty string without control characters")


def _server_root(url: str) -> str:
    _require_text("url", url)
    if any(char.isspace() for char in url) or "\\" in url or "?" in url or "#" in url:
        raise ValueError(
            "url must not contain whitespace, credentials, a query, or a fragment"
        )
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme in {"http", "https"} and parsed.hostname and parsed.port != 0
        )
    except ValueError:
        raise ValueError("url must be a valid HTTP or HTTPS URL") from None
    if not valid or "@" in parsed.netloc:
        raise ValueError(
            "url must be an HTTP or HTTPS URL without embedded credentials"
        )
    path = parsed.path.rstrip("/")
    for suffix in ("/api/v1/lineage", "/api/v1", "/api"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


@dataclass(frozen=True)
class Config:
    url: str
    database_service: str
    pipeline_service: str = "sqlmesh"
    default_database: str | None = None
    default_schema: str | None = None
    catalog_services: dict[str, str] = field(default_factory=dict)
    token: str | None = field(default=None, repr=False)
    token_env: str = "OPENMETADATA_TOKEN"
    timeout: float = 10
    retries: int = 2

    def __post_init__(self) -> None:
        object.__setattr__(self, "url", _server_root(self.url))
        for name in ("database_service", "pipeline_service", "token_env"):
            _require_text(name, getattr(self, name))
        for name in ("default_database", "default_schema", "token"):
            value = getattr(self, name)
            if value is not None:
                _require_text(name, value)
        if not isinstance(self.catalog_services, Mapping):
            raise ValueError(
                "catalog_services must be a mapping of catalog names to services"
            )
        for catalog, service in self.catalog_services.items():
            _require_text("catalog_services catalog", catalog)
            _require_text("catalog_services service", service)
        object.__setattr__(self, "catalog_services", dict(self.catalog_services))
        try:
            valid_timeout = (
                not isinstance(self.timeout, bool)
                and isinstance(self.timeout, (int, float))
                and math.isfinite(self.timeout)
                and self.timeout > 0
            )
        except OverflowError:
            valid_timeout = False
        if not valid_timeout:
            raise ValueError("timeout must be a positive finite number")
        if (
            isinstance(self.retries, bool)
            or not isinstance(self.retries, int)
            or not 0 <= self.retries <= 10
        ):
            raise ValueError("retries must be an integer between 0 and 10")

    def service_for(self, database: str) -> str:
        return self.catalog_services.get(database, self.database_service)

    @classmethod
    def from_file(
        cls, path: str | os.PathLike[str], *, environ: Mapping[str, str] | None = None
    ) -> Config:
        try:
            values = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise ValueError("cannot read a valid JSON configuration file") from None
        if not isinstance(values, dict):
            raise ValueError("configuration must be a JSON object")
        if "token" in values:
            raise ValueError(
                "configuration files must use token_env for authentication"
            )
        if set(values) - {item.name for item in fields(cls)}:
            raise ValueError("unknown configuration fields")
        environment = os.environ if environ is None else environ
        if not isinstance(environment, Mapping):
            raise ValueError("environ must be a mapping")
        if "OPENMETADATA_URL" in environment:
            values["url"] = environment["OPENMETADATA_URL"]
        token_env = values.get("token_env", "OPENMETADATA_TOKEN")
        _require_text("token_env", token_env)
        values["token"] = environment.get(token_env) or environment.get(
            "OPENMETADATA_AUTH_TOKEN"
        )
        try:
            return cls(**values)
        except TypeError:
            raise ValueError(
                "configuration requires url and database_service"
            ) from None
