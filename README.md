# llm-unit

DaemonSet on k3s, one pod per dedicated inference node (`purpose=ksai`/`purpose=kmai`, see [homelab](https://github.com/DmytroKrynytsyn/homelab)). Each pod runs its own Ollama plus a small broker that drains a RabbitMQ request queue, runs inference, and replies — decoupling request rate from inference throughput.

git push -> Github Action -> Docker Hub -> ArgoCD -> k3s

## How it works

The broker consumes `llm_requests`, calls the in-pod Ollama over `localhost:11434`, and publishes the result to the `reply_to` queue named in the request message, with the same `correlation_id`. On startup the broker pulls the configured model (default `qwen3:4b-instruct`) if it isn't already cached on that node — a no-op after the first run. One request at a time per node — Ollama serves sequentially.

## Stack

`k3s` · `ArgoCD` · `Helm` · `GitHub Actions` · `Docker Hub` · `FastAPI` · `uv` · `Ollama` · `RabbitMQ`

## Bootstrap

```bash
kubectl apply -f https://raw.githubusercontent.com/DmytroKrynytsyn/llm-unit/main/argocd/application.yaml
```

## Verifying a deployment

- `GET /health` and `/metrics` on port 8000 of any pod (broker container).
- Publish a test message to `llm_requests` with a `reply_to` queue set and confirm a reply arrives with `result` populated.
