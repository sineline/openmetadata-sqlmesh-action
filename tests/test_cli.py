import json
from pathlib import Path

import pytest

from openmetadata_sqlmesh.cli import main

ROOT = Path(__file__).parents[1]
CONFIG = ROOT / "examples" / "openmetadata-sqlmesh.json"


def test_help_and_version(capsys):
    with pytest.raises(SystemExit) as result:
        main(["--help"])
    assert result.value.code == 0
    assert "audit-results" in capsys.readouterr().out
    with pytest.raises(SystemExit) as result:
        main(["--version"])
    assert result.value.code == 0
    assert "0.1.0" in capsys.readouterr().out


def test_real_sync_dry_run_is_json_and_does_not_call_openmetadata(capsys, monkeypatch):
    pytest.importorskip("sqlmesh")
    from openmetadata_sqlmesh.client import OpenMetadataClient

    def no_requests(*args, **kwargs):
        pytest.fail("Dry run contacted OpenMetadata")

    monkeypatch.setattr(OpenMetadataClient, "request", no_requests)
    assert (
        main(
            [
                "sync",
                "--config",
                str(CONFIG),
                "--project",
                str(ROOT / "examples/sqlmesh"),
                "--dry-run",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert len(result["models"]) == 3
    assert result["models"][0]["column_lineage"]


def test_audit_json_dry_run(tmp_path, capsys):
    records = tmp_path / "results.json"
    records.write_text(
        json.dumps(
            [
                {
                    "model": "db.analytics.orders",
                    "audit": "not_null",
                    "timestamp": 1793657000000,
                    "count": 0,
                }
            ]
        )
    )
    assert (
        main(["audit-results", str(records), "--config", str(CONFIG), "--dry-run"]) == 0
    )
    assert (
        json.loads(capsys.readouterr().out)["audit_results"][0]["testCaseStatus"]
        == "Success"
    )


def test_config_errors_exit_nonzero_without_leaking_file_contents(tmp_path, capsys):
    invalid = tmp_path / "config.json"
    invalid.write_text(
        '{"url":"https://om.example","database_service":"warehouse","token":"private-value"}'
    )
    assert main(["sync", "--config", str(invalid)]) == 2
    output = capsys.readouterr()
    assert not output.out
    assert "private-value" not in output.err
    assert "token_env" in output.err


def test_publish_errors_exit_nonzero(monkeypatch, capsys):
    import openmetadata_sqlmesh.cli as cli
    from openmetadata_sqlmesh.models import ProjectMetadata
    from openmetadata_sqlmesh.sync import SyncReport

    monkeypatch.setattr(
        cli, "load_project", lambda *args, **kwargs: ProjectMetadata(())
    )
    monkeypatch.setattr(
        cli.Synchronizer, "sync", lambda *args: SyncReport(errors=["HTTP 401"])
    )
    assert main(["sync", "--config", str(CONFIG)]) == 1
    assert json.loads(capsys.readouterr().out)["errors"] == ["HTTP 401"]


def test_malformed_audit_input_and_output_file_errors(tmp_path, capsys):
    records = tmp_path / "results.json"
    records.write_text('{"model": "wrong-shape"}')
    assert (
        main(["audit-results", str(records), "--config", str(CONFIG), "--dry-run"]) == 2
    )
    assert "JSON array" in capsys.readouterr().err
