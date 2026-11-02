from __future__ import annotations

from .schemas import EvaluationQuery

SYSTEM_PROMPT = """You are answering a historical question for a human evaluation.
Answer in the language used by the question. Treat supplied passages as the complete evidence available for this answer. Do not invent details or citations. Cite each historical claim with one or more supplied passage IDs in square brackets, such as [p-article-231]. If the supplied evidence is insufficient, say what cannot be established. Keep the answer concise. A correct fact without support in the supplied passages is still possible; do not pretend that an unsupported fact is supported."""


def build_messages(
    query: EvaluationQuery,
    evidence_block: str,
) -> list[dict[str, str]]:
    simulation_context = ""
    if query.kind == "simulation":
        creation_cutoff = query.source_creation_cutoff or query.as_of
        publication_cutoff = query.publication_cutoff or query.as_of
        simulation_context = (
            f"\nMode: actor-restricted simulation. Actor: {query.actor_id}. "
            f"In-world date: {query.as_of.isoformat()}. "
            f"Source-document creation cutoff: {creation_cutoff.isoformat()}. "
            f"Publication cutoff: {publication_cutoff.isoformat()}. "
            "Use only evidence available to this actor by the in-world date; do not reveal future information."
        )
    evidence = evidence_block if evidence_block else "No source passages were supplied."
    user_content = (
        f"Question: {query.text}{simulation_context}\n\n"
        f"Supplied historical passages:\n{evidence}\n\n"
        "Answer the question in its language."
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
