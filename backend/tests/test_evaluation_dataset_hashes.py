from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from app.evaluation.dataset import sha256_file, validate_dataset
from app.evaluation.runner import run_evaluation

DATASET_ROOT = Path(__file__).parents[1] / "evaluation_data" / "versailles-1919"


def _copy_dataset(root: Path) -> Path:
    target = root / "dataset"
    shutil.copytree(DATASET_ROOT, target)
    return target


def _edit_manifest(root: Path, edit) -> None:
    path = root / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    edit(manifest)
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def test_sha256_file_is_stable_across_checkout_newlines(tmp_path: Path) -> None:
    dataset_file = tmp_path / "queries.jsonl"
    dataset_file.write_bytes(b'{"query_id":"q1"}\n{"query_id":"q2"}\n')
    lf_digest = sha256_file(dataset_file)

    dataset_file.write_bytes(b'{"query_id":"q1"}\r\n{"query_id":"q2"}\r\n')

    assert sha256_file(dataset_file) == lf_digest


def test_committed_manifest_is_valid_while_dataset_remains_draft() -> None:
    summary = validate_dataset(DATASET_ROOT)

    assert summary.errors == []
    assert any("status is draft" in warning for warning in summary.warnings)
    assert any("heldout target incomplete: 0/100" in warning for warning in summary.warnings)


@pytest.mark.parametrize(
    "filename",
    [
        "sources.jsonl",
        "passages.jsonl",
        "pilot_queries.jsonl",
        "heldout_queries.jsonl",
        "judgments.jsonl",
    ],
)
def test_missing_required_manifest_hash_fails_validation(
    tmp_path: Path, filename: str
) -> None:
    dataset_root = _copy_dataset(tmp_path)
    _edit_manifest(dataset_root, lambda manifest: manifest["file_hashes"].pop(filename))

    summary = validate_dataset(dataset_root)

    assert any("Manifest is missing hashes" in error and filename in error for error in summary.errors)


@pytest.mark.parametrize("split", ["pilot", "heldout"])
def test_evaluation_startup_refuses_missing_required_hash(
    tmp_path: Path, split: str
) -> None:
    dataset_root = _copy_dataset(tmp_path)
    _edit_manifest(
        dataset_root,
        lambda manifest: manifest["file_hashes"].pop("passages.jsonl"),
    )
    output_dir = tmp_path / "evaluation-output"

    with pytest.raises(ValueError, match="Dataset validation failed before retrieval"):
        run_evaluation(
            dataset_root=dataset_root,
            split=split,
            output_dir=output_dir,
            profiles=["bm25"],
            embed_url="http://embed.invalid",
            rerank_url="http://rerank.invalid",
            execution_location="host",
            cache_state="cold",
            warmup_count=0,
        )

    assert not output_dir.exists()


def test_missing_empty_heldout_hash_fails_validation(tmp_path: Path) -> None:
    dataset_root = _copy_dataset(tmp_path)
    assert (dataset_root / "heldout_queries.jsonl").read_text(encoding="utf-8") == ""
    _edit_manifest(
        dataset_root,
        lambda manifest: manifest["file_hashes"].pop("heldout_queries.jsonl"),
    )

    summary = validate_dataset(dataset_root)

    assert any(
        "Manifest is missing hashes" in error and "heldout_queries.jsonl" in error
        for error in summary.errors
    )


def test_incorrect_manifest_hash_remains_an_error(tmp_path: Path) -> None:
    dataset_root = _copy_dataset(tmp_path)
    _edit_manifest(
        dataset_root,
        lambda manifest: manifest["file_hashes"].__setitem__("passages.jsonl", "0" * 64),
    )

    summary = validate_dataset(dataset_root)

    assert "Manifest file hash mismatch: passages.jsonl" in summary.errors


def test_unsupported_manifest_hash_path_remains_an_error(tmp_path: Path) -> None:
    dataset_root = _copy_dataset(tmp_path)
    _edit_manifest(
        dataset_root,
        lambda manifest: manifest["file_hashes"].__setitem__("../secrets.json", "0" * 64),
    )

    summary = validate_dataset(dataset_root)

    assert "Manifest contains an unsupported file hash path: ../secrets.json" in summary.errors
