# Connecting an independent backend to shared inference

The inference deployment runs one llama.cpp model independently of the history backend.
It provides model execution only: applications own their prompts, schemas, game state,
databases, validation, and credentials. A model completion is a proposal, not an
authoritative game-state update.

Create `ddu_inference_bus` and start the inference Compose project as described in
the [M1 runbook](M1_RUNBOOK.md). Attach a trusted application's worker to that
external network and configure its inference URL as `http://ddu-llm:8080`.
Keep its API, database, case data and credentials on its own networks. The shared
Docker bridge is not a strong multi-tenant security boundary: attach only trusted
local applications and do not publish the model port.

## HTTP contract

Send `POST /v1/chat/completions` with a JSON body containing:

- `model`: the server's configured alias (`ddu-narrator` by default).
- `messages`: application-specific role and content messages.
- `response_format`: a `json_schema` object with the required application schema.
- `max_tokens` and `temperature`: values chosen by the calling application.
- `stream: false` for the non-streaming response used by these examples.

The client must check the HTTP result, the completion envelope and `finish_reason`,
then parse and validate the returned JSON against its own schema. Treat truncated,
empty, malformed or schema-invalid responses as failures. Apply an appropriate
timeout; do not log private prompts or responses in operational evidence.

Some chat templates support `chat_template_kwargs: {"enable_thinking": false}`.
This is a per-request model-specific option, not a universal API feature. The
history diagnostic uses it for its short response; other clients should choose
their own settings and verify them with their chosen model.

## Run the independent two-workload check

The standalone client uses only the Python standard library and does not import the
history backend. From the repository root on a Linux Docker host:

```sh
docker run --rm --network ddu_inference_bus \
  --mount "type=bind,src=$PWD/scripts/shared_inference_smoke.py,dst=/work/shared_inference_smoke.py,readonly" \
  -e LLM_BASE_URL=http://ddu-llm:8080 \
  -e LLM_MODEL_ALIAS=ddu-narrator \
  python:3.13-slim python /work/shared_inference_smoke.py --disable-thinking
```

The flag is appropriate for the tested Qwen3.5 starter model. Omit it for a model
whose template does not support that option. Use `--base-url`, `--model`,
`--timeout`, and `--max-tokens` to select a different endpoint and per-request
limits. Add `--concurrent` to send both workloads at once. Output contains only
the workload name, model alias, success/failure, elapsed time and total wall time.
Any failed request makes the command exit nonzero.
The timeout bounds how long the client waits, including DNS resolution; the
server may continue processing a request after the client times out.

With `LLM_PARALLEL=1`, concurrent requests may wait in the server queue; two
successful requests do not prove simultaneous GPU execution. See
[M1.5 host validation](M1_5_VALIDATION.md) for the measured setup and limitations.
