"""Read-only verification of scenario bundle checksum pins."""
from pathlib import Path

from test_scenario_schema import MINIMAL_SCENARIO

from app.scenarios.bundle import scenario_bundle_hash
from app.scenarios.cli import main

BEHAVIOR_SIDECAR = b"""policy_version: 1
scenario_slug: test-scenario
scenario_version: \"1\"
reaction_policy: index_pairing_is_illustrative_not_historical
rules: []
"""


def create_pinned_bundle(root: Path) -> tuple[Path, Path, Path, Path]:
    scenario = root / "scenario"
    sources = scenario / "sources"
    sources.mkdir(parents=True)
    manifest = scenario / "1.yaml"
    sidecar = scenario / "1_behaviors.yaml"
    source = sources / "catalog.yaml"
    pin = scenario / "1_bundle.sha256"
    manifest.write_bytes(MINIMAL_SCENARIO.encode("utf-8"))
    sidecar.write_bytes(BEHAVIOR_SIDECAR)
    source.write_bytes(b"sources: []\n")
    digest = scenario_bundle_hash(manifest, manifest.read_bytes())
    assert digest is not None
    pin.write_text(digest, encoding="ascii")
    return manifest, sidecar, source, pin


def verify(root: Path) -> int:
    try:
        return main(["verify-bundle-pins", str(root)])
    except SystemExit as exc:
        return int(exc.code)


def test_verifier_accepts_a_matching_bundle_pin(tmp_path, capsys):
    create_pinned_bundle(tmp_path)

    assert verify(tmp_path) == 0

    assert "verified 1 bundle pin(s)" in capsys.readouterr().out


def test_verifier_rejects_an_edited_sidecar(tmp_path, capsys):
    _, sidecar, _, _ = create_pinned_bundle(tmp_path)
    pin = tmp_path / "scenario" / "1_bundle.sha256"
    original_pin = pin.read_text(encoding="ascii")
    sidecar.write_bytes(BEHAVIOR_SIDECAR + b"# edited\n")

    assert verify(tmp_path) != 0

    assert "bundle hash mismatch" in capsys.readouterr().err
    assert pin.read_text(encoding="ascii") == original_pin


def test_verifier_rejects_an_edited_manifest(tmp_path, capsys):
    manifest, _, _, _ = create_pinned_bundle(tmp_path)
    manifest.write_bytes(manifest.read_bytes() + b"# byte-level edit\n")

    assert verify(tmp_path) != 0

    assert "bundle hash mismatch" in capsys.readouterr().err


def test_verifier_rejects_an_edited_source(tmp_path, capsys):
    _, _, source, _ = create_pinned_bundle(tmp_path)
    source.write_bytes(b"sources: [R01]\n")

    assert verify(tmp_path) != 0

    assert "bundle hash mismatch" in capsys.readouterr().err


def test_verifier_rejects_a_line_ending_change(tmp_path, capsys):
    _, _, source, _ = create_pinned_bundle(tmp_path)
    source.write_bytes(source.read_bytes().replace(b"\n", b"\r\n"))

    assert verify(tmp_path) != 0

    assert "bundle hash mismatch" in capsys.readouterr().err


def test_verifier_requires_a_pin_for_a_scenario_with_sidecars(tmp_path, capsys):
    _, _, _, pin = create_pinned_bundle(tmp_path)
    pin.unlink()

    assert verify(tmp_path) != 0

    assert "missing required bundle pin" in capsys.readouterr().err


def test_verifier_rejects_malformed_pin(tmp_path, capsys):
    _, _, _, pin = create_pinned_bundle(tmp_path)
    pin.write_text("not-a-sha256\n", encoding="ascii")

    assert verify(tmp_path) != 0

    assert "malformed SHA-256 bundle pin" in capsys.readouterr().err


def test_verifier_rejects_a_pin_without_a_matching_bundle(tmp_path, capsys):
    (tmp_path / "standalone.yaml").write_text(MINIMAL_SCENARIO, encoding="utf-8")
    (tmp_path / "orphan_bundle.sha256").write_text("0" * 64, encoding="ascii")

    assert verify(tmp_path) != 0

    assert "no matching scenario bundle" in capsys.readouterr().err


def test_verifier_reports_pin_read_errors(tmp_path, monkeypatch, capsys):
    _, _, _, pin = create_pinned_bundle(tmp_path)
    original_read_text = Path.read_text

    def fail_pin_read(path, *args, **kwargs):
        if path == pin:
            raise OSError("simulated read failure")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_pin_read)

    assert verify(tmp_path) != 0

    assert "cannot read bundle pin" in capsys.readouterr().err
