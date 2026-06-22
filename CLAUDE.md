# llm-unit

DaemonSet running on k3s. One pod per dedicated inference node (`purpose=ksai` or `purpose=kmai`), each pod bundling its own Ollama instance and a small broker that bridges RabbitMQ to it. `homelab` doesn't manage Ollama at all (no host-level install) — this repo owns the full stack for these nodes. Code is reviewed by Codex after the fact, so keep changes simple and easy to diff.

## Code style

- Simple, minimal solutions only. No abstractions, variables, or conditionals for cases that don't exist yet.
- All tunables (model name, queue name, RabbitMQ URL, node labels, image tag) live in `helm/llm-unit/values.yaml` — templates stay generic.

## Message contract

The broker consumes `REQUEST_QUEUE` (`llm_requests` by default). Request body:
```json
{"prompt": "...", "request_id": "...", "chat_id": 123}
```
The AMQP message also carries `reply_to` and `correlation_id` (set by the publisher, e.g. `telegram-bot-on-llm`). The broker runs the prompt through Ollama, then publishes to `reply_to` with the same `correlation_id`:
```json
{"result": "...", "error": null, "request_id": "...", "chat_id": 123, "model_used": "qwen3:4b-instruct", "duration_seconds": 12.3}
```
Any extra fields in the request body besides `prompt` are echoed back unchanged, so other consumers can ride along without the broker knowing about their schema. The message is always acked, even on inference failure (the error goes into the reply body, not a requeue) — don't change this without checking who else publishes to this queue.

## Model handling

`OLLAMA_MODEL` (default `qwen3:4b-instruct`) is a fixed Helm value, not auto-detected — nothing outside this chart pulls a model anymore. On startup the broker lists whatever's cached via `/api/tags`, deletes any model that isn't `OLLAMA_MODEL`, then `POST /api/pull`s the configured one — in that order, so disk is freed *before* the new model downloads (these nodes have as little as 13GB of storage; never assume room for two models at once). This means changing `OLLAMA_MODEL` in `values.yaml` and rolling the DaemonSet is enough to switch models cleanly, no manual cleanup. FastAPI's startup event blocks request handling until this finishes, so `/health` won't go green mid-pull.

## Scheduling and resources

- Node targeting uses `affinity.nodeAffinity` with an `In` match over `Values.nodeSelectorValues`, not a plain `nodeSelector` — there are two label values (`ksai`, `kmai`) to match and `nodeSelector` only does equality.
- No `resources` block on either container by design: each node is dedicated entirely to this DaemonSet, so don't add CPU/memory limits.
- Ollama's model cache is a `hostPath` volume (`Values.ollama.hostModelPath`), not a PVC — DaemonSet pods are node-local, and a pulled model should persist across pod restarts on that same node.

## Verifying changes

- Validate templates render before committing: `helm template helm/llm-unit` (or `helm lint helm/llm-unit`).
- This repo is the source of truth for ArgoCD with `selfHeal` and `prune` enabled — a bad commit on `main` gets applied automatically. Double-check changes before pushing.
