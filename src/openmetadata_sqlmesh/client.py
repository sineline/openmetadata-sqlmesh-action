"""Small authenticated OpenMetadata REST client with bounded retries."""

from __future__ import annotations

import json
import math
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import Config


class APIError(RuntimeError):
    """An API failure with a safe diagnostic and optional HTTP status."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        method: str = "",
        path: str = "",
    ):
        self.status = status
        self.method = method
        self.path = path
        prefix = f"OpenMetadata {method} {path}: " if method else ""
        status_text = f" (HTTP {status})" if status is not None else ""
        super().__init__(prefix + message + status_text)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _invalid_json_constant(value: str) -> None:
    raise ValueError("invalid JSON constant")


class OpenMetadataClient:
    def __init__(self, config: Config, *, opener=None, sleep=None):
        self.config = config
        self.api_base = config.url + "/api/v1"
        self._open = opener if opener is not None else build_opener(_NoRedirect()).open
        self._sleep = sleep if sleep is not None else time.sleep

    def _error(
        self, method: str, path: str, message: str, status: int | None = None
    ) -> APIError:
        safe_path = unquote(path.split("?", 1)[0])
        if self.config.token:
            for secret in {
                self.config.token,
                quote(self.config.token, safe=""),
                unquote(self.config.token),
            }:
                safe_path = safe_path.replace(secret, "[REDACTED]")
                message = message.replace(secret, "[REDACTED]")
        return APIError(message, status=status, method=method, path=safe_path)

    @staticmethod
    def _retry_delay(headers, attempt: int) -> float:
        delay = min(0.5 * (2**attempt), 30.0)
        retry_after = (
            headers.get("Retry-After") or headers.get("retry-after")
            if headers
            else None
        )
        if retry_after is not None:
            try:
                supplied = float(retry_after)
            except (TypeError, ValueError, OverflowError):
                try:
                    when = parsedate_to_datetime(retry_after)
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=timezone.utc)
                    supplied = (when - datetime.now(timezone.utc)).total_seconds()
                except (TypeError, ValueError, OverflowError):
                    return delay
            if math.isfinite(supplied):
                return min(max(supplied, 0.0), 30.0)
        return delay

    def request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        *,
        missing_ok: bool = False,
    ) -> dict | None:
        if not isinstance(method, str) or not re.fullmatch(r"[A-Za-z]+", method):
            raise ValueError("HTTP method must contain only letters")
        method = method.upper()
        if (
            not isinstance(path, str)
            or not path
            or any(
                char.isspace() or ord(char) < 32 or ord(char) == 127 for char in path
            )
        ):
            raise ValueError(
                "API path must be a nonempty relative path without whitespace"
            )
        try:
            parsed_path = urlsplit(path)
        except ValueError:
            raise ValueError(
                "API path must be relative and contain no fragment"
            ) from None
        if (
            parsed_path.scheme
            or parsed_path.netloc
            or path.startswith("//")
            or "#" in path
        ):
            raise ValueError("API path must be relative and contain no fragment")
        data = None
        if payload is not None:
            if not isinstance(payload, dict):
                raise ValueError("API payload must be a JSON object")
            try:
                data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode(
                    "utf-8"
                )
            except (TypeError, ValueError, UnicodeError):
                raise ValueError("API payload must be JSON serializable") from None
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self.config.token:
            headers["Authorization"] = "Bearer " + self.config.token
        request = Request(
            self.api_base + "/" + path.lstrip("/"),
            data=data,
            headers=headers,
            method=method,
        )
        for attempt in range(self.config.retries + 1):
            try:
                response = self._open(request, timeout=self.config.timeout)
                try:
                    status = response.status
                    response_headers = response.headers
                    body = response.read() if 200 <= status < 300 else b""
                finally:
                    response.close()
            except HTTPError as error:
                status, response_headers = error.code, error.headers
                error.close()
            except (URLError, OSError, HTTPException):
                if attempt < self.config.retries:
                    self._sleep(self._retry_delay(None, attempt))
                    continue
                raise self._error(method, path, "transport failure") from None
            except UnicodeError:
                raise self._error(
                    method, path, "request could not be encoded"
                ) from None
            else:
                if 200 <= status < 300:
                    if not body:
                        return {}
                    try:
                        result = json.loads(body, parse_constant=_invalid_json_constant)
                    except (ValueError, TypeError, UnicodeError):
                        raise self._error(
                            method, path, "response is not valid JSON", status
                        ) from None
                    if not isinstance(result, dict):
                        raise self._error(
                            method, path, "response JSON must be an object", status
                        )
                    return result
            if status == 404 and missing_ok:
                return None
            if (status == 429 or 500 <= status < 600) and attempt < self.config.retries:
                self._sleep(self._retry_delay(response_headers, attempt))
                continue
            message = (
                "redirects are disabled"
                if 300 <= status < 400
                else "HTTP request failed"
            )
            raise self._error(method, path, message, status) from None
        raise AssertionError("retry loop exhausted without a result")

    def get_by_name(self, collection: str, name: str) -> dict | None:
        return self.request(
            "GET",
            collection.strip("/") + "/name/" + quote(name, safe=""),
            missing_ok=True,
        )

    def upsert(self, collection: str, payload: dict) -> dict:
        result = self.request("PUT", collection, payload)
        assert result is not None
        return result
