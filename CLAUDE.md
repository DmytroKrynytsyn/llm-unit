# llm-unit

DaemonSet running on k3s. One pod per dedicated inference node (`purpose=ksai` or `purpose=kmai`), each pod a single tiny container running a Python broker that bridges RabbitMQ to `llama-server` (llama.cpp). The broker spawns `llama-server` itself as a subprocess against binaries and a model file mounted in from the host — it doesn't build or download either. The host-side build (`llama-server`, etc., refreshed weekly under `/opt/ollama-build/latest`) is owned by the `node-ollama-build` role in `homelab`, not this repo. Code is reviewed by Codex after the fact, so keep changes simple and easy to diff.

## Code style

- Simple, minimal solutions only. No abstractions, variables, or conditionals for cases that don't exist yet.
- All tunables (model path, queue name, RabbitMQ URL, node labels, image tag) live in `helm/llm-unit/values.yaml` — templates stay generic.

## Message contract

The broker consumes `REQUEST_QUEUE` (`llm_requests` by default). Request body:
```json
{"prompt": "...", "request_id": "...", "chat_id": 123}
```
The AMQP message also carries `reply_to` and `correlation_id` (set by the publisher, e.g. `telegram-bot-on-llm`). The broker runs the prompt through `llama-server`'s native `/completion` endpoint, then publishes to `reply_to` with the same `correlation_id`:
```json
{"result": "...", "error": null, "request_id": "...", "chat_id": 123, "model_used": "model.gguf", "duration_seconds": 12.3}
```
Any extra fields in the request body besides `prompt` are echoed back unchanged, so other consumers can ride along without the broker knowing about their schema. The message is always acked, even on inference failure (the error goes into the reply body, not a requeue) — don't change this without checking who else publishes to this queue.

## Model handling

There's no in-cluster pull or download. `LLAMA_MODEL_DIR` (mapped from `Values.llamacpp.modelDir`) and `LLAMA_BIN_DIR` (mapped from `Values.llamacpp.binDir`, normally `/opt/ollama-build/latest`) are both `hostPath` directory mounts onto dirs populated on the node by hand. On startup the broker globs `*.gguf` in `LLAMA_MODEL_DIR` and picks the one with the newest mtime — switching models means just dropping a new `.gguf` file into that directory on the node (no edit to `values.yaml`, no DaemonSet roll required; the next pod restart on that node picks it up). The broker then launches `llama-server -m <picked path> --port $LLAMA_PORT` as a subprocess bound to `127.0.0.1` and polls its `/health` endpoint before connecting to RabbitMQ. FastAPI's startup event blocks request handling until that subprocess reports healthy, so `/health` won't go green until the model is loaded. The broker's own `/health` also reflects whether the `llama-server` subprocess is still alive — if it dies, the liveness probe fails and the whole pod (and subprocess with it) restarts.

## Scheduling and resources

- Node targeting uses `affinity.nodeAffinity` with an `In` match over `Values.nodeSelectorValues`, not a plain `nodeSelector` — there are two label values (`ksai`, `kmai`) to match and `nodeSelector` only does equality.
- No `resources` block on the container by design: each node is dedicated entirely to this DaemonSet, so don't add CPU/memory limits.
- Both `hostPath` mounts (`llamacpp.binDir`, `llamacpp.modelHostPath`) are read-only — the broker never writes to them, it only execs the binary and reads the model file.

## Verifying changes

- Validate templates render before committing: `helm template helm/llm-unit` (or `helm lint helm/llm-unit`).
- This repo is the source of truth for ArgoCD with `selfHeal` and `prune` enabled — a bad commit on `main` gets applied automatically. Double-check changes before pushing.
