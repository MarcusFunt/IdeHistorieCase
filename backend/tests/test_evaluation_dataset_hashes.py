from __future__ import annotations

from pathlib import Path

from app.evaluation.dataset import sha256_file


def test_sha256_file_is_stable_across_checkout_newlines(tmp_path: Path) -> None:
    dataset_file = tmp_path / "queries.jsonl"
    dataset_file.write_bytes(b'{"query_id":"q1"}\n{"query_id":"q2"}\n')
    lf_digest = sha256_file(dataset_file)

    dataset_file.write_bytes(b'{"query_id":"q1"}\r\n{"query_id":"q2"}\r\n')

    assert sha256_file(dataset_file) == lf_digest
