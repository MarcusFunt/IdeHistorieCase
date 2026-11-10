"""Developer-only commands for validation and versioned scenario publication."""
import argparse
import asyncio
import json
import re
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.db.connection import session_factory
from app.db.models import (
    HistoricalPassage,
    HistoricalPassageReview,
    RagScenarioPolicy,
    ScenarioContentItem,
    ScenarioVersion,
)
from app.scenarios.content_integrity import (
    ScenarioContentIntegrityError,
    verify_v2_content_rows,
)
from app.scenarios.evidence import VALID_SCOPES
from app.scenarios.loader import load_scenario, validate_all
from app.scenarios.repository import (
    ScenarioVersionNotFound,
    VersionContentConflict,
    activate_scenario_version,
    import_scenario,
    list_scenario_versions,
    scenario_matches_loaded,
)
from app.scenarios.runtime_compatibility import runtime_compatibility
from app.scenarios.schema import ScenarioDefinition
from app.scenarios.schema_v2 import ScenarioDefinitionV2
from app.scenarios.validation import validate_definition, validate_v2_definition


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.scenarios.cli")
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate", help="validate one manifest without a database")
    validate.add_argument("path", type=Path)
    validate.add_argument("--json", action="store_true", help="emit a machine-readable validation report")
    validate_many = commands.add_parser("validate-all", help="validate manifests in a directory")
    validate_many.add_argument("directory", type=Path)
    preview = commands.add_parser("preview", help="preview a validated release without a database")
    preview.add_argument("path", type=Path)

    verify_pins = commands.add_parser(
        "verify-bundle-pins", help="verify byte-exact pins for authored scenario bundles"
    )
    verify_pins.add_argument("directory", type=Path)

    import_command = commands.add_parser("import", help="insert one immutable scenario version")
    import_command.add_argument("path", type=Path)
    import_command.add_argument("--dry-run", action="store_true")

    activate = commands.add_parser("activate", help="select the version used for new sessions")
    activate.add_argument("slug")
    activate.add_argument("--version", required=True)

    commands.add_parser("list", help="list imported scenario versions")
    show = commands.add_parser("show", help="inspect one imported version")
    show.add_argument("slug")
    show.add_argument("--version", required=True)
    readiness = commands.add_parser(
        "readiness",
        aliases=["playable-check", "check-playable"],
        help="report schema, publication, runtime, author, and evidence readiness as JSON",
    )
    readiness.add_argument("slug")
    readiness.add_argument("--version", required=True)
    return parser


def _summary(loaded) -> str:
    definition = loaded.definition
    bundle = f", bundle-sha256:{loaded.bundle_hash}" if loaded.bundle_hash else ""
    if isinstance(definition, ScenarioDefinitionV2):
        details = f"{len(definition.roles)} roles, {len(definition.content_items)} content items"
    else:
        details = f"{len(definition.phases)} phases, {len(definition.dilemmas)} dilemmas"
    return (
        f"{definition.slug}@{definition.version} "
        f"(schema {definition.schema_version}, sha256:{loaded.content_hash}{bundle}; "
        f"{details})"
    )


def _preview(loaded) -> None:
    print(f"Preview: {_summary(loaded)}")
    definition = loaded.definition
    if isinstance(definition, ScenarioDefinitionV2):
        print(f"Opening date: {definition.chronology.opening_date.isoformat()}")
        print(f"Default role: {definition.default_role_id}")
        print("Historical passage approval: separate from author-content import")
        for item in definition.content_items:
            body = loaded.content_items[item.key]
            excerpt = body[:400] + ("…" if len(body) > 400 else "")
            print(f"Content {item.key} [{item.mode}, {item.review_status}]: {excerpt}")
        if loaded.evidence_policy is None:
            print("Evidence readiness: no source access policy attached")


def _verify_bundle_pins(directory: Path) -> tuple[int, int]:
    scenarios = validate_all(directory)
    expected_pins: set[Path] = set()
    verified = 0
    for loaded in scenarios:
        if loaded.bundle_hash is None:
            continue
        if loaded.path is None:
            raise ValueError("scenario loader did not retain a manifest path")
        pin_path = loaded.path.with_name(f"{loaded.path.stem}_bundle.sha256")
        expected_pins.add(pin_path)
        if not pin_path.is_file():
            raise ValueError(f"missing required bundle pin: {pin_path}")
        try:
            pinned_hash = pin_path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError) as exc:
            raise ValueError(f"cannot read bundle pin {pin_path}: {exc}") from exc
        if re.fullmatch(r"[0-9a-f]{64}", pinned_hash) is None:
            raise ValueError(f"malformed SHA-256 bundle pin: {pin_path}")
        if pinned_hash != loaded.bundle_hash:
            raise ValueError(
                f"bundle hash mismatch for {loaded.path}: "
                f"pin {pinned_hash}, computed {loaded.bundle_hash}"
            )
        verified += 1

    present_pins = {
        path for path in directory.rglob("*_bundle.sha256") if path.is_file()
    }
    orphaned_pins = sorted(present_pins - expected_pins)
    if orphaned_pins:
        raise ValueError(f"bundle pin has no matching scenario bundle: {orphaned_pins[0]}")
    return verified, len(scenarios)

async def _import_command(path: Path, dry_run: bool) -> int:
    loaded = load_scenario(path)
    definition = loaded.definition
    if dry_run:
        async with session_factory() as db:
            existing = (
                await db.execute(
                    select(ScenarioVersion).where(
                        ScenarioVersion.slug == definition.slug,
                        ScenarioVersion.version == definition.version,
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                print(f"Dry run: would insert {_summary(loaded)}")
            elif await scenario_matches_loaded(db, existing, loaded):
                print(f"Dry run: unchanged {_summary(loaded)}")
            else:
                raise VersionContentConflict(
                    f"{definition.slug}@{definition.version} already contains different content; "
                    "increment the scenario version"
                )
        return 0

    async with session_factory() as db, db.begin():
        result = await import_scenario(db, loaded)
    print(f"{result.status}: {_summary(loaded)}")
    return 0


async def _activate_command(slug: str, version: str) -> int:
    async with session_factory() as db, db.begin():
        scenario = await activate_scenario_version(db, slug, version)
    print(f"Active for new sessions: {scenario.slug}@{scenario.version}")
    return 0


async def _list_command() -> int:
    async with session_factory() as db:
        scenarios = await list_scenario_versions(db)
    if not scenarios:
        print("No scenario versions found.")
        return 0
    for scenario in scenarios:
        authored = scenario.definition is not None
        title = scenario.definition.get("metadata", {}).get("title") if authored else None
        label = title or "legacy unauthored stub"
        state = "active" if scenario.is_active else "inactive"
        digest = f", sha256:{scenario.content_hash}" if scenario.content_hash else ""
        print(f"{state}: {scenario.slug}@{scenario.version} ({label}{digest})")
    return 0


async def _show_command(slug: str, version: str) -> int:
    async with session_factory() as db:
        scenario = (
            await db.execute(
                select(ScenarioVersion).where(
                    ScenarioVersion.slug == slug,
                    ScenarioVersion.version == version,
                )
            )
        ).scalar_one_or_none()
    if scenario is None:
        raise ScenarioVersionNotFound(f"scenario version not found: {slug}@{version}")
    if scenario.definition is None:
        print(
            f"{scenario.slug}@{scenario.version}: legacy unauthored stub "
            f"(active={str(scenario.is_active).lower()}, initial_state={scenario.initial_state})"
        )
        return 0
    async with session_factory() as db:
        content_items = (
            await db.execute(
                select(ScenarioContentItem)
                .where(ScenarioContentItem.scenario_version_id == scenario.id)
                .order_by(ScenarioContentItem.item_key)
            )
        ).scalars().all()
    print(
        json.dumps(
            {
                "slug": scenario.slug,
                "version": scenario.version,
                "schema_version": scenario.schema_version,
                "active": scenario.is_active,
                "content_hash": scenario.content_hash,
                "digest": f"sha256:{scenario.content_hash}",
                "bundle_hash": scenario.bundle_hash,
                "initial_state": scenario.initial_state,
                "definition": scenario.definition,
                "content_items": [
                    {
                        "key": item.item_key,
                        "kind": item.kind,
                        "language": item.language,
                        "body": item.body,
                        "authoritative_scope": item.authoritative_scope,
                        "availability_date": (
                            item.availability_date.isoformat() if item.availability_date else None
                        ),
                        "world_valid_from": (
                            item.world_valid_from.isoformat() if item.world_valid_from else None
                        ),
                        "world_valid_to": (
                            item.world_valid_to.isoformat() if item.world_valid_to else None
                        ),
                        "visibility": item.visibility,
                        "mode": item.mode,
                        "review_status": item.review_status,
                        "source_refs": item.source_refs,
                        "sha256": item.sha256,
                    }
                    for item in content_items
                ],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


async def _playable_check_command(slug: str, version: str) -> int:
    async with session_factory() as db:
        scenario = (
            await db.execute(
                select(ScenarioVersion).where(
                    ScenarioVersion.slug == slug,
                    ScenarioVersion.version == version,
                )
            )
        ).scalar_one_or_none()
        if scenario is None:
            raise ScenarioVersionNotFound(f"scenario version not found: {slug}@{version}")
        report = await scenario_readiness_report(db, scenario)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if report["playable"] else 1


async def scenario_readiness_report(db, scenario: ScenarioVersion) -> dict:
    definition = None
    schema_error = None
    try:
        if scenario.definition is None:
            raise ValueError("release has no authored definition")
        if scenario.schema_version == 1:
            definition = ScenarioDefinition.model_validate(scenario.definition)
            validate_definition(definition)
        elif scenario.schema_version == 2:
            definition = ScenarioDefinitionV2.model_validate(scenario.definition)
            validate_v2_definition(definition)
        else:
            raise ValueError(f"unsupported stored schema_version: {scenario.schema_version!r}")
    except ValueError as exc:
        schema_error = str(exc)

    schema_check = {
        "status": "valid" if definition is not None else "invalid",
        "error": schema_error,
    }
    publication_check = {"status": "not_applicable"}
    author_check = {"status": "not_applicable"}
    if scenario.schema_version == 2:
        publication_check = {"status": "not_established"}
        author_check = {"status": "not_established"}
    if definition is not None and scenario.schema_version == 2:
        rows = (
            await db.execute(
                select(ScenarioContentItem)
                .where(ScenarioContentItem.scenario_version_id == scenario.id)
                .order_by(ScenarioContentItem.item_key)
            )
        ).scalars().all()
        try:
            verified_rows = verify_v2_content_rows(scenario, rows)
            publication_check = {"status": "valid", "content_item_count": len(verified_rows)}
            draft_keys = sorted(
                item.key
                for item in definition.content_items
                if item.review_status != "author_reviewed"
            )
            author_check = {
                "status": "not_ready" if draft_keys else "ready",
                "draft_item_keys": draft_keys,
            }
        except ScenarioContentIntegrityError as exc:
            publication_check = {"status": "invalid", "error": str(exc)}
            author_check = {"status": "not_established"}

    compatibility = runtime_compatibility(scenario)
    runtime_check = {
        "status": compatibility.status,
        "schema_version": compatibility.schema_version,
        "supported_schema_versions": list(compatibility.supported_schema_versions),
    }
    evidence_check = await _evidence_readiness(db, scenario, definition)
    return {
        "scenario": {"slug": scenario.slug, "version": scenario.version},
        "checks": {
            "schema_valid": schema_check,
            "publication_integrity": publication_check,
            "runtime_compatibility": runtime_check,
            "author_content_readiness": author_check,
            "evidence_readiness": evidence_check,
        },
        "playable": (
            schema_check["status"] == "valid"
            and publication_check["status"] in {"valid", "not_applicable"}
            and runtime_check["status"] == "supported"
            and author_check["status"] in {"ready", "not_applicable"}
            and evidence_check["status"] in {"ready", "not_required"}
        ),
    }


async def _evidence_readiness(db, scenario: ScenarioVersion, definition) -> dict:
    source_ids: Sequence[str] = getattr(definition, "source_refs", ()) if definition else ()
    required = set(source_ids)
    if not required:
        return {"status": "not_required", "required_source_ids": [], "approved_source_ids": []}

    policy = scenario.evidence_policy
    entries = policy.get("source_access") if isinstance(policy, dict) else None
    policy_identity_valid = (
        isinstance(policy, dict)
        and policy.get("scenario_slug") == scenario.slug
        and str(policy.get("scenario_version")) == scenario.version
        and type(policy.get("policy_version")) is int
        and isinstance(policy.get("citation_ready"), bool)
        and isinstance(entries, list)
    )
    policy_ids: set[str] = set()
    blocked_policy_ids: set[str] = set()
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        source_id = entry.get("source_id")
        availability = entry.get("in_world_available_from")
        scope = entry.get("scope")
        actor_ids = entry.get("actor_ids")
        entry_valid = (
            isinstance(source_id, str)
            and bool(source_id)
            and isinstance(scope, str)
            and scope in VALID_SCOPES
            and isinstance(actor_ids, list)
            and all(isinstance(actor_id, str) and actor_id for actor_id in actor_ids)
        )
        if not entry_valid:
            continue
        if availability == "blocked":
            blocked_policy_ids.add(source_id)
            continue
        if availability != "postgame":
            if not isinstance(availability, str):
                continue
            try:
                date.fromisoformat(availability)
            except ValueError:
                continue
        policy_ids.add(source_id)
    missing_policy_ids = sorted(required - policy_ids)
    blocked_policy_source_ids = sorted(required & blocked_policy_ids)
    rag_policy = (
        await db.execute(
            select(RagScenarioPolicy.id).where(
                RagScenarioPolicy.scenario_version_id == scenario.id,
                RagScenarioPolicy.scenario_slug == scenario.slug,
                RagScenarioPolicy.scenario_version == scenario.version,
            )
        )
    ).scalar_one_or_none()
    latest_review = (
        select(
            HistoricalPassageReview.passage_id.label("passage_id"),
            func.max(HistoricalPassageReview.revision).label("revision"),
        )
        .group_by(HistoricalPassageReview.passage_id)
        .subquery()
    )
    approved = set(
        (
            await db.execute(
                select(HistoricalPassage.source_identifier)
                .join(latest_review, latest_review.c.passage_id == HistoricalPassage.id)
                .join(
                    HistoricalPassageReview,
                    (HistoricalPassageReview.passage_id == latest_review.c.passage_id)
                    & (HistoricalPassageReview.revision == latest_review.c.revision),
                )
                .where(
                    HistoricalPassage.scenario_version_id == scenario.id,
                    HistoricalPassage.scenario_slug == scenario.slug,
                    HistoricalPassage.scenario_version == scenario.version,
                    HistoricalPassage.source_identifier.in_(required),
                    HistoricalPassageReview.review_status == "approved",
                    HistoricalPassageReview.rights_status == "approved",
                )
            )
        ).scalars().all()
    )
    missing_approved = sorted(required - approved)
    ready = (
        policy_identity_valid
        and not missing_policy_ids
        and not blocked_policy_source_ids
        and not missing_approved
        and rag_policy is not None
    )
    return {
        "status": "ready" if ready else "not_ready",
        "required_source_ids": sorted(required),
        "approved_source_ids": sorted(approved),
        "missing_policy_source_ids": missing_policy_ids,
        "blocked_policy_source_ids": blocked_policy_source_ids,
        "missing_approved_source_ids": missing_approved,
        "source_policy_identity_valid": policy_identity_valid,
        "rag_policy_present": rag_policy is not None,
    }


def _input_validation_report(loaded) -> dict:
    definition = loaded.definition
    compatibility_scenario = type("Scenario", (), {
        "schema_version": definition.schema_version,
        "definition": definition.model_dump(mode="json"),
    })()
    compatibility = runtime_compatibility(compatibility_scenario)
    if isinstance(definition, ScenarioDefinitionV2):
        draft_keys = sorted(
            item.key for item in definition.content_items if item.review_status != "author_reviewed"
        )
        author_status = "not_ready" if draft_keys else "ready"
    else:
        draft_keys = []
        author_status = "not_applicable"
    return {
        "scenario": {"slug": definition.slug, "version": definition.version},
        "checks": {
            "schema_valid": {"status": "valid"},
            "publication_integrity": {"status": "database_check_required"},
            "runtime_compatibility": {
                "status": compatibility.status,
                "schema_version": compatibility.schema_version,
                "supported_schema_versions": list(compatibility.supported_schema_versions),
            },
            "author_content_readiness": {"status": author_status, "draft_item_keys": draft_keys},
            "evidence_readiness": {
                "status": "database_check_required" if definition.source_refs else "not_required"
            },
        },
        "playable": False,
    }


async def _run_database_command(args) -> int:
    if args.command == "import":
        return await _import_command(args.path, args.dry_run)
    if args.command == "activate":
        return await _activate_command(args.slug, args.version)
    if args.command == "list":
        return await _list_command()
    if args.command in {"readiness", "playable-check", "check-playable"}:
        return await _playable_check_command(args.slug, args.version)
    return await _show_command(args.slug, args.version)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate":
            loaded = load_scenario(args.path)
            if args.json:
                print(
                    json.dumps(
                        _input_validation_report(loaded),
                        ensure_ascii=False,
                        sort_keys=True,
                        indent=2,
                    )
                )
            else:
                print(_summary(loaded))
            return 0
        if args.command == "preview":
            _preview(load_scenario(args.path))
            return 0
        if args.command == "validate-all":
            scenarios = validate_all(args.directory)
            for loaded in scenarios:
                print(_summary(loaded))
            print(f"validated {len(scenarios)} manifest(s)")
            return 0
        if args.command == "verify-bundle-pins":
            pins, manifests = _verify_bundle_pins(args.directory)
            print(f"verified {pins} bundle pin(s) across {manifests} manifest(s)")
            return 0
        return asyncio.run(_run_database_command(args))
    except (ValueError, OSError) as exc:
        print(f"scenario command failed: {exc}", file=sys.stderr)
        return 2
    except SQLAlchemyError as exc:
        print(f"scenario database command failed ({type(exc).__name__})", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
