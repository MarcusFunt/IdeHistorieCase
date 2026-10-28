# M1.5 shared-inference host validation

Recorded 28 October 2026 on the target Windows host. The first M1 smoke test
used baseline `f524f5e5694c87b87fe36905dac0e3648c0e806d`; the independent
client, updated history probe and lifecycle checks used implementation commit
`6f4ed140f3f7b7443f628acbd15e3850d07c3d35`. This evidence file is added
after that implementation commit. Baseline GitHub Actions
[passed](https://github.com/MarcusFunt/IdeHistorieCase/actions/runs/40679098644).

## Environment

- Windows 11 build 26200.9457; WSL 2.6.1.0, kernel 6.6.87.2.
- NVIDIA GeForce RTX 3060, 12,288 MiB VRAM, driver 591.86.
- Docker Desktop 4.97.0, Engine 29.9.2, Compose 5.6.1. An NVIDIA-enabled CUDA
  test container successfully ran `nvidia-smi`. A separate NVIDIA Container
  Toolkit version was not exposed in the inspected Docker Desktop or Ubuntu
  environment; GPU passthrough itself was verified.
- One `ghcr.io/ggml-org/llama.cpp:server-cuda` image:
  `ghcr.io/ggml-org/llama.cpp@sha256:d87b02aafa74797a2d599b281c50468704bd0bcdc57bfcf31b570d593f1d285b`.
  Its mutable tag is not pinned by the Compose file.
- Unsloth `Qwen3.5-4B-Q4_K_M.gguf`, revision
  `b0bd07786f94b700012140fd9263b2de83d4f97b`, Q4_K_M,
  SHA-256 `00fe7986ff5f6b463e62455821146049db6f9313603938a70800d1fb69ef11a4`.
  The file hash was checked on the host. Model weights remain outside Git.
- `LLM_CTX_SIZE=4096`, `LLM_GPU_LAYERS=99`, `LLM_PARALLEL=1`.
  The history API used loopback port 18080 because port 8000 was occupied.

## Results

| Check | Observed result |
|---|---|
| Inference-only start | Health returned `{"status":"ok"}` after 20.48 s, measured from Compose start to the first successful polling response. Host file caches may have been warm. |
| History backend and probe | Migration completed, `/healthz` and `/readyz` returned 200, status reported worker running and inference ready. The Danish JSON probe exited 0; a separate timed run took 1.96 s including Docker exec overhead. |
| Standalone back-to-back workloads | History 0.784 s; fictional examiner 0.507 s; combined wall time 1.291 s. Both passed strict response validation. |
| History backend stopped | The same inference container ID, `60983886999f`, remained running. The independent history and fictional examiner requests succeeded in 0.790 s and 0.538 s respectively. |
| History backend restarted | Both health endpoints, status and the real history probe passed again without restarting inference. |
| Two concurrent workloads | History 1.434 s, fictional examiner 0.660 s; total client wall time 1.434 s. Both succeeded. With one configured slot, this does not establish simultaneous model execution. |
| Sampled VRAM during concurrent run | Device-wide maximum 4,307 MiB across 10 samples, with a 200 ms pause plus command overhead between samples. Desktop processes were also present; this is not a process-specific allocation or a guaranteed absolute peak. Before loading, device usage was 1,313 MiB. |
| Inference stopped | Backend remained alive and ready; status became `degraded` after 13.22 s. The standalone client's two requests each timed out after 2.001 s and it exited 1. |
| Inference restarted | The existing worker reported inference `ready` after 22.96 s, measured from the start command and polled every two seconds; the backend was not restarted. |
| Network and ports | One inference container served both workloads. Docker inspection showed no host port bindings for llama.cpp or PostgreSQL. The history API was bound to `127.0.0.1:18080`. |

The synthetic examiner prompt contains no patient information and demonstrates
protocol compatibility only. No clinical accuracy, diagnosis or safety claim
is made. These are single-host, small-sample measurements, not throughput or
p95 benchmarks. No AI-Stack inference workload was running during the test;
GPU contention with it remains unmeasured. Desktop GPU processes were active.

## Reproducing the checks

Use the [M1 runbook](M1_RUNBOOK.md) to configure a compatible model and private
password, validate Compose, create the external network, start inference on its
own, then start the backend. Never commit environment files or model weights.
With the backend running, `bash scripts/m1_smoke.sh` checks its endpoints,
private inference health and the genuine history probe.

Run the standalone commands in [the shared-inference guide](SHARED_INFERENCE.md)
with `--disable-thinking` for this Qwen model. Run once normally and once with
`--concurrent`. Repeat after stopping only the history backend:

```sh
docker compose -f deployment/backend/compose.yaml --env-file deployment/backend/.env stop
# Run the standalone check from the shared-inference guide here.
docker compose -f deployment/backend/compose.yaml --env-file deployment/backend/.env up -d
bash scripts/m1_smoke.sh
```

To observe failure and recovery, stop only inference, wait for the worker poll,
check the backend's three endpoints, and run the standalone client with
`--timeout 2` expecting a nonzero exit. Restart inference and poll status
until it reports ready; do not restart the backend for that recovery check.
Use `nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits` at
regular intervals during the concurrent call for an approximate device-wide
VRAM sample. Inspect Compose/container port bindings to verify the model and
database remain unpublished.
