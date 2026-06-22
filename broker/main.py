import os
import json
import time
import asyncio

import httpx
import aio_pika
from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator
from prometheus_client import Histogram, Counter

app = FastAPI()
Instrumentator().instrument(app).expose(app)

llm_request_duration = Histogram(
    "llm_request_duration_seconds",
    "LLM request duration via llm-broker",
    ["model"]
)

llm_request_errors = Counter(
    "llm_request_errors_total",
    "Number of failed LLM requests",
    ["model"]
)

RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://guest:guest@rabbitmq.rabbitmq.svc.cluster.local/")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:4b-instruct")
REQUEST_QUEUE = os.getenv("REQUEST_QUEUE", "llm_requests")


def log(event: str, **kwargs):
    print(json.dumps({"event": event, **kwargs}, ensure_ascii=False), flush=True)


async def sync_model_on_disk():
    async with httpx.AsyncClient(timeout=None) as client:
        tags = await client.get(f"{OLLAMA_URL}/api/tags")
        tags.raise_for_status()
        for m in tags.json().get("models", []):
            if m["name"] != OLLAMA_MODEL:
                log("model_delete", model=m["name"])
                await client.delete(f"{OLLAMA_URL}/api/delete", json={"model": m["name"]})

        log("model_pull_start", model=OLLAMA_MODEL)
        r = await client.post(f"{OLLAMA_URL}/api/pull", json={"model": OLLAMA_MODEL, "stream": False})
        r.raise_for_status()
    log("model_pull_done", model=OLLAMA_MODEL)


async def run_inference(prompt: str) -> str:
    async with httpx.AsyncClient(timeout=None) as client:
        r = await client.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
        )
        r.raise_for_status()
        return r.json()["response"]


async def on_request(message: aio_pika.IncomingMessage) -> None:
    async with message.process():
        body = json.loads(message.body)
        prompt = body.pop("prompt", "")
        start = time.monotonic()

        try:
            result = await run_inference(prompt)
            duration = time.monotonic() - start
            llm_request_duration.labels(model=OLLAMA_MODEL).observe(duration)
            reply = {**body, "result": result, "error": None, "model_used": OLLAMA_MODEL, "duration_seconds": duration}
            log("inference_done", request_id=body.get("request_id"), duration_seconds=duration)
        except Exception as e:
            duration = time.monotonic() - start
            llm_request_errors.labels(model=OLLAMA_MODEL).inc()
            reply = {**body, "result": None, "error": str(e), "model_used": OLLAMA_MODEL, "duration_seconds": duration}
            log("inference_error", request_id=body.get("request_id"), error=str(e))

        if not message.reply_to:
            log("missing_reply_to", request_id=body.get("request_id"))
            return

        await rabbitmq_channel.default_exchange.publish(
            aio_pika.Message(
                body=json.dumps(reply).encode(),
                correlation_id=message.correlation_id,
            ),
            routing_key=message.reply_to,
        )


rabbitmq_connection: aio_pika.RobustConnection = None
rabbitmq_channel: aio_pika.Channel = None


async def setup_consumer():
    global rabbitmq_channel
    rabbitmq_channel = await rabbitmq_connection.channel()
    await rabbitmq_channel.set_qos(prefetch_count=1)
    queue = await rabbitmq_channel.declare_queue(REQUEST_QUEUE, durable=True)
    await queue.consume(on_request)
    log("consumer_registered", queue=REQUEST_QUEUE)


@app.get("/health")
def health():
    return {"healthy": True}


@app.on_event("startup")
async def startup():
    global rabbitmq_connection

    await sync_model_on_disk()

    rabbitmq_connection = await aio_pika.connect_robust(RABBITMQ_URL)
    rabbitmq_connection.reconnect_callbacks.add(lambda *_: asyncio.create_task(setup_consumer()))

    await setup_consumer()

    log("startup", rabbitmq_url=RABBITMQ_URL, ollama_url=OLLAMA_URL, model=OLLAMA_MODEL, queue=REQUEST_QUEUE)
