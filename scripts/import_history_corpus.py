"""Explicit offline importer for the pinned Royal Danish Library release."""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from app.core.settings import get_settings
from app.history.importer import import_release
from sqlalchemy.ext.asyncio import create_async_engine

ARCHIVE_NAME = "public_domain_Danmarks_Breve.zip"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus-dir",
        type=Path,
        default=os.getenv("HISTORY_CORPUS_DIR"),
        help=(
            "read-only extracted bundle root with latest/authentic_corpus_v3_1 and "
            "source_archive/public_domain_Danmarks_Breve.zip (defaults to HISTORY_CORPUS_DIR)"
        ),
    )
    parser.add_argument("--release-dir", type=Path, help="override the extracted release directory")
    parser.add_argument("--source-archive", type=Path, help="override the original source ZIP path")
    args = parser.parse_args()
    if args.corpus_dir is None and (args.release_dir is None or args.source_archive is None):
        parser.error("set HISTORY_CORPUS_DIR/--corpus-dir, or provide both --release-dir and --source-archive")
    if (args.release_dir is None) != (args.source_archive is None):
        parser.error("--release-dir and --source-archive must be provided together")
    if args.release_dir is None:
        args.release_dir = args.corpus_dir / "latest" / "authentic_corpus_v3_1"
        args.source_archive = args.corpus_dir / "source_archive" / ARCHIVE_NAME
    return args


async def main() -> int:
    args = parse_args()
    engine = create_async_engine(get_settings().database_url)
    try:
        result = await import_release(engine, args.release_dir, args.source_archive)
    finally:
        await engine.dispose()
    print(
        f"{result.status}: release={result.release_sha256} documents={result.document_count} "
        f"passages={result.passage_count} metadata_anomalies={result.metadata_anomaly_count} "
        f"ingest_run={result.ingest_run_id}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
