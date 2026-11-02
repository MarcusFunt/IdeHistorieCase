# Retrieval evaluation design

**Status:** Draft pending approval<br>
**Date:** 2026-10-30<br>
**Repositories:** `IdeHistorieCase` (evaluation) and `Local-AI-API-LITE` (model services)

## Intent

Measure whether historical source retrieval and reranking improve answer quality enough to justify their CPU, memory, and latency cost. Run the same source passages, queries, answer model, and prompt across four retrieval configurations, then compare those answers with oracle-evidence and no-context conditions.

`IdeHistorieCase` currently has an M2 fake turn provider, a narrow inference probe, and 12 Versailles answer goldens. It has no source-passage store, production retriever, or reranker. `Local-AI-API-LITE` currently serves the chat model only. The evaluation will be an offline benchmark over a versioned source corpus, backed by actual embedding/reranking model services added to the LITE repository. It will not claim to measure the fake turn worker.

## Goals

- Add a local evaluation runner and source-grounded answer path in IdeHistorieCase.
- Add CPU-only embedding and reranking services in Local-AI-API-LITE, with real adapters required for acceptance.
- Compare BM25, Qwen embeddings, BM25 + Qwen hybrid retrieval, and hybrid + GTE reranking.
- Use 40 reviewed pilot queries followed by at least 100 held-out Danish/English queries with actual source passages.
- Measure Recall@5, MRR@10, nDCG@10, multi-passage evidence coverage, and paired reranking deltas.
- Make blind human ratings the primary answer-quality and unsupported-claim measure; use an AI judge only as an optional secondary diagnostic.
- Add an oracle-evidence condition to separate retrieval errors from generation errors.
- Separate warm/cold and local/remote timing and resource measurements.

## Non-goals

- Change the production turn worker, scenario publication, database schema, or public API.
- Change the GPU vLLM model, its profile, GPU allocation, stable `ddu-llm` alias, or current dashboard controls.
- Mark source passages or in-game citations as ready for product use.
- Count mocks, canned vectors, stub rerankers, or model-ID-only checks as real-model acceptance.
- Run the live evaluation or start/stop the existing vLLM service during implementation.

## Current constraints and technical basis

- `IMPLEMENTATION_PLAN.md` calls for 30–50 labeled Danish/English questions, Recall@5, MRR, CPU latency, startup time, and RAM. The first 40 are a pilot; final comparisons use a separately authored held-out split of at least 100 cases.
- The current Versailles source policy remains `citation_ready: false`. Evaluation passages live outside the scenario bundle. FRUS is identified by the U.S. Office of the Historian as public domain and reusable; the fixture will retain source attribution, links, dates, and exact section/page anchors, and exclude later editorial notes unless a case tests them. The product source policy remains unchanged. See the [FRUS reuse FAQ](https://history.state.gov/about/faq/what-is-frus).
- Local-AI-API-LITE's main `compose.yaml` is deliberately constrained to one vLLM service. Its dashboard validator also allowlists that service and its fixed commands. Retrieval services therefore use a second optional Compose project in the same repository rather than broadening dashboard lifecycle controls.
- Hugging Face Text Embeddings Inference (TEI) documents CPU images and support for both Qwen3 embeddings and the GTE multilingual reranker; it exposes embedding/reranking APIs and Prometheus metrics. The design uses pinned TEI images and immutable model revisions. See [TEI supported models and CPU images](https://huggingface.co/docs/text-embeddings-inference/en/supported_models), [TEI reranking API](https://huggingface.co/docs/text-embeddings-inference/quick_tour), and [TEI CLI options](https://huggingface.co/docs/text-embeddings-inference/cli_arguments).
- Qwen3-Embedding recommends an instruction-prefixed query and unprefixed documents. The exact historical-retrieval instruction and model revision are pinned in the evaluation manifest. See [Qwen3-Embedding model card](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B).
- Pilot cases may be used for adapter and rubric development. Held-out query labels, prompt, top-k, and model revisions are frozen before comparison and are not used to tune candidate configurations.

## Approaches

1. **Add all retrieval containers to the current LITE Compose project and dashboard allowlist.** Rejected: the dashboard validates a single vLLM service and fixed GPU controls. Widening that boundary would couple retrieval service lifecycle to the GPU model controls.
2. **Add an optional CPU-only retrieval Compose project in the LITE repository, and keep evaluation in IdeHistorieCase.** Selected: it preserves LITE ownership, keeps the GPU chat service unchanged, provides real local model endpoints, and lets the evaluation consume them over the existing private Docker network.
3. **Use arbitrary external model services without extending LITE.** Rejected by the user: real Qwen and GTE services must be part of LITE and working adapters are required for acceptance.

## LITE model-service design

Add `compose.retrieval.yaml` in Local-AI-API-LITE as a separate project named `local-ai-api-lite-retrieval`. It contains two CPU-only TEI containers:

- `embed`: `Qwen/Qwen3-Embedding-0.6B`, pinned model revision and pinned TEI CPU image digest; serves `/embed`.
- `rerank`: `Alibaba-NLP/gte-multilingual-reranker-base`, pinned model revision and pinned TEI CPU image digest; serves `/rerank`.

Both services join the external `ddu_inference_bus` network with stable internal aliases `ddu-embed` and `ddu-rerank`. They have no published host ports and receive no GPU reservation. They use the persistent Hugging Face cache outside the repository. CPU, memory, tokenizer worker, batch, and concurrency limits are explicit environment settings and appear in every evaluation record. The existing vLLM `compose.yaml`, `.env` model profile, dashboard service allowlist, and GPU controls remain unchanged.

Document a dedicated startup/stop/health/metrics runbook for the retrieval project. Its lifecycle is operated separately from the vLLM project and dashboard. The evaluation runner checks TEI `/health` and `/info`, compares the reported model ID and `model_sha` with configuration when `model_sha` is present, calls each actual model endpoint with a real input, validates finite embeddings and rerank scores, and fails on an unexpected identity. If TEI omits `model_sha`, the host wrapper must inspect the running container and verify the pinned image digest, exact model ID, and immutable `--revision` commit passed to the process; it records that evidence with the run. The evaluator's LITE clients use TEI's `/embed`, `/rerank`, `/info`, and `/metrics` contracts; they do not substitute local stubs.

## Evaluation data and retrieval configurations

In IdeHistorieCase, store data under `backend/evaluation_data/versailles-1919/`, outside the published scenario bundle. Use separate pilot, held-out, passage, and relevance-label files plus a manifest. The manifest pins schema and split versions, topic groups, languages, source URLs, exact passage text and hashes, source dates, page/section anchors, temporal availability, graded relevance, required evidence groups, answer criteria, and abstention expectations.

- **Pilot:** 40 Danish/English questions seeded by the existing 12 goldens. Use it for adapter, retrieval, prompt, and rubric development.
- **Held-out:** at least 100 separately authored questions, balanced across Danish and English. Freeze it before final configuration; report it separately from pilot data.
- Every answerable case has exact, source-checked passages and graded relevance labels. Unanswerable cases expect abstention. Multi-fact questions identify distinct required evidence groups; each group may list alternate passages that support the same fact.
- Questions, passages, relevance labels, answer criteria, and temporal access rules remain separate from what the retrieval adapter receives. Dataset status is `draft` until a human checks the passages and labels; draft results are clearly provisional.

Run the same corpus and query through four configurations:

1. **BM25:** deterministic lexical baseline with tokenizer and parameters pinned in the manifest.
2. **Qwen embeddings:** embed the corpus with the real Qwen TEI service, embed each instructed query, and rank the full corpus by exact cosine similarity. Measure one-time corpus embedding/index construction separately from per-query latency.
3. **Hybrid:** fuse BM25 and Qwen rankings with pinned reciprocal-rank fusion parameters.
4. **Hybrid + GTE:** send the fixed top candidate pool from hybrid retrieval to the real GTE TEI `/rerank` endpoint; preserve both the pre-rerank and final ranking.

The evaluation implementation contains the BM25, exact-cosine, and fusion logic. Qwen embedding and GTE reranking are mandatory real LITE services for final acceptance. All configs run through the same passage filters and return exact passage IDs, ranks, and scores. Unknown passages, forbidden temporal access, malformed model responses, or citations outside the supplied passages fail closed.

## Answer conditions and human evaluation

For every held-out case and retrieval configuration, use the same answer model (`ddu-narrator` by default), fixed prompt, decoding settings, language, role, date, and access filters. Pass the exact ranked passages from that configuration to the generator. Add two controls:

- **Oracle evidence:** pass all gold evidence groups directly and bypass retrieval. This estimates answer quality when evidence retrieval is correct.
- **No-context ablation:** provide no source passages to measure model-only answer quality and the effect of adding evidence.

Human evaluation is primary. Export answers with randomized labels that hide retrieval profile, while showing the question criteria, cited IDs, and exact passages supplied to the model. Reviewers score factual correctness, required-point coverage, relevance, temporal consistency, and use of evidence on a fixed rubric. They label each factual claim as supported, contradicted, or unsupported by the passages and record the evidence reference. One reviewer rates all held-out answers; a second reviewer independently rates a randomized 20% sample, and disagreements are flagged for adjudication. A schema-validated ratings file is required before the report emits final human answer-quality and unsupported-claim aggregates.

An AI judge is optional and secondary. It may be compared with human ratings but cannot replace missing ratings or determine acceptance. Report unsupported-by-retrieved-evidence and contradicted-by-evidence separately, including unsupported claims per 100 factual claims and the share of answers with at least one unsupported claim.

## Metrics and run protocol

For each query and retrieval configuration, calculate:

- **Recall@5:** fraction of labeled relevant passage IDs present in the top five.
- **MRR@10:** reciprocal rank of the first relevant passage through rank ten, macro-averaged by query.
- **nDCG@10:** graded ranking quality using the labels in the dataset.
- **Evidence coverage@5 and @10:** fraction of required evidence groups represented in the corresponding top-k results.
- **Reranking improvement:** paired pre/post GTE deltas in Recall@5, MRR@10, nDCG@10, and evidence coverage, plus per-query rank movement and added CPU/RSS/latency.
- **Human answer quality:** every rubric dimension and the unweighted mean for retrieved, oracle, and no-context conditions, with confidence intervals and reviewer agreement.
- **Unsupported historical claims:** human-labeled unsupported and contradicted claims per 100 factual claims, and share of answers with at least one unsupported claim, reported for each condition.

Measure performance separately by execution location and cache state:

- **Local orchestration:** wall time and CPU for BM25, exact search, fusion, serialization, and evaluation client work.
- **Model worker:** client wall time to Qwen/GTE, server time/queue metrics where TEI exposes them, and worker CPU/RSS. Scrape TEI `/metrics`; the host runner also samples the two fixed retrieval containers with Docker stats so CPU/memory remain measurable if a process metric is unavailable. Record the source and sampling interval.
- **Cold:** the operator starts a dedicated retrieval project from a stopped state with model files already in the persistent cache. The host wrapper records service start; the evaluator records health/readiness and first-request times. This is a cold process/model load, not a cold download. The evaluator never starts, stops, or restarts services, vLLM, or the backend.
- **Warm:** after a fixed warmup count, report p50/p95 latency and CPU/RSS per query for embedding, retrieval, reranking, answer generation, and end-to-end.
- **Remote worker mode:** retain client network+queue latency separately from server timing. Obtain worker CPU/RSS from the worker host sampler or its Prometheus `/metrics` endpoint; missing worker CPU/RSS makes the resource comparison incomplete, not zero. Record the source and sampling interval.

Run each configuration as a separate labeled phase so service CPU deltas and peak memory can be attributed to that configuration. Record hardware, container/model revisions, CPU/memory limits, batch size, concurrency, tokenization workers, model cache state, corpus and query-set hashes, prompt versions, and timestamps. Report paired held-out deltas grouped by topic with bootstrap confidence intervals. Put quality and resource changes side by side rather than reducing them to one winner score.

## Runner and artifacts

The evaluation is an explicit CLI in IdeHistorieCase. Run it in a short-lived, resource-limited client container attached to `ddu_inference_bus`, with read-only access to benchmark data and a private writable results directory. The evaluator receives no PostgreSQL connection, Docker socket, or production API port. A fixed host-side wrapper gathers Docker stats and combines them with the run artifact; the dashboard does not add arbitrary or free-form benchmark jobs.

Write query-level JSONL traces, blinded review files, a ratings import file, and Markdown/JSON summaries to a caller-selected local directory ignored by Git. Traces include the exact retrieved passages, answers, rankings, model revisions, and run configuration. Runs compare only when corpus, held-out split, model/container revisions, prompt, and runtime configuration match.

## Acceptance

Acceptance requires all of the following:

- 40 pilot cases and at least 100 bilingual held-out cases validate. Held-out source excerpts, relevance grades, evidence groups, and temporal rules receive independent human review; the dataset is not labeled approved before that review.
- The LITE retrieval Compose project starts both actual TEI models on CPU, without host-published ports or GPU access. The Qwen adapter performs real `/embed` calls; the GTE adapter performs real `/rerank` calls; both verify model identity and validate returned values. Verify revisions from TEI `model_sha` when supplied; otherwise require host-side container evidence of the pinned image and immutable model revision. No mock or stub result can satisfy this requirement.
- BM25, Qwen, hybrid, and hybrid + GTE all produce ranking traces on the same held-out cases. Oracle and no-context conditions use the same answer model and frozen prompt.
- Human ratings are loaded for every held-out output; second-review agreement and adjudication are recorded. AI-judge scores alone cannot pass acceptance.
- Recall@5, MRR@10, nDCG@10, evidence coverage, unsupported-claim rates, and reranking deltas are reported by language and condition.
- Cold/warm and local/remote latency and resource measurements are separated. Missing service metrics, human ratings, adapters, or a human-reviewed dataset are clearly marked incomplete.

## Builder decisions

The builder may choose exact JSON/YAML schemas, CLI flags, percentile and bootstrap implementation, module names, output layout, TEI image digest, immutable model commits, CPU/memory defaults, and the host-side metrics wrapper. Keep the retrieval Compose project separate from the vLLM dashboard-controlled project. Keep the evaluation outside the production turn worker and scenario version hash. Do not add excerpts to the product source catalog or change game citation readiness. The four configurations, real Qwen/GTE adapters, pilot/held-out split, human-primary scoring, oracle condition, and cold/warm/local/remote measurements are fixed requirements.
