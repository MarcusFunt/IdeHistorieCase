"""Developer CLI contracts that do not need PostgreSQL."""
import importlib.util

from test_scenario_schema import MINIMAL_SCENARIO


def cli_main():
    try:
        spec = importlib.util.find_spec("app.scenarios.cli")
    except ModuleNotFoundError:
        spec = None
    assert spec is not None, "scenario developer CLI is not implemented"
    from app.scenarios.cli import main

    return main


def test_validate_command_reports_the_immutable_identity_and_hash(tmp_path, capsys):
    manifest = tmp_path / "scenario.yaml"
    manifest.write_text(MINIMAL_SCENARIO, encoding="utf-8")

    assert cli_main()(["validate", str(manifest)]) == 0

    output = capsys.readouterr().out
    assert "test-scenario@1" in output
    assert "sha256:" in output


def test_invalid_manifest_has_nonzero_exit_and_actionable_error(tmp_path, capsys):
    manifest = tmp_path / "bad.yaml"
    manifest.write_text(MINIMAL_SCENARIO.replace("schema_version: 1", "schema_version: 9"), encoding="utf-8")

    assert cli_main()(["validate", str(manifest)]) != 0

    assert "schema_version" in capsys.readouterr().err


def test_validate_all_checks_authored_manifests(tmp_path, capsys):
    (tmp_path / "one.yaml").write_text(MINIMAL_SCENARIO, encoding="utf-8")
    (tmp_path / "two.yaml").write_text(
        MINIMAL_SCENARIO.replace("test-scenario", "second-scenario"),
        encoding="utf-8",
    )

    assert cli_main()(["validate-all", str(tmp_path)]) == 0

    assert "validated 2 manifest(s)" in capsys.readouterr().out
