"""Runtime schema support separate from manifest structural validation."""

from dataclasses import dataclass

SUPPORTED_RUNTIME_SCHEMA_VERSIONS = frozenset({1})


class ScenarioRuntimeIncompatible(ValueError):
    """A structurally valid release cannot be run by this application version."""


@dataclass(frozen=True)
class RuntimeCompatibility:
    status: str
    compatible: bool
    schema_version: int | None
    supported_schema_versions: tuple[int, ...]


def runtime_compatibility(scenario) -> RuntimeCompatibility:
    """Report whether the runtime can execute the stored release schema."""
    schema_version = scenario.schema_version
    definition = scenario.definition
    declared_version = definition.get("schema_version") if isinstance(definition, dict) else None
    compatible = (
        type(schema_version) is int
        and schema_version == declared_version
        and schema_version in SUPPORTED_RUNTIME_SCHEMA_VERSIONS
    )
    return RuntimeCompatibility(
        status="supported" if compatible else "unsupported",
        compatible=compatible,
        schema_version=schema_version if type(schema_version) is int else None,
        supported_schema_versions=tuple(sorted(SUPPORTED_RUNTIME_SCHEMA_VERSIONS)),
    )


def require_runtime_compatible(scenario) -> None:
    """Fail before activation or session creation when this runtime cannot play it."""
    compatibility = runtime_compatibility(scenario)
    if compatibility.compatible:
        return
    version = compatibility.schema_version
    raise ScenarioRuntimeIncompatible(
        f"{scenario.slug}@{scenario.version}: schema version {version} is not supported by "
        f"this runtime (supported: {', '.join(map(str, compatibility.supported_schema_versions))}); "
        "keep the release inactive until runtime support is implemented"
    )
