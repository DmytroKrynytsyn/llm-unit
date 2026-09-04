import os
import json
import time
import glob
import socket
import asyncio
import logging
import subprocess

import httpx
import aio_pika
from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator
from prometheus_client import Histogram, Counter


class FilterHealthMetrics:
    def filter(self, record) -> bool:
        msg = record.getMessage()
        return "/health" not in msg and "/metrics" not in msg


logging.getLogger("uvicorn.access").addFilter(FilterHealthMetrics())

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

llm_tokens_total = Counter(
    "llm_tokens_total",
    "LLM tokens consumed",
    ["model", "source", "type"],
)

RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://guest:guest@rabbitmq.rabbitmq.svc.cluster.local/")
# Ordered by priority: REQUEST_QUEUES[0] is drained fully before any later queue is checked.
REQUEST_QUEUES = [q.strip() for q in os.getenv("REQUEST_QUEUES", "llm_requests").split(",") if q.strip()]
RESPONSE_QUEUE = os.getenv("RESPONSE_QUEUE", "llm_responses")
LLAMA_BIN_DIR = os.getenv("LLAMA_BIN_DIR", "/opt/llama-bin")
LLAMA_MODEL_DIR = os.getenv("LLAMA_MODEL_DIR", "/models")
LLAMA_PORT = os.getenv("LLAMA_PORT", "8080")
LLAMA_URL = f"http://127.0.0.1:{LLAMA_PORT}"
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "")
MISTRAL_MODEL = os.getenv("MISTRAL_MODEL", "mistral-small-latest")
MISTRAL_URL = "https://api.mistral.ai/v1"
EXTERNAL_QUEUES = {q.strip() for q in os.getenv("EXTERNAL_QUEUES", "").split(",") if q.strip()}
MODEL_NAME = None


HOSTNAME = socket.gethostname()


def log(event: str, **kwargs):
    print(json.dumps({"event": event, "hostname": HOSTNAME, **kwargs}, ensure_ascii=False), flush=True)


llama_server_process: subprocess.Popen = None


def start_llama_server():
    global llama_server_process, MODEL_NAME
    candidates = glob.glob(os.path.join(LLAMA_MODEL_DIR, "*.gguf"))
    if not candidates:
        raise RuntimeError(f"no .gguf files found in {LLAMA_MODEL_DIR}")
    model_path = max(candidates, key=os.path.getmtime)
    MODEL_NAME = os.path.basename(model_path)

    llama_server_process = subprocess.Popen([
        f"{LLAMA_BIN_DIR}/llama-server",
        "-m", model_path,
        "--host", "127.0.0.1",
        "--port", LLAMA_PORT,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log("llama_server_started", pid=llama_server_process.pid, model=MODEL_NAME)


async def wait_for_llama_server():
    async with httpx.AsyncClient() as client:
        while True:
            try:
                r = await client.get(f"{LLAMA_URL}/health")
                if r.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1)
    log("llama_server_ready")


async def run_inference(prompt: str) -> tuple[str, dict]:
    async with httpx.AsyncClient(timeout=None) as client:
        r = await client.post(f"{LLAMA_URL}/v1/chat/completions", json={
            "messages": [{"role": "user", "content": prompt}],
        })
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"], data.get("usage", {})


async def run_inference_mistral(prompt: str) -> tuple[str, dict]:
    async with httpx.AsyncClient(timeout=None) as client:
        r = await client.post(f"{MISTRAL_URL}/chat/completions",
            headers={"Authorization": f"Bearer {MISTRAL_API_KEY}"},
            json={
                "model": MISTRAL_MODEL,
                "messages": [{"role": "user", "content": prompt}],
            })
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"], data.get("usage", {})


async def on_request(message: aio_pika.IncomingMessage, queue_name: str) -> None:
    async with message.process():
        body = json.loads(message.body)
        prompt = body.pop("prompt", "")
        chunk_num = body.get("chunk_num")
        total_chunk_num = body.get("total_chunk_num")
        if queue_name in EXTERNAL_QUEUES:
            infer = run_inference_mistral
            model_label = MISTRAL_MODEL
            source = "external"
        else:
            infer = run_inference
            model_label = MODEL_NAME
            source = "internal"
        log("request_received", request_id=body.get("request_id"), queue=queue_name, backend=model_label, chunk_num=chunk_num, total_chunk_num=total_chunk_num, body_bytes=len(message.body))
        start = time.monotonic()

        try:
            result, usage = await infer(prompt)
            duration = time.monotonic() - start
            llm_request_duration.labels(model=model_label).observe(duration)
            llm_tokens_total.labels(model=model_label, source=source, type="input").inc(usage.get("prompt_tokens", 0))
            llm_tokens_total.labels(model=model_label, source=source, type="output").inc(usage.get("completion_tokens", 0))
            reply = {**body, "result": result, "error": None, "model_used": model_label, "duration_seconds": duration}
            log("inference_done", request_id=body.get("request_id"), duration_seconds=duration, prompt_tokens=usage.get("prompt_tokens"), completion_tokens=usage.get("completion_tokens"), chunk_num=chunk_num, total_chunk_num=total_chunk_num)
        except httpx.HTTPStatusError as e:
            duration = time.monotonic() - start
            llm_request_errors.labels(model=model_label).inc()
            reply = {**body, "result": None, "error": str(e), "model_used": model_label, "duration_seconds": duration}
            log("inference_error", request_id=body.get("request_id"), error=str(e),
                chunk_num=chunk_num, total_chunk_num=total_chunk_num,
                status_code=e.response.status_code, response_body=e.response.text[:500],
                response_headers=dict(e.response.headers))
        except Exception as e:
            duration = time.monotonic() - start
            llm_request_errors.labels(model=model_label).inc()
            reply = {**body, "result": None, "error": str(e), "model_used": model_label, "duration_seconds": duration}
            log("inference_error", request_id=body.get("request_id"), error=str(e), chunk_num=chunk_num, total_chunk_num=total_chunk_num)

        reply_body = json.dumps(reply).encode()
        await rabbitmq_channel.default_exchange.publish(
            aio_pika.Message(
                body=reply_body,
                correlation_id=message.correlation_id,
            ),
            routing_key=RESPONSE_QUEUE,
        )
        log("response_published", request_id=body.get("request_id"), queue=RESPONSE_QUEUE, body_bytes=len(reply_body))


rabbitmq_connection: aio_pika.RobustConnection = None
rabbitmq_channel: aio_pika.Channel = None
consume_task: asyncio.Task = None


async def consume_loop():
    queues = [
        await rabbitmq_channel.declare_queue(name, durable=True)
        for name in REQUEST_QUEUES
    ]
    while True:
        for queue in queues:
            message = await queue.get(fail=False)
            if message is not None:
                await on_request(message, queue.name)
                break
        else:
            await asyncio.sleep(0.5)


async def setup_consumer():
    global rabbitmq_channel, consume_task
    rabbitmq_channel = await rabbitmq_connection.channel()
    await rabbitmq_channel.declare_queue(RESPONSE_QUEUE, durable=True)
    if consume_task is not None:
        consume_task.cancel()
    consume_task = asyncio.create_task(consume_loop())
    log("consumer_registered", queues=REQUEST_QUEUES, response_queue=RESPONSE_QUEUE)


@app.get("/health")
def health():
    return {"healthy": llama_server_process is not None and llama_server_process.poll() is None}


@app.on_event("startup")
async def startup():
    global rabbitmq_connection

    start_llama_server()
    await wait_for_llama_server()

    rabbitmq_connection = await aio_pika.connect_robust(RABBITMQ_URL)
    rabbitmq_connection.reconnect_callbacks.add(lambda *_: asyncio.create_task(setup_consumer()))

    await setup_consumer()

    log("startup", rabbitmq_url=RABBITMQ_URL, llama_url=LLAMA_URL, model=MODEL_NAME, queues=REQUEST_QUEUES)
