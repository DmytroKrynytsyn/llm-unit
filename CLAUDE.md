# llm-unit

DaemonSet running on k3s. One pod per dedicated inference node (`purpose=ksai` or `purpose=kmai`), each pod a single tiny container running a Python broker that bridges RabbitMQ to `llama-server` (llama.cpp). The broker spawns `llama-server` itself as a subprocess against binaries and a model file mounted in from the host — it doesn't build or download either. The host-side build (`llama-server`, etc., refreshed weekly under `/opt/ollama-build/latest`) is owned by the `node-ollama-build` role in `homelab`, not this repo. Code is reviewed by Codex after the fact, so keep changes simple and easy to diff.

## Code style

- Simple, minimal solutions only. No abstractions, variables, or conditionals for cases that don't exist yet.
- All tunables (model path, queue name, RabbitMQ URL, node labels, image tag) live in `helm/llm-unit/values.yaml` — templates stay generic.

## Message contract

The broker consumes `REQUEST_QUEUES`, a comma-separated ordered list (per-pool in `values.yaml`, e.g. `llm_requests_sai,llm_requests_mai`). Priority is positional: the broker polls `REQUEST_QUEUES[0]` and only checks the next queue in the list once it's empty, so an earlier queue is always fully drained before a later one is touched. Processing is strictly sequential — one message at a time, no concurrent consumers — since `llama-server` itself only serves one request at a time anyway. Request body:
```json
{"prompt": "...", "request_id": "...", "chat_id": 123}
```
The broker runs the prompt through `llama-server`'s OpenAI-compatible `/v1/chat/completions` endpoint (so the model's own chat template gets applied, regardless of which `.gguf` is loaded), then publishes to the single shared `RESPONSE_QUEUE` (`llm_responses`) with the same `correlation_id`, regardless of which request queue it came from or any `reply_to` the publisher set:
```json
{"result": "...", "error": null, "request_id": "...", "chat_id": 123, "model_used": "model.gguf", "duration_seconds": 12.3}
```
Any extra fields in the request body besides `prompt` are echoed back unchanged, so other consumers can ride along without the broker knowing about their schema. The message is always acked, even on inference failure (the error goes into the reply body, not a requeue) — don't change this without checking who else publishes to these queues or consumes `llm_responses`.

## Model handling

There's no in-cluster pull or download. `LLAMA_MODEL_DIR` (mapped from `Values.llamacpp.modelDir`) and `LLAMA_BIN_DIR` (mapped from `Values.llamacpp.binDir`, normally `/opt/ollama-build/latest`) are both `hostPath` directory mounts onto dirs populated on the node by hand. On startup the broker globs `*.gguf` in `LLAMA_MODEL_DIR` and picks the one with the newest mtime — switching models means just dropping a new `.gguf` file into that directory on the node (no edit to `values.yaml`, no DaemonSet roll required; the next pod restart on that node picks it up). The broker then launches `llama-server -m <picked path> --port $LLAMA_PORT` as a subprocess bound to `127.0.0.1` and polls its `/health` endpoint before connecting to RabbitMQ. FastAPI's startup event blocks request handling until that subprocess reports healthy, so `/health` won't go green until the model is loaded. The broker's own `/health` also reflects whether the `llama-server` subprocess is still alive — if it dies, the liveness probe fails and the whole pod (and subprocess with it) restarts.

## Scheduling and resources

- One DaemonSet per pool, rendered by ranging over `Values.pools` (each entry: `name` — the `purpose` label value — and `queues` — its ordered `REQUEST_QUEUES` list). Adding a pool or changing a pool's queue list is a `values.yaml`-only change.
- No `resources` block on the container by design: each node is dedicated entirely to this DaemonSet, so don't add CPU/memory limits.
- Both `hostPath` mounts (`llamacpp.binDir`, `llamacpp.modelHostPath`) are read-only — the broker never writes to them, it only execs the binary and reads the model file.

## Verifying changes

- Validate templates render before committing: `helm template helm/llm-unit` (or `helm lint helm/llm-unit`).
- This repo is the source of truth for ArgoCD with `selfHeal` and `prune` enabled — a bad commit on `main` gets applied automatically. Double-check changes before pushing.
