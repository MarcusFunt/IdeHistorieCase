# Historical retrieval evaluation design

**Status:** Approved, revised 2026-10-30<br>
**Date:** 2026-10-30<br>
**Primary repository:** IdeHistorieCase<br>
**Retrieval service dependency:** Local-AI-API-LITE

## Intent

Measure whether the historical retrieval configurations improve evidence discovery enough to justify their CPU, memory, and latency cost. Stage 1 evaluates retrieval only over versioned, real source passages. A later, smaller human study measures answer quality without making generation a gate for retrieval-harness acceptance.

## Goals

- Add a local evaluation harness and a versioned Versailles source-passage corpus under backend/evaluation_data/versailles-1919/, outside the published scenario bundle.
- Compare BM25, exact dense Qwen retrieval, BM25 plus Qwen reciprocal-rank fusion, and hybrid retrieval reranked by GTE.
- Target a 40-query bilingual pilot and a 100-query held-out evaluation. Compute quality metrics on a reviewed query sample and report its coverage; exhaustive human review is not required. Human and high-capability agent reviews are both valid label provenance. Pool top results from every retrieval method for the sampled queries.
- Measure Recall@5, MRR@10, nDCG@10, evidence coverage@5/@10, paired reranking changes, latency, CPU, and memory.
- Implement real-model clients for the existing Local-AI-API-LITE workers and verify service health, readiness, identity, and pinned model revisions before a real-model run.
- Keep answer-quality evaluation as a separate study over a stratified 25–40 held-out questions whose oracle evidence has been reviewed; other held-out questions and sources do not require review.

## Non-goals

- Add, replace, or reconfigure retrieval services in Local-AI-API-LITE. Its existing CPU Qwen and GTE services remain the model runtime for this milestone.
- Introduce Text Embeddings Inference (TEI) or another model-serving engine in the implementation. TEI and alternative engines may be compared later as optional performance experiments.
- Change the production turn worker, scenario publication, database schema, public API, product source catalog, or citation readiness.
- Require an answer generator, AI judge, or answer-quality ratings for Stage 1 retrieval acceptance.
- Start, stop, or restart inference services from the evaluation runner.

## Existing model-service interface

Local-AI-API-LITE main commit 87f610041736dc5012f85238c724ed53816e4604 already contains the CPU-only workers in deploy/retrieval/compose.yaml, on the private ddu_inference_bus network with aliases ddu-embed and ddu-rerank. Preserve that deployment and its service lifecycle.

The evaluation clients use these worker contracts:

- Before a run, GET /health and GET /ready must both return 200. GET /v1/models must return the expected alias, task, and pinned model revision. Fail closed on a missing service, loading worker, unexpected identity or revision, malformed response, non-finite value, or a dimension mismatch.
- Qwen: POST http://ddu-embed:8080/v1/embeddings with model ddu-embed, input_type query or document, and encoding_format float. Query instructions are applied by the worker; documents are unprefixed. Batch no more than eight strings. Verify ordered results, 1,024 finite dimensions, and normalized vectors. Pin model Qwen/Qwen3-Embedding-0.6B at revision 97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3 and record query instruction version qwen3-question-to-passages-en-v1.
- GTE: POST http://ddu-rerank:8080/v1/rerank with model ddu-rerank, query, up to 20 documents, and top_n when needed. Validate returned original indexes and finite scores. Pin Alibaba-NLP/gte-multilingual-reranker-base at revision 8215cf04918ba6f7b6a62bb44238ce2953d8831c; record reviewed custom-code revision 40ced75c3017eb27626c9d4ea981bde21a2662f4 with the worker source/image identity.
- The existing worker API does not expose a TEI /metrics endpoint. Record endpoint wall time and response token usage; the host-side sampler records Docker CPU and memory. Missing worker-side timing or remote-host samples are marked unavailable, never reported as zero.

## Evaluation data and temporal access

Keep evaluation data separate from scenarios and runtime state. Store a manifest, sources, exact passage excerpts, queries, and graded judgments as separate files. Each passage has a stable ID, exact text and SHA-256, source URL/title/institution, source document creation date, publication date, text anchor/page, and an actor-knowledge availability rule. Retain attribution and access dates. The source policy remains citation_ready: false.

Queries identify language, topic, question kind, answerability, and any actor and as-of time. Distinguish retrospective historical QA from actor/time-restricted simulation. Store document creation, publication, and actor knowledge availability as separate fields. Retrospective QA can retrieve historical records without pretending an actor in 1919 knew later events. Simulation retrieval filters passages by the specified actor's knowledge availability at the query time.

The initial data status is draft. A manifest records target and actual counts, optional source/query review state, judgment coverage, and hashes. Retrieval-run completeness depends on dataset validity and trace coverage, not review coverage. Quality reports use fully judged reviewed queries, identify whether labels were reviewed by a person, an agent, or both, and show the reviewed sample size. Missing judgments are not treated as zero. A full held-out retrieval run can be reported with a sampled quality result.

Judgments use a 0–3 relevance scale plus one or more required evidence groups. A query can have multiple relevant passages, and each evidence group can name alternate passages that support the same required fact. Keep labels separate from query and corpus content.

For pooling, combine the top ten unique passages per selected query from BM25, dense, hybrid, and reranked results. Export passages with randomized IDs and profile/rank provenance withheld from the reviewer or review agent. Import grades and evidence-group links through a validated format and store review method and reviewer identity. A high-capability review agent may use the source URL and other authoritative internet sources to validate labels. Unjudged queries are excluded from graded aggregates, not from retrieval results; report the labeled sample size, query/pool coverage, and provenance.

## Retrieval configurations

Run the same query set, corpus, temporal filter, and top-k settings through:

1. BM25: deterministic tokenization, term-frequency/inverse-document-frequency implementation, and parameters pinned in the manifest.
2. Dense Qwen: exact cosine ranking over normalized real Qwen vectors. Measure corpus embedding/index construction separately from per-query query embedding and ranking.
3. Hybrid: reciprocal-rank fusion of BM25 and dense rankings with fixed, recorded parameters.
4. Hybrid + GTE: send the fixed hybrid candidate pool to the real GTE worker. Preserve the pre-rerank candidates and final ranking.

All rankings carry passage ID, rank, retrieval profile, component scores where applicable, and returned source text. Apply temporal filters before ranking. Never substitute mock vectors or a local fake reranker in real-model comparison mode.

## Stage 1: retrieval-only study

Stage 1 does not call a text-generation model. It produces per-query retrieval traces and aggregate retrieval reports. Metrics are:

- Recall@5 over judged relevant passages.
- MRR@10 over judged relevant passages.
- Graded nDCG@10 with relevance grades 0–3.
- Required evidence group coverage@5 and @10.
- Paired pre/post GTE deltas for Recall@5, MRR@10, nDCG@10, and coverage, with per-query rank movement.
- Warm and cold client latency for embedding, lexical/dense/hybrid computation, reranking, and total retrieval; report p50/p95 and corpus-index construction separately.
- CPU and peak/mean RSS by worker and configuration from Docker sampling, with host, interval, limits, and missing telemetry recorded.

Report pilot and held-out separately, by language, topic, and query kind. Provide paired held-out deltas and bootstrap confidence intervals over the reviewed query sample, alongside sample size and coverage. Put quality and resource measurements side by side; do not collapse them into a single score. A run with no reviewed labels can still be retrieval-complete, with quality metrics marked unavailable.

## Resource and run protocol

The evaluator runs as a short-lived, resource-limited client on ddu_inference_bus. It reads evaluation data read-only and writes to an ignored local results directory. It receives no PostgreSQL connection, Docker socket, or production API port. A fixed host-side PowerShell wrapper samples only the two named retrieval containers using Docker stats and writes timestamped JSONL resource samples. The service workers and evaluation client remain separate.

Measure local orchestration and model-service request latency separately. The worker API reports token usage but no per-request server duration, so endpoint wall time is the worker-side proxy available to the client. For remote workers, report client network-plus-queue latency separately; CPU and RSS are complete only when the worker host sampler is collected.

Cold-start measurement is operator-managed: the operator starts the retrieval workers from a stopped state with model weights already cached, and the wrapper records container start, readiness, and first request timestamps. The evaluator never starts or stops a service. Cold means process/model load, not model download. Warm measurements follow a fixed warm-up count. Do not mix local and remote, cold and warm, or different worker revisions in one comparison.

## Stage 2: answer-quality study

Once the 100-query held-out set and retrieval traces exist, sample 25–40 held-out questions stratified by language, topic, answerability, and question kind from queries with reviewed oracle-evidence labels. Only this selected sample needs graded evidence review; source review and judgments for the remaining held-out queries are optional. Compare retrieved-evidence answers with an oracle-evidence control using the same answer model, prompt, maximum passage count, evidence-token budget, and citation format. Oracle selection fills the same evidence budget with the highest-graded reviewed evidence passages; retrieval conditions fill it from each profile's ranking. A no-context condition may be included as a separate diagnostic with its own evidence-support interpretation.

Blind profile labels and randomize answer order. For generated-answer quality, blind human ratings remain primary on the selected 25–40 answer sample; this does not require every held-out query or source to be human-reviewed. Score separately:

- Historical factual accuracy, independent of the evidence supplied.
- Support or contradiction by the supplied evidence.
- Citation correctness: cited passage exists, was supplied, and supports the attached claim.
- Required-point coverage, relevance, and temporal consistency.

For every factual claim, record factual status, evidence-support status, and citation status as separate fields. A no-context claim can be factually correct while unsupported by the provided evidence; do not automatically count it as factually false. Report unsupported and contradicted claims separately. AI judging is optional and secondary.

Retrospective QA and actor/time-restricted simulation are separate strata. For simulation, reviewers see the actor, time, and allowed knowledge set and score whether the answer leaks later information. The answer study does not change Stage 1 acceptance.

## Artifacts and comparison identity

The CLI writes query-level JSONL traces with exact retrieved passages, scores, ranks, timing, query/corpus hashes, worker identity, model revisions, and runtime configuration. It exports pooled judgments, blinded answer packets, validated human-rating imports, and Markdown/JSON summaries. Results go under a caller-selected ignored directory.

Runs are comparable only when corpus and split hashes, judgment file, retrieval parameters, prompt/model settings if applicable, worker revisions, limits, execution location, and cache state match. Reports identify judgment provenance and sample coverage and never treat missing labels or metrics as zero. Review coverage is descriptive and does not block retrieval-run completeness.

## Acceptance stages

1. Retrieval harness: dataset and manifest validation, deterministic BM25, trace format, retrieval metrics, pooling export/import, and report generation. No generator or live model service is required for this stage.
2. Real-model retrieval comparison: actual Qwen and GTE calls through the existing LITE endpoints; health/readiness/model identity and pinned model revisions verified; all four retrieval profiles produce traces; reviewed-sample quality, latency, and Docker resource costs reported. Review coverage is shown but does not gate trace acceptance.
3. Answer-quality study: optional follow-on acceptance using 25–40 stratified held-out cases with reviewed oracle evidence and blind human answer ratings. Exhaustive review of the 100-query set or the full source corpus is not required.
4. Optional optimization: TEI or other engines may be tested as separate A/B experiments with identical corpus, queries, and resource controls. They are not part of this implementation milestone.

## Source and repository boundaries

Existing 12 Versailles answer goldens seed the pilot design but are not retrieval judgments by themselves. Use exact, source-checked passages from authoritative source documents and record source anchors; do not copy unreviewed R13/R14 material or later FRUS editorial notes as treaty text. FRUS source text is identified by the U.S. Office of the Historian as public domain and reusable; product source policy remains unchanged. See the FRUS reuse FAQ: https://history.state.gov/about/faq/what-is-frus.

Keep implementation in backend/app/evaluation and backend/evaluation_data/versailles-1919/. Do not alter the production turn worker, game prompt, scenario version hash, source catalog, or Local-AI-API-LITE worker runtime in this milestone.
