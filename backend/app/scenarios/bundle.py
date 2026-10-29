"""Byte-exact content hash for a manifest plus its authored research sidecars."""
import hashlib
from pathlib import Path

MAX_BUNDLE_BYTES = 32 * 1024 * 1024


def scenario_bundle_hash(manifest_path: str | Path, manifest_bytes: bytes) -> str | None:
    manifest = Path(manifest_path)
    root = manifest.parent
    files: dict[str, bytes] = {manifest.name: manifest_bytes}
    sidecar_pattern = f"{manifest.stem}_*.yaml"
    for path in root.glob(sidecar_pattern):
        if path.is_file():
            files[path.name] = path.read_bytes()
    sources = root / "sources"
    if sources.is_dir():
        for path in sources.rglob("*.yaml"):
            if path.is_file():
                files[path.relative_to(root).as_posix()] = path.read_bytes()
    if len(files) == 1:
        return None
    if sum(len(content) for content in files.values()) > MAX_BUNDLE_BYTES:
        raise ValueError(f"scenario sidecar bundle exceeds {MAX_BUNDLE_BYTES} bytes")

    digest = hashlib.sha256()
    for relative_path, content in sorted(files.items()):
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()
