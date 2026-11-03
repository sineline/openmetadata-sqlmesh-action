import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("ruamel.yaml")
pytest.importorskip("sqlmesh")


def test_composite_action_runs_the_same_cli_without_shell_interpolation(tmp_path):
    root = Path(__file__).parents[1]
    action = yaml.YAML(typ="safe").load((root / "action.yml").read_text())
    step = action["runs"]["steps"][-1]
    assert step["shell"] == "bash"
    assert "${{" not in step["run"]
    # Provide the ordinary console-script entry point in the test interpreter.
    command = tmp_path / "openmetadata-sqlmesh"
    command.write_text(
        f"#!{sys.executable}\nfrom openmetadata_sqlmesh.cli import main\nraise SystemExit(main())\n"
    )
    command.chmod(0o755)
    output = tmp_path / "github-output"
    env = dict(
        os.environ,
        PATH=str(tmp_path)
        + os.pathsep
        + str(Path(sys.executable).parent)
        + os.pathsep
        + os.environ.get("PATH", ""),
        PYTHONPATH=str(root / "src"),
        OM_PROJECT=str(root / "examples/sqlmesh"),
        OM_URL="https://om.invalid",
        OM_DATABASE_SERVICE="warehouse",
        OM_PIPELINE_SERVICE="sqlmesh",
        OM_DEFAULT_DATABASE="",
        OM_DEFAULT_SCHEMA="",
        OM_CATALOG_SERVICES="{}",
        OM_GATEWAY="",
        OM_DRY_RUN="true",
        OPENMETADATA_TOKEN="test-secret-do-not-log",
        RUNNER_TEMP=str(tmp_path),
        GITHUB_OUTPUT=str(output),
    )
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "test-secret-do-not-log" not in result.stdout + result.stderr
    report_path = Path(output.read_text().strip().split("=", 1)[1])
    assert len(json.loads(report_path.read_text())["models"]) == 3
    assert not list(tmp_path.glob("*.config.json"))
