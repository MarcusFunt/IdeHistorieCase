"""Developer-only commands for validation and versioned scenario publication."""
import argparse
import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.db.connection import session_factory
from app.db.models import ScenarioVersion
from app.scenarios.loader import load_scenario, validate_all
from app.scenarios.repository import (
    ScenarioVersionNotFound,
    VersionContentConflict,
    activate_scenario_version,
    import_scenario,
    list_scenario_versions,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.scenarios.cli")
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate", help="validate one manifest without a database")
    validate.add_argument("path", type=Path)
    validate_many = commands.add_parser("validate-all", help="validate manifests in a directory")
    validate_many.add_argument("directory", type=Path)

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
    return parser


def _summary(loaded) -> str:
    definition = loaded.definition
    bundle = f", bundle-sha256:{loaded.bundle_hash}" if loaded.bundle_hash else ""
    return (
        f"{definition.slug}@{definition.version} "
        f"(schema {definition.schema_version}, sha256:{loaded.content_hash}{bundle}; "
        f"{len(definition.phases)} phases, {len(definition.dilemmas)} dilemmas)"
    )

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
        elif (
            existing.schema_version == definition.schema_version
            and existing.content_hash == loaded.content_hash
            and existing.bundle_hash == loaded.bundle_hash
            and existing.behavior_rules == loaded.behavior_rules
            and existing.evidence_policy == loaded.evidence_policy
            and existing.definition == definition.model_dump(mode="json")
            and existing.initial_state == loaded.initial_state
        ):
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
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


async def _run_database_command(args) -> int:
    if args.command == "import":
        return await _import_command(args.path, args.dry_run)
    if args.command == "activate":
        return await _activate_command(args.slug, args.version)
    if args.command == "list":
        return await _list_command()
    return await _show_command(args.slug, args.version)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate":
            print(_summary(load_scenario(args.path)))
            return 0
        if args.command == "validate-all":
            scenarios = validate_all(args.directory)
            for loaded in scenarios:
                print(_summary(loaded))
            print(f"validated {len(scenarios)} manifest(s)")
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
