"""V1/v2 loader dispatch, bounded bundle files, and canonical identities."""
import shutil
from pathlib import Path

import pytest
import yaml
from test_scenario_schema import MINIMAL_SCENARIO

FIXTURE_DIR = Path(__file__).parent / "fixtures/scenarios/industrial-inquiry"


def copy_bundle(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    destination = tmp_path / "industrial-inquiry"
    shutil.copytree(FIXTURE_DIR, destination)
    return destination / "1.0.yaml"


def test_v1_and_v2_manifests_dispatch_to_their_own_validators(tmp_path: Path):
    from app.scenarios.loader import load_scenario
    from app.scenarios.schema import ScenarioDefinition

    v1_path = tmp_path / "v1.yaml"
    v1_path.write_text(MINIMAL_SCENARIO, encoding="utf-8")
    v1 = load_scenario(v1_path)
    v2 = load_scenario(copy_bundle(tmp_path))

    assert isinstance(v1.definition, ScenarioDefinition)
    assert v1.initial_state["phase"] == "opening"
    assert v2.definition.schema_version == 2
    assert v2.initial_state == {}
    assert v2.content_items["site_briefing"].startswith("Dette er et forfatterudarbejdet")
    assert len(v2.content_hash) == 64
    assert len(v2.bundle_hash) == 64


def test_v2_content_hash_is_canonical_but_bundle_hash_tracks_original_bytes(tmp_path: Path):
    from app.scenarios.loader import load_scenario

    first_path = copy_bundle(tmp_path / "first")
    first = load_scenario(first_path)

    second_path = copy_bundle(tmp_path / "second")
    parsed = yaml.safe_load(second_path.read_text(encoding="utf-8"))
    second_path.write_text(yaml.safe_dump(parsed, sort_keys=True, allow_unicode=True), encoding="utf-8")
    second = load_scenario(second_path)

    assert first.content_hash == second.content_hash
    assert first.bundle_hash != second.bundle_hash


def test_v2_bundle_hash_includes_referenced_content_bytes(tmp_path: Path):
    from app.scenarios.loader import load_scenario

    path = copy_bundle(tmp_path)
    first = load_scenario(path)
    note = path.parent / "notes/site_briefing.md"
    note.write_text(note.read_text(encoding="utf-8") + " En ekstra redaktionel sætning.", encoding="utf-8")
    changed = load_scenario(path)

    assert first.content_hash == changed.content_hash
    assert first.bundle_hash != changed.bundle_hash


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ("schema_version: 2", "duplicate key"),
        ("schema_version: true", "schema_version"),
        ("schema_version: 3", "schema_version"),
        ("file: ../outside.md", "path"),
        ("file: notes/missing.md", "cannot read content file"),
    ],
)
def test_malformed_v2_manifests_fail_during_load(tmp_path: Path, replacement: str, message: str):
    from app.scenarios.loader import load_scenario

    path = copy_bundle(tmp_path)
    text = path.read_text(encoding="utf-8")
    if replacement == "schema_version: 2":
        text = text.replace("schema_version: 2", "schema_version: 2\nschema_version: 2")
    elif replacement.startswith("schema_version:"):
        text = text.replace("schema_version: 2", replacement)
    else:
        text = text.replace("file: notes/site_briefing.md", replacement)
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_scenario(path)


def test_v2_content_file_size_is_bounded(tmp_path: Path):
    from app.scenarios.loader import MAX_CONTENT_ITEM_BYTES, load_scenario

    path = copy_bundle(tmp_path)
    note = path.parent / "notes/site_briefing.md"
    note.write_bytes(b"x" * (MAX_CONTENT_ITEM_BYTES + 1))

    with pytest.raises(ValueError, match="content file exceeds maximum size"):
        load_scenario(path)


def test_v2_validation_cli_works_and_import_is_rejected_before_database_use(tmp_path: Path, monkeypatch, capsys):
    from app.scenarios import cli

    path = copy_bundle(tmp_path)
    assert cli.main(["validate", str(path)]) == 0
    assert "schema 2" in capsys.readouterr().out

    def no_database_access():
        raise AssertionError("v2 import reached the database before Goal 3")

    monkeypatch.setattr(cli, "session_factory", no_database_access)
    assert cli.main(["import", str(path)]) == 2
    assert "v2 SQL publication is not implemented" in capsys.readouterr().err


def test_v2_repository_import_is_rejected_before_database_use(tmp_path: Path):
    import asyncio

    from app.scenarios.loader import load_scenario
    from app.scenarios.repository import import_scenario

    class NoDatabase:
        async def execute(self, *_args, **_kwargs):
            raise AssertionError("v2 import reached the database before Goal 3")

    loaded = load_scenario(copy_bundle(tmp_path))
    with pytest.raises(ValueError, match="v2 SQL publication is not implemented"):
        asyncio.run(import_scenario(NoDatabase(), loaded))
