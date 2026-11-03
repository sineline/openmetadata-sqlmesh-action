import ipaddress
import os
import socket

import pytest

# Set before importing SQLMesh, including during collection.
os.environ.setdefault("SQLMESH__DISABLE_ANONYMIZED_ANALYTICS", "1")
# Loading schemas must not fork while the contract HTTP server has a thread.
# Runtime batch concurrency is exercised separately by the observer tests.
os.environ.setdefault("MAX_FORK_WORKERS", "1")


@pytest.fixture(autouse=True)
def isolate_test_endpoints(monkeypatch):
    """Keep developer environment overrides out of local contract tests."""
    for name in ("OPENMETADATA_URL", "OPENMETADATA_TOKEN", "OPENMETADATA_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    connect = socket.socket.connect

    def local_connect(instance, address):
        if isinstance(address, tuple):
            try:
                local = ipaddress.ip_address(address[0]).is_loopback
            except ValueError:
                local = address[0] == "localhost"
            if not local:
                raise AssertionError("Tests may contact only loopback services")
        return connect(instance, address)

    monkeypatch.setattr(socket.socket, "connect", local_connect)
