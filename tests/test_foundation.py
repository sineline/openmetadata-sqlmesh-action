import io
import json
import math
from dataclasses import FrozenInstanceError
from email.message import Message
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPHandler
from urllib.response import addinfourl

import pytest

from openmetadata_sqlmesh.client import APIError, OpenMetadataClient
from openmetadata_sqlmesh.config import Config
from openmetadata_sqlmesh.names import TableName, fqn, parse_table


def make_config(**changes):
    return Config(
        url="https://metadata.example", database_service="warehouse", **changes
    )


def test_config_has_safe_defaults_and_is_frozen():
    config = make_config(token="a-library-secret")
    assert config.pipeline_service == "sqlmesh"
    assert config.catalog_services == {}
    assert config.timeout == 10
    assert config.retries == 2
    assert config.token_env == "OPENMETADATA_TOKEN"
    assert "a-library-secret" not in repr(config)
    with pytest.raises(FrozenInstanceError):
        config.url = "https://other.example"


@pytest.mark.parametrize(
    "suffix", ["", "/", "/api", "/api/", "/api/v1", "/api/v1/", "/api/v1/lineage"]
)
def test_config_normalizes_supported_url_forms(suffix):
    config = Config(
        url="https://metadata.example/proxy" + suffix, database_service="warehouse"
    )
    assert config.url == "https://metadata.example/proxy"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "metadata.example",
        "ftp://metadata.example",
        "https:///path",
        "https://user:secret@metadata.example",
        "https://metadata.example?token=secret",
        "https://metadata.example#secret",
        "https://metadata.example?",
        "https://metadata.example#",
        "https://meta data.example",
        "https://metadata.example:bad",
        "https://metadata.example\n",
    ],
)
def test_config_rejects_invalid_or_sensitive_urls_without_echoing_them(url):
    with pytest.raises(ValueError) as caught:
        Config(url=url, database_service="warehouse")
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("database_service", ""),
        ("database_service", None),
        ("pipeline_service", " "),
        ("default_database", ""),
        ("default_schema", 3),
        ("token_env", []),
        ("token_env", ""),
        ("catalog_services", []),
        ("catalog_services", {"catalog": ""}),
        ("catalog_services", {1: "service"}),
        ("timeout", 0),
        ("timeout", -1),
        ("timeout", math.inf),
        ("timeout", math.nan),
        ("timeout", True),
        ("timeout", "10"),
        ("retries", -1),
        ("retries", 1.5),
        ("retries", True),
        ("retries", 1000),
        ("token", "\nsecret"),
    ],
)
def test_config_validates_runtime_types_and_bounds(field, value):
    values = {
        "url": "https://metadata.example",
        "database_service": "warehouse",
        field: value,
    }
    with pytest.raises(ValueError):
        Config(**values)


def test_catalog_service_mapping_is_explicit_and_copied():
    overrides = {"external": "external-service"}
    config = make_config(catalog_services=overrides)
    overrides["external"] = "changed"
    assert config.service_for("external") == "external-service"
    assert config.service_for("analytics") == "warehouse"
    assert make_config().catalog_services is not make_config().catalog_services


def test_config_file_applies_url_and_configured_token_environment(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "url": "https://file.example/api",
                "database_service": "warehouse",
                "token_env": "MY_OM_TOKEN",
            }
        )
    )
    config = Config.from_file(
        path,
        environ={
            "OPENMETADATA_URL": "https://env.example/api/v1",
            "MY_OM_TOKEN": "env-secret",
        },
    )
    assert config.url == "https://env.example"
    assert config.token == "env-secret"
    assert "env-secret" not in repr(config)


def test_config_file_uses_default_token_environment(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"url": "https://file.example", "database_service": "warehouse"})
    )
    assert (
        Config.from_file(
            path, environ={"OPENMETADATA_TOKEN": "environment-token"}
        ).token
        == "environment-token"
    )


def test_config_file_normalizes_url_only_once_when_loading_a_token(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "url": "https://metadata.example/proxy/api/api/v1",
                "database_service": "warehouse",
            }
        )
    )
    config = Config.from_file(path, environ={"OPENMETADATA_TOKEN": "environment-token"})
    assert config.url == "https://metadata.example/proxy/api"


def test_config_rejects_timeouts_too_large_for_the_http_runtime():
    with pytest.raises(ValueError):
        make_config(timeout=10**400)


@pytest.mark.parametrize(
    "extra", [{"unknown": True}, {"token": "file-secret"}, {"token": None}]
)
def test_config_file_rejects_unknown_keys_and_literal_credentials(tmp_path, extra):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "url": "https://metadata.example",
                "database_service": "warehouse",
                **extra,
            }
        )
    )
    with pytest.raises(ValueError) as caught:
        Config.from_file(path, environ={})
    assert "file-secret" not in str(caught.value)


@pytest.mark.parametrize("content", ["[", "[]", "null", '{"url":"file-secret",}'])
def test_config_file_reports_invalid_json_safely(tmp_path, content):
    path = tmp_path / "config.json"
    path.write_text(content)
    with pytest.raises(ValueError) as caught:
        Config.from_file(path, environ={})
    assert "file-secret" not in str(caught.value)


def test_fqn_preserves_case_and_quotes_each_dotted_component():
    assert (
        fqn("Warehouse", "catalog.name", "Sales", "order.items")
        == 'Warehouse."catalog.name".Sales."order.items"'
    )
    assert fqn("service", 'a"b', "table") == 'service."a""b".table'


@pytest.mark.parametrize("parts", [(), ("",), ("service", None), ("service", 4)])
def test_fqn_requires_nonempty_identifier_parts(parts):
    with pytest.raises(ValueError):
        fqn(*parts)


@pytest.mark.parametrize(
    "name,dialect,expected",
    [
        ('"Catalog"."Sales"."order.items"', "", ("Catalog", "Sales", "order.items")),
        (
            "`catalog.name`.`sales`.`orders`",
            "mysql",
            ("catalog.name", "sales", "orders"),
        ),
        (
            "[Catalog].[Sales].[order.items]",
            "tsql",
            ("Catalog", "Sales", "order.items"),
        ),
        ('"cat"."s"."a""b"', "postgres", ("cat", "s", 'a"b')),
        ("`cat`.`s`.`a``b`", "mysql", ("cat", "s", "a`b")),
        ("[cat].[s].[a]]b]", "tsql", ("cat", "s", "a]b")),
        ('  "cat" . sales . "A Table"  ', "", ("cat", "sales", "A Table")),
        ("cat.sales.Orders", "", ("cat", "sales", "Orders")),
    ],
)
def test_parse_table_preserves_quoted_components(name, dialect, expected):
    table = parse_table(name, make_config(), dialect=dialect)
    assert (table.database, table.schema, table.table) == expected


def test_table_identity_uses_database_service_override_and_qualified_sql():
    config = make_config(catalog_services={"cat.name": "other.service"})
    table = parse_table('"cat.name".Sales."order.items"', config)
    assert table.database_fqn(config) == '"other.service"."cat.name"'
    assert table.schema_fqn(config) == '"other.service"."cat.name".Sales'
    assert table.fqn(config) == '"other.service"."cat.name".Sales."order.items"'
    assert table.sql == '"cat.name"."Sales"."order.items"'
    assert parse_table(table.sql, config) == table


def test_qualified_sql_escapes_embedded_double_quotes():
    table = TableName("cat", "schema", 'a"b')
    assert table.sql == '"cat"."schema"."a""b"'
    assert parse_table(table.sql, make_config()) == table


def test_parse_table_uses_only_explicit_database_and_schema_defaults():
    config = make_config(default_database="catalog", default_schema="sales")
    assert parse_table("orders", config) == TableName("catalog", "sales", "orders")
    assert parse_table("other.orders", config) == TableName(
        "catalog", "other", "orders"
    )
    assert parse_table("explicit.other.orders", config) == TableName(
        "explicit", "other", "orders"
    )


@pytest.mark.parametrize(
    "name,defaults",
    [
        ("orders", {}),
        ("sales.orders", {}),
        ("orders", {"default_database": "cat"}),
        ("orders", {"default_schema": "sales"}),
        ("", {}),
        ("cat..orders", {}),
        ("cat.sales.", {}),
        ("a.b.c.d", {}),
        ('"cat.sales.orders', {}),
        ('cat.sales.""', {}),
        ('cat.sales."orders"extra', {}),
        ("cat.sale s.orders", {}),
    ],
)
def test_parse_table_rejects_incomplete_or_malformed_names(name, defaults):
    with pytest.raises(ValueError):
        parse_table(name, make_config(**defaults))


def test_table_name_is_frozen():
    table = TableName("cat", "sales", "orders")
    with pytest.raises(FrozenInstanceError):
        table.schema = "other"


class Response(io.BytesIO):
    def __init__(self, body=b"{}", status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = headers or {}


class ScriptedOpener:
    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls = []

    def __call__(self, request, *, timeout):
        self.calls.append((request, timeout))
        result = self.steps.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def http_error(
    status,
    *,
    headers=None,
    body=b"untrusted-body-secret",
    reason="untrusted-error-secret",
):
    return HTTPError(
        "https://metadata.example/api/v1/tables",
        status,
        reason,
        headers or {},
        io.BytesIO(body),
    )


@pytest.mark.parametrize("suffix", ["", "/api", "/api/v1", "/api/v1/lineage"])
def test_client_has_one_normalized_api_base(suffix):
    config = Config(
        url="https://metadata.example/proxy" + suffix, database_service="warehouse"
    )
    assert (
        OpenMetadataClient(config).api_base == "https://metadata.example/proxy/api/v1"
    )


def test_client_sends_authenticated_json_with_timeout():
    opener = ScriptedOpener(Response(b'{"id":"table-id"}'))
    config = make_config(token="token-secret", timeout=7.5)
    client = OpenMetadataClient(config, opener=opener)
    assert client.config is config
    assert client.upsert("tables", {"name": "orders"}) == {"id": "table-id"}
    request, timeout = opener.calls[0]
    assert request.full_url == "https://metadata.example/api/v1/tables"
    assert request.get_method() == "PUT"
    assert request.get_header("Authorization") == "Bearer token-secret"
    assert request.get_header("Content-type") == "application/json"
    assert request.get_header("Accept") == "application/json"
    assert json.loads(request.data) == {"name": "orders"}
    assert timeout == 7.5


def test_client_omits_authentication_when_unconfigured():
    opener = ScriptedOpener(Response())
    OpenMetadataClient(make_config(), opener=opener).request("GET", "/tables")
    assert opener.calls[0][0].get_header("Authorization") is None


def test_get_by_name_encodes_the_entire_fqn_as_one_path_component():
    opener = ScriptedOpener(Response(b'{"id":"found"}'))
    name = 'warehouse.catalog.sales."a/b.c"'
    client = OpenMetadataClient(make_config(), opener=opener)
    assert client.get_by_name("tables", name) == {"id": "found"}
    assert opener.calls[0][
        0
    ].full_url == "https://metadata.example/api/v1/tables/name/" + quote(name, safe="")


def test_only_404_lookup_is_missing_and_is_not_cached():
    opener = ScriptedOpener(http_error(404), Response(b'{"id":"new"}'), http_error(404))
    client = OpenMetadataClient(make_config(), opener=opener)
    assert client.get_by_name("tables", "warehouse.cat.sales.orders") is None
    assert client.get_by_name("tables", "warehouse.cat.sales.orders") == {"id": "new"}
    assert client.request("GET", "tables/name/missing", missing_ok=True) is None
    assert len(opener.calls) == 3


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
def test_client_never_retries_permanent_http_errors(status):
    opener = ScriptedOpener(http_error(status))
    sleeps = []
    client = OpenMetadataClient(make_config(), opener=opener, sleep=sleeps.append)
    with pytest.raises(APIError) as caught:
        client.request("GET", "tables")
    assert caught.value.status == status
    assert caught.value.method == "GET"
    assert caught.value.path == "tables"
    assert len(opener.calls) == 1
    assert sleeps == []


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_client_recovers_from_retryable_http_errors(status):
    opener = ScriptedOpener(
        http_error(status), http_error(status), Response(b'{"id":"recovered"}')
    )
    sleeps = []
    client = OpenMetadataClient(make_config(), opener=opener, sleep=sleeps.append)
    assert client.get_by_name("tables", "warehouse.cat.sales.orders") == {
        "id": "recovered"
    }
    assert len(opener.calls) == 3
    assert len(sleeps) == 2


def test_client_limits_retries_to_configured_additional_attempts():
    opener = ScriptedOpener(http_error(503), http_error(503), http_error(503))
    client = OpenMetadataClient(
        make_config(retries=2), opener=opener, sleep=lambda _: None
    )
    with pytest.raises(APIError) as caught:
        client.get_by_name("tables", "warehouse.cat.sales.orders")
    assert caught.value.status == 503
    assert len(opener.calls) == 3


def test_transient_lookup_failure_is_not_cached_as_missing():
    opener = ScriptedOpener(http_error(503), Response(b'{"id":"recovered"}'))
    client = OpenMetadataClient(make_config(retries=0), opener=opener)
    with pytest.raises(APIError):
        client.get_by_name("tables", "warehouse.cat.sales.orders")
    assert client.get_by_name("tables", "warehouse.cat.sales.orders") == {
        "id": "recovered"
    }


@pytest.mark.parametrize(
    "failure",
    [
        URLError("token-secret"),
        TimeoutError("token-secret"),
        ConnectionResetError("token-secret"),
    ],
)
def test_client_retries_transport_failures(failure):
    opener = ScriptedOpener(failure, Response(b'{"ok":true}'))
    client = OpenMetadataClient(
        make_config(token="token-secret"), opener=opener, sleep=lambda _: None
    )
    assert client.request("GET", "tables") == {"ok": True}
    assert len(opener.calls) == 2


def test_transport_exhaustion_is_safe_and_not_missing():
    opener = ScriptedOpener(URLError("token-secret"))
    client = OpenMetadataClient(
        make_config(token="token-secret", retries=0), opener=opener
    )
    with pytest.raises(APIError) as caught:
        client.get_by_name("tables", "warehouse.cat.sales.orders")
    assert caught.value.status is None
    assert "token-secret" not in str(caught.value)
    assert "token-secret" not in repr(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("retry_after,expected", [("2", 2), ("999999", 30)])
def test_client_respects_bounded_retry_after(retry_after, expected):
    opener = ScriptedOpener(
        http_error(429, headers={"Retry-After": retry_after}), Response()
    )
    sleeps = []
    client = OpenMetadataClient(make_config(), opener=opener, sleep=sleeps.append)
    assert client.request("GET", "tables") == {}
    assert sleeps == [expected]


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_client_treats_redirects_as_permanent_failures(status):
    opener = ScriptedOpener(
        http_error(status, headers={"Location": "https://other.example/token-secret"})
    )
    client = OpenMetadataClient(
        make_config(token="token-secret"), opener=opener, sleep=lambda _: None
    )
    with pytest.raises(APIError) as caught:
        client.request("GET", "tables")
    assert caught.value.status == status
    assert "token-secret" not in str(caught.value)
    assert len(opener.calls) == 1


def test_default_http_opener_does_not_forward_authorization_on_redirect(monkeypatch):
    requests = []

    def redirect_response(handler, request):
        requests.append(request)
        headers = Message()
        headers["Location"] = "http://other.example/steal"
        response = addinfourl(io.BytesIO(b""), headers, request.full_url, 302)
        response.msg = "Found"
        return response

    monkeypatch.setattr(HTTPHandler, "http_open", redirect_response)
    config = Config(
        url="http://metadata.example",
        database_service="warehouse",
        token="token-secret",
    )
    with pytest.raises(APIError) as caught:
        OpenMetadataClient(config).request("GET", "tables")
    assert caught.value.status == 302
    assert len(requests) == 1
    assert requests[0].full_url == "http://metadata.example/api/v1/tables"


@pytest.mark.parametrize(
    "body",
    [b"not-json-secret", b'{"broken":', b"[]", b"null", b'{"number":NaN}', b"\xff"],
)
def test_client_rejects_invalid_success_json_without_echoing_body(body):
    opener = ScriptedOpener(Response(body))
    client = OpenMetadataClient(make_config(), opener=opener)
    with pytest.raises(APIError) as caught:
        client.request("GET", "tables")
    assert "not-json-secret" not in str(caught.value)
    assert len(opener.calls) == 1


@pytest.mark.parametrize("body", [b" ", b"\n", b"\r\n\t"])
def test_client_rejects_nonempty_whitespace_success_response(body):
    opener = ScriptedOpener(Response(body, status=200))
    sleeps = []
    client = OpenMetadataClient(make_config(), opener=opener, sleep=sleeps.append)
    with pytest.raises(APIError, match="response is not valid JSON") as caught:
        client.request("GET", "tables")
    assert caught.value.status == 200
    assert len(opener.calls) == 1
    assert sleeps == []


@pytest.mark.parametrize("status", [200, 204])
def test_empty_successful_response_returns_an_empty_object(status):
    opener = ScriptedOpener(Response(b"", status=status))
    assert (
        OpenMetadataClient(make_config(), opener=opener).request("PUT", "lineage", {})
        == {}
    )


def test_client_redacts_known_token_in_error_path_and_drops_untrusted_diagnostics():
    opener = ScriptedOpener(http_error(403))
    client = OpenMetadataClient(make_config(token="token-secret"), opener=opener)
    with pytest.raises(APIError) as caught:
        client.request("GET", "tables/token-secret?credentials=unknown-query-secret")
    combined = str(caught.value) + repr(caught.value) + caught.value.path
    for secret in (
        "token-secret",
        "untrusted-body-secret",
        "untrusted-error-secret",
        "unknown-query-secret",
    ):
        assert secret not in combined


def test_client_redacts_percent_encoded_tokens_independently_of_hex_case():
    opener = ScriptedOpener(http_error(403))
    client = OpenMetadataClient(make_config(token="token/secret"), opener=opener)
    with pytest.raises(APIError) as caught:
        client.request("GET", "tables/token%2fsecret")
    assert "token%2fsecret" not in str(caught.value)
    assert "token/secret" not in str(caught.value)


def test_client_reports_header_encoding_errors_without_exposing_the_token():
    opener = ScriptedOpener(
        UnicodeEncodeError("ascii", "token-secret-☃", 13, 14, "invalid header")
    )
    client = OpenMetadataClient(make_config(token="token-secret-☃"), opener=opener)
    with pytest.raises(APIError) as caught:
        client.request("GET", "tables")
    assert "token-secret" not in str(caught.value)
    assert len(opener.calls) == 1


@pytest.mark.parametrize(
    "path",
    [
        "https://other.example/tables",
        "//other.example/tables",
        "tables\r\nAuthorization: token-secret",
        "tables#token-secret",
    ],
)
def test_client_rejects_absolute_or_unsafe_request_paths(path):
    opener = ScriptedOpener()
    client = OpenMetadataClient(make_config(token="token-secret"), opener=opener)
    with pytest.raises(ValueError) as caught:
        client.request("GET", path)
    assert "token-secret" not in str(caught.value)
    assert opener.calls == []
