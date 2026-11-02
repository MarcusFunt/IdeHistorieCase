"""Bounded manifest validation; catalog links without passages are never evidence."""
import hashlib

import pytest

from app.rag.corpus import MAX_CORPUS_BYTES, load_corpus


def corpus_record(text="Exact reviewed source line.", **overrides):
    passage = {
        "key": "sample-passage",
        "source_identifier": "R04",
        "source_title": "Official historical record",
        "source_url": "https://history.state.gov/historicaldocuments/sample",
        "source_anchor": "Document 4, page 1",
        "passage_text": text,
        "passage_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "document_date": "1918-01-08",
        "in_world_available_from": "1918-01-08",
        "postgame_only": False,
        "actor_ids": ["britain", "china"],
        "visibility_scope": "actor_limited",
        "rights_basis": "FRUS public-domain statement; local review remains pending.",
    }
    passage.update(overrides)
    return {
        "schema_version": 1,
        "scenario_slug": "versailles-1919",
        "scenario_version": "2.5",
        "phase_world_dates": {"opening_context": "1919-03-20"},
        "passages": [passage],
    }


def write_corpus(tmp_path, record):
    path = tmp_path / "corpus.yaml"
    import yaml

    path.write_text(yaml.safe_dump(record, allow_unicode=True), encoding="utf-8")
    return path


def test_load_corpus_checks_exact_passage_hash_and_returns_manifest_hash(tmp_path):
    path = write_corpus(tmp_path, corpus_record())

    loaded = load_corpus(path)

    assert loaded.corpus.scenario_slug == "versailles-1919"
    assert loaded.corpus.passages[0].passage_text == "Exact reviewed source line."
    assert loaded.corpus.passages[0].review_status == "pending"
    assert loaded.corpus.passages[0].rights_status == "pending"
    assert loaded.corpus_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


def test_corpus_manifest_cannot_preapprove_review_or_rights_status(tmp_path):
    record = corpus_record(rights_status="approved")

    with pytest.raises(ValueError, match="rights_status"):
        load_corpus(write_corpus(tmp_path, record))


def test_catalog_url_without_exact_text_and_anchor_is_rejected(tmp_path):
    record = corpus_record()
    del record["passages"][0]["passage_text"]
    del record["passages"][0]["passage_sha256"]
    del record["passages"][0]["source_anchor"]

    with pytest.raises(ValueError, match="passage_text|passage_sha256|source_anchor"):
        load_corpus(write_corpus(tmp_path, record))


def test_load_corpus_rejects_text_hash_mismatch(tmp_path):
    record = corpus_record(passage_sha256="0" * 64)

    with pytest.raises(ValueError, match="SHA-256"):
        load_corpus(write_corpus(tmp_path, record))


def test_load_corpus_rejects_duplicate_passage_keys(tmp_path):
    record = corpus_record()
    duplicate = dict(
        record["passages"][0],
        source_anchor="Document 5, page 2",
        actor_ids=["britain", "china"],
    )
    record["passages"].append(duplicate)

    with pytest.raises(ValueError, match="unique"):
        load_corpus(write_corpus(tmp_path, record))


def test_postgame_passages_can_have_no_in_world_actor_audience(tmp_path):
    record = corpus_record(
        actor_ids=[],
        document_date="1921-05-05",
        in_world_available_from=None,
        postgame_only=True,
        visibility_scope="postgame",
    )

    loaded = load_corpus(write_corpus(tmp_path, record))

    assert loaded.corpus.passages[0].postgame_only is True
    assert loaded.corpus.passages[0].actor_ids == []


def test_load_corpus_rejects_oversized_input_before_yaml_parsing(tmp_path):
    path = tmp_path / "oversized.yaml"
    path.write_bytes(b"x" * (MAX_CORPUS_BYTES + 1))

    with pytest.raises(ValueError, match="maximum size"):
        load_corpus(path)


def test_rag_cli_can_validate_a_corpus_without_database_or_embedding_service(tmp_path, capsys):
    from app.rag.cli import main

    result = main(["validate", str(write_corpus(tmp_path, corpus_record()))])

    output = capsys.readouterr().out
    assert result == 0
    assert "versailles-1919@2.5" in output
    assert "passages=1" in output
    assert "sha256:" in output
