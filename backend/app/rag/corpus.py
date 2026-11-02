"""Bounded YAML corpus loading and content-addressed manifest identity."""
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
from yaml.nodes import MappingNode
from yaml.tokens import AliasToken, AnchorToken

from app.rag.schemas import RagCorpus

MAX_CORPUS_BYTES = 256 * 1024


class _UniqueSafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader: _UniqueSafeLoader, node: MappingNode, deep: bool = False):
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping", node.start_mark,
                "unhashable mapping key", key_node.start_mark,
            ) from exc
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping", node.start_mark,
                f"duplicate key {key!r}", key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueSafeLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


@dataclass(frozen=True)
class LoadedCorpus:
    corpus: RagCorpus
    corpus_sha256: str
    path: Path


def load_corpus(path: Path) -> LoadedCorpus:
    """Load one bounded corpus and verify every exact passage hash."""
    try:
        with path.open("rb") as stream:
            content = stream.read(MAX_CORPUS_BYTES + 1)
    except OSError as exc:
        raise ValueError(f"cannot read RAG corpus {path}: {exc}") from exc
    if len(content) > MAX_CORPUS_BYTES:
        raise ValueError(f"RAG corpus exceeds maximum size of {MAX_CORPUS_BYTES} bytes")
    try:
        source = content.decode("utf-8-sig")
        for token in yaml.scan(source, Loader=_UniqueSafeLoader):
            if isinstance(token, (AliasToken, AnchorToken)):
                raise ValueError("YAML anchors and aliases are not supported in RAG corpora")
        raw = yaml.load(source, Loader=_UniqueSafeLoader)
        corpus = RagCorpus.model_validate(raw)
    except ValueError:
        raise
    except (yaml.YAMLError, ValidationError) as exc:
        if isinstance(exc, ValidationError):
            detail = "; ".join(
                f"{'.'.join(str(part) for part in error['loc']) or '$'}: {error['msg']}"
                for error in exc.errors()
            )
        else:
            detail = str(getattr(exc, "problem", None) or exc)
        raise ValueError(f"invalid RAG corpus: {detail}") from exc
    if not isinstance(raw, dict):
        raise ValueError("RAG corpus must be one YAML mapping")
    return LoadedCorpus(corpus, sha256(content).hexdigest(), path)
