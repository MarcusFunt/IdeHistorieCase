# Historical retrieval evaluation

This evaluation measures whether Qwen dense retrieval and GTE reranking improve discovery of historical source evidence enough to justify their CPU, memory, and latency costs.

## Architecture and model identity

Retrieval workers remain owned by Local-AI-API-LITE. Its inspected `main` commit `87f610041736dc5012f85238c724ed53816e4604` already includes the CPU-only Qwen3-Embedding-0.6B and GTE multilingual reranking workers in `deploy/retrieval/compose.yaml`, attached to `ddu_inference_bus` with aliases `ddu-embed` and `ddu-rerank`. This milestone calls those services directly and adds no TEI or alternative serving runtime.

The IdeHistorieCase clients use:

- `http://ddu-embed:8080/v1/embeddings`, alias `ddu-embed`, task `embedding`, model `Qwen/Qwen3-Embedding-0.6B`, revision `97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`, dimension 1,024, normalized vectors, and query-instruction version `qwen3-question-to-passages-en-v1`.
- `http://ddu-rerank:8080/v1/rerank`, alias `ddu-rerank`, task `rerank`, model `Alibaba-NLP/gte-multilingual-reranker-base`, revision `8215cf04918ba6f7b6a62bb44238ce2953d8831c`, and worker code revision `40ced75c3017eb27626c9d4ea981bde21a2662f4`.

Before real-model retrieval, both adapters require HTTP 200 from `/health` and `/ready`, then require `/v1/models` to serve the expected alias, task, and exact pinned model revision. The runner checks the dataset's Qwen, GTE model, query-instruction, and checked-in GTE code-revision pins. In the inspected LITE worker contract, `/v1/models` exposes the model revision but not the GTE source-code revision; the host wrapper records the worker image ID/reference as deployment provenance. A missing service, an unexpected model, a loading worker, malformed output, or a vector dimension/normalization error stops the run. The worker API supplies token usage and request wall time; it does not expose a TEI `/metrics` route. Do not interpret client request time as server-only inference time. See the pinned LITE [`models.py`](https://github.com/MarcusFunt/Local-AI-API-LITE/blob/87f610041736dc5012f85238c724ed53816e4604/services/retrieval/retrieval_worker/models.py) contract.

## Evaluation data

Data lives in `backend/evaluation_data/versailles-1919/`, outside scenario bundles and production source catalogs. Passage records contain exact excerpts, SHA-256 hashes, source URLs and anchors, document creation dates, publication dates, and actor knowledge availability as distinct fields. The source policy remains `citation_ready: false`.

The initial pilot has 40 bilingual questions. Agent-reviewed grades cover the complete pooled top-ten candidates for these pilot queries; this is a pilot result, not yet a benchmark. The held-out set currently has zero of its 100 target queries. Draft labels are not used in quality metrics; human, agent, and mixed reviewed labels are. The manifest, sources, passages, queries, and judgments are hashed separately. Source review and judgment coverage are reported as provenance; they do not block retrieval runs or require every source/query to be reviewed.

Judgments use grades 0–3 and may name required evidence groups with alternate supporting passages. Graded metrics use the query sample whose labels are marked reviewed and whose available top-ten results are all graded; reports show the sample size and coverage. A high-capability agent with internet access may review pooled passages and its labels count in these same metrics. Record review provenance with `review_method` (`human`, `agent`, or `mixed`) and reviewer IDs. Pooling exports unique top-ten results from all profiles with randomized candidate IDs and hides profile and rank. Unreviewed seed labels are not treated as reviewed. Review packets show query access cutoffs, allowed actor-knowledge passage IDs, source creation/publication dates, and actor-knowledge availability as separate fields. They also include evidence-group IDs and descriptions so reviewers can link alternate supporting passages. Import validates labels and updates dataset hashes and reviewed-query counts while preserving existing evidence-group links. Review only the representative query sample needed for the comparison; exhaustive source or query review is not required.

## Stage 1: retrieval-only acceptance

Stage 1 does not call the narrator or require answer ratings. The offline harness can be exercised with BM25 alone:

```powershell
cd backend
python -m pip install -e .
historical-eval validate --dataset evaluation_data/versailles-1919
historical-eval retrieve --dataset evaluation_data/versailles-1919 --split pilot --profiles bm25 --output evaluation-results/pilot-bm25
historical-eval report --dataset evaluation_data/versailles-1919 --traces evaluation-results/pilot-bm25/traces.jsonl --output evaluation-results/pilot-bm25
```

The four fixed comparison profiles are:

1. `bm25`
2. `dense` — exact cosine ranking over real Qwen vectors
3. `hybrid` — BM25 and Qwen reciprocal-rank fusion with manifest-pinned `rrf_k`
4. `hybrid_rerank` — the same fused list, reranked by the real GTE worker

The runner batches document embeddings in groups of at most eight and limits GTE requests to 20 candidates. It preserves source text, IDs, anchors, component scores, and pre-rerank rankings. It applies simulation access filters before ranking; retrospective QA does not apply actor knowledge cutoffs.

Metrics include Recall@5, MRR@10, graded nDCG@10, required evidence coverage@5/@10, paired pre/post rerank deltas, and per-query rank movement. Summaries group by split, language, topic, and question kind. Latency has per-stage p50/p95 values and separates corpus indexing from per-query work. Metrics are computed only on reviewed, fully graded query pools; other traces remain in retrieval-completeness and latency reports. Their absence from quality aggregates is shown as coverage, not as a failure of the retrieval run.

## Real-model comparison and resource sampling

First configure `deployment/evaluation/.env` from `.env.example`. Point `EVAL_DATA_PATH` to this checkout's `backend/evaluation_data/versailles-1919` and `EVAL_RESULTS_PATH` to its ignored `backend/evaluation-results` directory. Start the existing LITE retrieval workers by following their own runbook; the evaluation wrapper never starts or stops them. Cold-run stop/start actions belong to the operator workflow and are recorded separately from retrieval requests.

Run a warm pilot comparison:

```powershell
./scripts/evaluation-host.ps1 -Split pilot -ExecutionLocation local -CacheState warm
```

For a cold process/model-load observation, the operator starts the existing workers manually from a stopped state with model weights already cached, then runs:

```powershell
./scripts/evaluation-host.ps1 -Split pilot -ExecutionLocation local -CacheState cold -WarmupCount 0
```

Cold means worker process/model loading, not weight download. The wrapper records worker start time, health, image identity, and image reference before the evaluation; the adapter records when `/ready` and `/v1/models` preflight completes. These timestamps let operators distinguish their start-to-ready action from retrieval latency. The client writes timestamped phase events, including warmup, preflight, the first corpus-embedding request, and reranking requests. Existing worker API response timings/token usage and host-side Docker CPU/memory samples are reported separately; the evaluator does not assume a TEI `/metrics` endpoint and does not mount the Docker socket into the client container. Warmup requests, adapter `/health`/`/ready`/`/v1/models` preflight, first model requests, steady-state per-query stages, and one-time corpus embedding batches remain distinct in the report.

Keep local and remote workers, warm and cold runs, model revisions, corpus hashes, and query splits separate. For a remote worker, set its private worker URL in `.env`. The wrapper skips Docker inspection and sampling on the evaluator host when `-ExecutionLocation remote` is selected, so unrelated local containers cannot be attributed to the remote request. If the remote worker has a configured Docker context on the evaluation host, pass `-WorkerDockerContext <context-name>` to sample the existing LITE worker containers through that context. Otherwise the report marks worker CPU/memory unavailable/not applicable. Warmup request durations, service preflight durations, endpoint wall time, local BM25/dense/RRF time, first versus remaining corpus-embedding batches, and steady-state query retrieval remain separate measurements.

Blind relevance pooling commands:

```powershell
historical-eval pool --dataset backend/evaluation_data/versailles-1919 --traces backend/evaluation-results/<run>/traces.jsonl --output backend/evaluation-results/<run>/pool
historical-eval import-judgments --dataset backend/evaluation_data/versailles-1919 --ratings backend/evaluation-results/<run>/pool/pooled_review.jsonl --private-key backend/evaluation-results/<run>/pool/pool_key_private.json --pool-manifest backend/evaluation-results/<run>/pool/pool_manifest.json --reviewer-id <reviewer> --review-method agent
```

The pool file contains exact source passages, attribution, and anchors, with no profile/rank. Keep its private key restricted to the evaluation operator. Review a stratified subset of query pools. The importer records human or agent review provenance; updating the manifest to `reviewed` is optional and does not gate retrieval acceptance.

## Stage 2: blinded answer-quality study

This is a follow-on study, not a Stage 1 acceptance dependency. Once the 100-query held-out set and its retrieval traces exist, the tool selects 25–40 stratified questions from the subset with reviewed oracle-evidence labels. The remaining held-out queries and sources do not need human review. It compares all four retrieval profiles, an oracle selected by the reviewed grades, and a no-context diagnostic. The generation model, prompt, maximum passage count, evidence-token cap, and citation format are held constant across retrieved and oracle answers.

The answer-study client uses the existing chat alias (`ddu-narrator` by default). It counts complete formatted evidence blocks with the served model's `/tokenize` endpoint, then enforces the same evidence-token cap and maximum passage count for each retrieved and oracle condition. The no-context arm is a separate diagnostic and has no supplied evidence. The current LITE deployment must expose the tokenizer route for this optional study; if it does not, the command fails closed rather than estimating token counts. The route and request/response token fields are documented by the [vLLM tokenizer endpoint reference](https://docs.vllm.ai/en/v0.11.0/api/vllm/entrypoints/openai/protocol.html#tokenizecompletionrequest) and [vLLM OpenAI-compatible serving guide](https://docs.vllm.ai/en/stable/serving/online_serving/openai_compatible_server/).

Run the answer study from the evaluation client on `ddu_inference_bus` after a reviewed sample has been prepared:

```powershell
docker compose --env-file deployment/evaluation/.env --file deployment/evaluation/compose.yaml run --rm --entrypoint historical-eval client answer-study --dataset /srv/backend/evaluation_data/versailles-1919 --traces /results/<heldout-run>/traces.jsonl --output /results/answer-study --sample-size 25 --evidence-token-budget 1500 --max-passages 5
```

Distribute only `blind_review_packets.jsonl` and the rating template. Keep `answer_key_private.json` and `answer_run_private.json` with the operator. A blind packet shows the question, answer, simulation actor/time context, actual source passages supplied, and source citations, but does not show the retrieval profile or oracle label. Human reviewers should first rate independently without seeing the key.

For each factual claim, rate these separately:

- `factual_status`: correct, incorrect, uncertain, or not assessed, based on historical evidence independent of what the model was shown.
- `evidence_support`: supported, unsupported, contradicted, or not applicable, based only on the supplied passages.
- `citation_status`: correct, incorrect, missing, or not applicable. A correct citation must identify a supplied passage that supports the claim.

Also rate required-point coverage and relevance from 0–3 and temporal consistency. A no-context claim may be factually correct and unsupported; it is never automatically labeled factually incorrect. Unsupported and contradicted claims are counted separately. Retrospective QA and actor/time simulation remain separate; simulation reviewers see the actor, in-world date, source creation cutoff, publication cutoff, and actor knowledge constraints.

After blind ratings are complete, import and unblind only the aggregate summary:

```powershell
docker compose --env-file deployment/evaluation/.env --file deployment/evaluation/compose.yaml run --rm --entrypoint historical-eval client import-answer-ratings --ratings /results/answer-study/ratings.jsonl --packets /results/answer-study/blind_review_packets.jsonl --private-key /results/answer-study/answer_key_private.json --output /results/answer-study
```

The Markdown/JSON result reports factual accuracy, unsupported claims, contradicted claims, citation correctness, coverage, relevance, temporal inconsistency, future-information leaks, and rating coverage per hidden condition. No AI judge is used as the primary quality measure.

## Acceptance stages

1. **Retrieval harness:** source and pilot validation, deterministic BM25, real-service adapter contracts, traces, ranking metrics, pooling, and report formats. BM25 harness acceptance does not require the narrator.
2. **Real-model retrieval comparison:** verify the existing Qwen/GTE endpoints and pinned identities, then compare all four retrieval profiles with judged retrieval quality, latency, and sampled CPU/memory cost. The current pilot data remains draft; the held-out target is not populated yet.
3. **Answer-quality study:** optional 25–40 question blind human study. Only selected questions need reviewed oracle evidence; the rest of the held-out set and all sources do not require exhaustive human review. Use equal retrieved/oracle evidence budgets and separate factual, support, citation, and temporal ratings.
4. **Optional optimization:** TEI or another retrieval engine can be compared later as an A/B performance experiment under the same corpus and queries. It is outside this implementation milestone.
