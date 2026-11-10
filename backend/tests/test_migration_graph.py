from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def test_migration_graph_has_one_linear_head() -> None:
    backend_root = Path(__file__).resolve().parents[1]
    config = Config()
    config.set_main_option("script_location", str(backend_root / "migrations"))
    scripts = ScriptDirectory.from_config(config)

    assert scripts.get_heads() == ["0014_turn_retry_diagnostics"]
    assert scripts.get_revision("0014_turn_retry_diagnostics").down_revision == "0013_turn_repair_budget"
    assert scripts.get_revision("0013_turn_repair_budget").down_revision == "0012_initialization_provenance"
    assert scripts.get_revision("0012_initialization_provenance").down_revision == "0011_session_initialization"
    assert scripts.get_revision("0011_session_initialization").down_revision == "0010_manifest_guard"
    assert scripts.get_revision("0010_manifest_guard").down_revision == "0009_content_item_hash_guard"
    assert scripts.get_revision("0009_content_item_hash_guard").down_revision == "0008_scenario_content_guards"
    assert scripts.get_revision("0008_scenario_content_guards").down_revision == "0007_scenario_content_items"
    assert scripts.get_revision("0007_scenario_content_items").down_revision == "0006_history_authentic"
    assert scripts.get_revision("0006_history_authentic").down_revision == "0005_historical_rag"
    assert scripts.get_revision("0005_historical_rag").down_revision == "0004_multiple_events_per_turn"


def test_history_models_register_on_shared_alembic_metadata() -> None:
    import app.history.models  # noqa: F401
    from app.db.models import Base

    assert {
        "history_authentic.corpus_releases",
        "history_authentic.ingest_runs",
        "history_authentic.source_editions",
        "history_authentic.documents",
        "history_authentic.passages",
    } <= set(Base.metadata.tables)
