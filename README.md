# llm-unit

DaemonSet on k3s, one pod per dedicated inference node (`purpose=ksai`/`purpose=kmai`, see [homelab](https://github.com/DmytroKrynytsyn/homelab)). Each pod is a single tiny container: a Python broker that drains a RabbitMQ request queue, runs inference via a `llama-server` (llama.cpp) subprocess, and replies — decoupling request rate from inference throughput.

git push -> Github Action -> Docker Hub -> ArgoCD -> k3s

## How it works

The `llama-server` binary and the GGUF model file are not built or downloaded by this repo — they're `hostPath` mounts onto files placed on the node ahead of time (binaries are built weekly on the host by `homelab`'s `node-ollama-build` role; the model file is placed manually). On startup the broker launches `llama-server` as a subprocess bound to `127.0.0.1`, waits for it to report healthy, then consumes `llm_requests`, calls `llama-server`'s native `/completion` endpoint, and publishes the result to the `reply_to` queue named in the request message, with the same `correlation_id`. One request at a time per node — `llama-server` serves sequentially.

## Stack

`k3s` · `ArgoCD` · `Helm` · `GitHub Actions` · `Docker Hub` · `FastAPI` · `uv` · `llama.cpp` · `RabbitMQ`

## Bootstrap

```bash
kubectl apply -f https://raw.githubusercontent.com/DmytroKrynytsyn/llm-unit/main/argocd/application.yaml
```

## Verifying a deployment

- `GET /health` and `/metrics` on port 8000 of any pod (broker container).
- Publish a test message to `llm_requests` with a `reply_to` queue set and confirm a reply arrives with `result` populated.
