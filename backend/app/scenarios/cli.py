"""Developer-only commands for validation and versioned scenario publication."""
import argparse
import asyncio
import json
import re
import sys
from hashlib import sha256
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.db.connection import session_factory
from app.db.models import ScenarioContentItem, ScenarioVersion
from app.scenarios.loader import load_scenario, validate_all
from app.scenarios.repository import (
    ScenarioVersionNotFound,
    VersionContentConflict,
    activate_scenario_version,
    import_scenario,
    list_scenario_versions,
    scenario_matches_loaded,
)
from app.scenarios.schema import ScenarioDefinition
from app.scenarios.schema_v2 import ScenarioDefinitionV2
from app.scenarios.validation import validate_definition, validate_v2_definition


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.scenarios.cli")
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate", help="validate one manifest without a database")
    validate.add_argument("path", type=Path)
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
    playable = commands.add_parser(
        "playable-check",
        aliases=["check-playable"],
        help="check an imported release and its SQL-backed content",
    )
    playable.add_argument("slug")
    playable.add_argument("--version", required=True)
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
        if scenario.definition is None:
            raise ValueError(f"{slug}@{version} is an unauthored legacy stub")
        if scenario.schema_version == 2:
            definition = ScenarioDefinitionV2.model_validate(scenario.definition)
            validate_v2_definition(definition)
            rows = (
                await db.execute(
                    select(ScenarioContentItem)
                    .where(ScenarioContentItem.scenario_version_id == scenario.id)
                    .order_by(ScenarioContentItem.item_key)
                )
            ).scalars().all()
            expected = {item.key: item for item in definition.content_items}
            by_key = {row.item_key: row for row in rows}
            if set(by_key) != set(expected):
                raise ValueError("SQL content item rows do not match the immutable v2 manifest")
            for key, item in expected.items():
                row = by_key[key]
                if (
                    row.kind != item.kind
                    or row.language != definition.metadata.language
                    or row.authoritative_scope != "author_context"
                    or row.availability_date != item.availability_date
                    or row.world_valid_from != item.world_valid_from
                    or row.world_valid_to != item.world_valid_to
                    or row.visibility != item.visibility
                    or row.mode != item.mode
                    or row.review_status != item.review_status
                    or row.source_refs != item.source_refs
                    or row.metadata_json != {"source_file": item.file}
                    or row.sha256 != sha256(row.body.encode("utf-8")).hexdigest()
                ):
                    raise ValueError(f"SQL content item {key!r} does not match the v2 release")
            print(
                f"Playable-check passed: {slug}@{version} has a playable default role, "
                f"authoritative opening date, and {len(rows)} SQL content item(s). "
                "Evidence readiness is separate and requires eligible approved passages."
            )
            return 0
        if scenario.schema_version == 1:
            definition = ScenarioDefinition.model_validate(scenario.definition)
            validate_definition(definition)
            print(f"Playable-check passed: {slug}@{version} (schema 1 compatibility release).")
            return 0
        raise ValueError(f"unsupported stored schema_version: {scenario.schema_version!r}")


async def _run_database_command(args) -> int:
    if args.command == "import":
        return await _import_command(args.path, args.dry_run)
    if args.command == "activate":
        return await _activate_command(args.slug, args.version)
    if args.command == "list":
        return await _list_command()
    if args.command in {"playable-check", "check-playable"}:
        return await _playable_check_command(args.slug, args.version)
    return await _show_command(args.slug, args.version)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate":
            print(_summary(load_scenario(args.path)))
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
