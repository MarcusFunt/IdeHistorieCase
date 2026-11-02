"""Developer-only validation, staging and review commands for historical passages."""
import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.core.settings import get_settings
from app.db.connection import session_factory
from app.db.models import HistoricalPassage, HistoricalPassageReview
from app.rag.clients import ModelServiceError
from app.rag.corpus import load_corpus
from app.rag.evaluation import load_evaluation_set
from app.rag.repository import ingest_corpus, record_review


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.rag.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="validate one bounded corpus without a database")
    validate.add_argument("path", type=Path)
    validate_eval = commands.add_parser(
        "validate-eval", help="validate a bounded labeled evaluation manifest without a database"
    )
    validate_eval.add_argument("path", type=Path)
    ingest = commands.add_parser("ingest", help="stage passages and embeddings without approving them")
    ingest.add_argument("path", type=Path)
    review = commands.add_parser("review", help="append a review and rights decision")
    review.add_argument("slug")
    review.add_argument("version")
    review.add_argument("passage_key")
    review.add_argument("--reviewer", required=True)
    review.add_argument("--decision", choices=("approved", "rejected", "revoked"), required=True)
    review.add_argument(
        "--rights-status", choices=("approved", "pending", "denied", "restricted"),
        default="pending",
    )
    review.add_argument("--notes", default=None)
    listing = commands.add_parser("list", help="show passage and latest review statuses")
    listing.add_argument("slug")
    listing.add_argument("version")
    return parser


async def _ingest(path: Path) -> int:
    # Import lazily so offline corpus validation has no model-service dependency.
    from app.rag.clients import EmbeddingServiceClient

    loaded = load_corpus(path)
    settings = get_settings()
    async with httpx.AsyncClient(timeout=settings.rag_embed_timeout_seconds, trust_env=False) as client:
        embedder = EmbeddingServiceClient(
            settings.rag_embed_base_url,
            settings.rag_embed_model_id,
            client,
            timeout_seconds=settings.rag_embed_timeout_seconds,
        )
        vectors = await embedder.embed_documents([item.passage_text for item in loaded.corpus.passages])
    async with session_factory() as db, db.begin():
        summary = await ingest_corpus(db, loaded, vectors, model_id=embedder.model_id)
    print(
        f"{summary.status}: {loaded.corpus.scenario_slug}@{loaded.corpus.scenario_version} "
        f"passages={summary.passages} embeddings={summary.embeddings}; approval remains pending"
    )
    return 0


async def _review(args) -> int:
    async with session_factory() as db, db.begin():
        passage = (
            await db.execute(
                select(HistoricalPassage).where(
                    HistoricalPassage.scenario_slug == args.slug,
                    HistoricalPassage.scenario_version == args.version,
                    HistoricalPassage.passage_key == args.passage_key,
                )
            )
        ).scalar_one_or_none()
        if passage is None:
            raise ValueError(
                f"passage not found: {args.slug}@{args.version}/{args.passage_key}"
            )
        result = await record_review(
            db,
            passage.id,
            reviewer=args.reviewer,
            decision=args.decision,
            rights_status=args.rights_status,
            notes=args.notes,
        )
    print(
        f"review revision {result.revision}: {args.passage_key} "
        f"review={result.review_status} rights={result.rights_status}"
    )
    return 0


async def _list(slug: str, version: str) -> int:
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(HistoricalPassage).where(
                    HistoricalPassage.scenario_slug == slug,
                    HistoricalPassage.scenario_version == version,
                ).order_by(HistoricalPassage.passage_key)
            )
        ).scalars().all()
        result = []
        for passage in rows:
            review = (
                await db.execute(
                    select(HistoricalPassageReview)
                    .where(HistoricalPassageReview.passage_id == passage.id)
                    .order_by(HistoricalPassageReview.revision.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            result.append({
                "key": passage.passage_key,
                "source_identifier": passage.source_identifier,
                "passage_sha256": passage.passage_sha256,
                "review_status": review.review_status if review else "pending",
                "rights_status": review.rights_status if review else "pending",
                "review_revision": review.revision if review else None,
            })
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


async def _run(args) -> int:
    if args.command == "ingest":
        return await _ingest(args.path)
    if args.command == "review":
        return await _review(args)
    return await _list(args.slug, args.version)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate":
            loaded = load_corpus(args.path)
            print(
                f"valid: {loaded.corpus.scenario_slug}@{loaded.corpus.scenario_version} "
                f"passages={len(loaded.corpus.passages)} sha256:{loaded.corpus_sha256}"
            )
            return 0
        if args.command == "validate-eval":
            evaluation = load_evaluation_set(args.path)
            print(
                f"valid evaluation set: {evaluation.scenario_slug}@{evaluation.scenario_version} "
                f"cases={len(evaluation.cases)}"
            )
            return 0
        return asyncio.run(_run(args))
    except (ValueError, OSError, httpx.HTTPError, ModelServiceError) as exc:
        print(f"RAG command failed: {exc}", file=sys.stderr)
        return 2
    except SQLAlchemyError as exc:
        print(f"RAG database command failed ({type(exc).__name__})", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
