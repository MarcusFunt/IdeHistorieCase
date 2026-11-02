from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .answer_study import prepare_answer_study
from .dataset import validate_dataset
from .pooling import export_unjudged_pool, import_pool_judgments
from .ratings import import_answer_ratings
from .report import read_traces, write_report
from .retrieval import PROFILES
from .runner import _dataset_hashes, run_evaluation

DEFAULT_DATA = Path(__file__).resolve().parents[2] / "evaluation_data" / "versailles-1919"


def _json(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="historical-eval",
        description="Evaluate historical evidence retrieval without calling the game worker.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="Validate the dataset files and references.")
    validate.add_argument("--dataset", type=Path, default=DEFAULT_DATA)

    retrieve = sub.add_parser("retrieve", help="Run retrieval-only profiles and save traces.")
    retrieve.add_argument("--dataset", type=Path, default=DEFAULT_DATA)
    retrieve.add_argument("--split", choices=("pilot", "heldout"), required=True)
    retrieve.add_argument("--profiles", default=",".join(PROFILES))
    retrieve.add_argument("--output", type=Path, required=True)
    retrieve.add_argument("--execution-location", choices=("local", "remote"), default="local")
    retrieve.add_argument("--cache-state", choices=("cold", "warm"), default="warm")
    retrieve.add_argument("--warmup-count", type=int, default=2)
    retrieve.add_argument(
        "--embed-url", default=os.getenv("EVAL_EMBED_URL", "http://ddu-embed:8080")
    )
    retrieve.add_argument(
        "--rerank-url", default=os.getenv("EVAL_RERANK_URL", "http://ddu-rerank:8080")
    )

    pool = sub.add_parser("pool", help="Export unjudged top results for blind relevance review.")
    pool.add_argument("--dataset", type=Path, default=DEFAULT_DATA)
    pool.add_argument("--traces", type=Path, required=True)
    pool.add_argument("--output", type=Path, required=True)

    import_labels = sub.add_parser("import-judgments", help="Import blind pooled labels.")
    import_labels.add_argument("--dataset", type=Path, default=DEFAULT_DATA)
    import_labels.add_argument("--ratings", type=Path, required=True)
    import_labels.add_argument("--private-key", type=Path, required=True)
    import_labels.add_argument("--pool-manifest", type=Path, required=True)
    import_labels.add_argument("--reviewer-id", required=True)
    import_labels.add_argument(
        "--review-method",
        choices=("human", "agent"),
        default="human",
        help="Record whether the blind pool was labeled by a person or an AI review agent.",
    )
    import_labels.add_argument(
        "--review-note",
        help="Optional audit note stored with each query judgment completed by this import.",
    )

    report = sub.add_parser("report", help="Summarize existing retrieval traces.")
    report.add_argument("--dataset", type=Path, default=DEFAULT_DATA)
    report.add_argument("--traces", type=Path, required=True)
    report.add_argument("--output", type=Path, required=True)
    report.add_argument("--resources", type=Path)

    answer_study = sub.add_parser(
        "answer-study",
        help="Generate blind answer packets from a reviewed held-out sample.",
    )
    answer_study.add_argument("--dataset", type=Path, default=DEFAULT_DATA)
    answer_study.add_argument("--traces", type=Path, required=True)
    answer_study.add_argument("--output", type=Path, required=True)
    answer_study.add_argument(
        "--generator-url", default=os.getenv("EVAL_GENERATOR_URL", "http://ddu-llm:8080")
    )
    answer_study.add_argument(
        "--generator-model", default=os.getenv("EVAL_GENERATOR_MODEL", "ddu-narrator")
    )
    answer_study.add_argument("--sample-size", type=int, default=25)
    answer_study.add_argument("--seed", type=int, default=198)
    answer_study.add_argument("--evidence-token-budget", type=int, default=1500)
    answer_study.add_argument("--max-passages", type=int, default=5)

    answer_ratings = sub.add_parser(
        "import-answer-ratings",
        help="Validate blind human ratings and unblind aggregate answer quality.",
    )
    answer_ratings.add_argument("--ratings", type=Path, required=True)
    answer_ratings.add_argument("--packets", type=Path, required=True)
    answer_ratings.add_argument("--private-key", type=Path, required=True)
    answer_ratings.add_argument("--output", type=Path, required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        if args.command == "validate":
            summary = validate_dataset(args.dataset)
            _json(summary.model_dump())
            if summary.errors:
                raise SystemExit(2)
            return
        if args.command == "retrieve":
            profiles = [item.strip() for item in args.profiles.split(",") if item.strip()]
            unknown = sorted(set(profiles) - set(PROFILES))
            if unknown:
                raise ValueError(f"Unknown retrieval profiles: {', '.join(unknown)}")
            if args.warmup_count < 0:
                raise ValueError("warmup-count cannot be negative")
            summary = run_evaluation(
                dataset_root=args.dataset,
                split=args.split,
                output_dir=args.output,
                profiles=profiles,  # type: ignore[arg-type]
                embed_url=args.embed_url,
                rerank_url=args.rerank_url,
                execution_location=args.execution_location,
                cache_state=args.cache_state,
                warmup_count=args.warmup_count if args.cache_state == "warm" else 0,
            )
            _json(summary)
            return
        if args.command == "pool":
            _json(
                export_unjudged_pool(
                    dataset_root=args.dataset,
                    traces_path=args.traces,
                    output_dir=args.output,
                )
            )
            return
        if args.command == "import-judgments":
            _json(
                import_pool_judgments(
                    dataset_root=args.dataset,
                    ratings_path=args.ratings,
                    private_key_path=args.private_key,
                    pool_manifest_path=args.pool_manifest,
                    reviewer_id=args.reviewer_id,
                    review_method=args.review_method,
                    review_note=args.review_note,
                )
            )
            return
        if args.command == "report":
            from .dataset import load_dataset

            traces = read_traces(args.traces)
            if not traces:
                raise ValueError("Trace file contains no query records")
            splits = {trace.split for trace in traces}
            if len(splits) != 1:
                raise ValueError("A report trace file must contain exactly one split")
            split = next(iter(splits))
            dataset = load_dataset(args.dataset, split)
            validation = validate_dataset(args.dataset)
            if validation.errors:
                raise ValueError("Dataset validation failed: " + "; ".join(validation.errors))
            expected_query_ids = {query.query_id for query in dataset.queries}
            if any(trace.query_id not in expected_query_ids for trace in traces):
                raise ValueError("Trace file contains a query ID outside the selected dataset split")
            current_hashes = _dataset_hashes(args.dataset, split)
            warnings = list(validation.warnings)
            run_path = args.traces.parent / "run.json"
            if run_path.is_file():
                run_metadata = json.loads(run_path.read_text(encoding="utf-8"))
                original_hashes = run_metadata.get("dataset_hashes", {})
                immutable_files = (
                    "sources.jsonl",
                    "passages.jsonl",
                    "pilot_queries.jsonl" if split == "pilot" else "heldout_queries.jsonl",
                )
                changed_inputs = [
                    filename
                    for filename in immutable_files
                    if original_hashes.get(filename) != current_hashes.get(filename)
                ]
                if changed_inputs:
                    raise ValueError(
                        "Trace corpus/query hashes do not match the current dataset: "
                        + ", ".join(changed_inputs)
                    )
                if original_hashes.get("judgments.jsonl") != current_hashes.get("judgments.jsonl"):
                    warnings.append(
                        "Judgments changed since retrieval; current reviewed labels are being applied "
                        "to the unchanged source corpus, queries, and ranking traces."
                    )
            _json(
                write_report(
                    traces,
                    dataset.judgments,
                    args.output,
                    dataset_status=dataset.manifest.status,
                    validation_warnings=warnings,
                    resource_path=args.resources,
                    reported_dataset_hashes=current_hashes,
                    source_review_state=dataset.manifest.source_review_state,
                    source_review_method=dataset.manifest.source_review_method,
                    source_reviewer_ids=dataset.manifest.source_reviewer_ids,
                    expected_query_ids={query.query_id for query in dataset.queries},
                    target_queries=dataset.manifest.targets[split].target_queries,
                    actual_queries=dataset.manifest.targets[split].actual_queries,
                    manifest_reviewed_queries=dataset.manifest.targets[split].reviewed_queries,
                )
            )
            return
        if args.command == "answer-study":
            _json(
                prepare_answer_study(
                    dataset_root=args.dataset,
                    traces_path=args.traces,
                    output_dir=args.output,
                    generator_url=args.generator_url,
                    generator_model=args.generator_model,
                    sample_size=args.sample_size,
                    seed=args.seed,
                    evidence_token_budget=args.evidence_token_budget,
                    max_passages=args.max_passages,
                )
            )
            return
        if args.command == "import-answer-ratings":
            _json(
                import_answer_ratings(
                    ratings_path=args.ratings,
                    packets_path=args.packets,
                    private_key_path=args.private_key,
                    output_dir=args.output,
                )
            )
            return
    except SystemExit:
        raise
    except Exception as error:
        print(f"historical-eval: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
