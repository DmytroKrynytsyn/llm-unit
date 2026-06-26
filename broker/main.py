import os
import json
import time
import glob
import asyncio
import subprocess

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
# Ordered by priority: REQUEST_QUEUES[0] is drained fully before any later queue is checked.
REQUEST_QUEUES = [q.strip() for q in os.getenv("REQUEST_QUEUES", "llm_requests").split(",") if q.strip()]
RESPONSE_QUEUE = os.getenv("RESPONSE_QUEUE", "llm_responses")
LLAMA_BIN_DIR = os.getenv("LLAMA_BIN_DIR", "/opt/llama-bin")
LLAMA_MODEL_DIR = os.getenv("LLAMA_MODEL_DIR", "/models")
LLAMA_PORT = os.getenv("LLAMA_PORT", "8080")
LLAMA_URL = f"http://127.0.0.1:{LLAMA_PORT}"
MODEL_NAME = None


def log(event: str, **kwargs):
    print(json.dumps({"event": event, **kwargs}, ensure_ascii=False), flush=True)


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
    ])
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


async def run_inference(prompt: str) -> str:
    async with httpx.AsyncClient(timeout=None) as client:
        r = await client.post(f"{LLAMA_URL}/completion", json={"prompt": prompt})
        r.raise_for_status()
        return r.json()["content"]


async def on_request(message: aio_pika.IncomingMessage, queue_name: str) -> None:
    async with message.process():
        body = json.loads(message.body)
        prompt = body.pop("prompt", "")
        log("request_received", request_id=body.get("request_id"), queue=queue_name)
        start = time.monotonic()

        try:
            result = await run_inference(prompt)
            duration = time.monotonic() - start
            llm_request_duration.labels(model=MODEL_NAME).observe(duration)
            reply = {**body, "result": result, "error": None, "model_used": MODEL_NAME, "duration_seconds": duration}
            log("inference_done", request_id=body.get("request_id"), duration_seconds=duration)
        except Exception as e:
            duration = time.monotonic() - start
            llm_request_errors.labels(model=MODEL_NAME).inc()
            reply = {**body, "result": None, "error": str(e), "model_used": MODEL_NAME, "duration_seconds": duration}
            log("inference_error", request_id=body.get("request_id"), error=str(e))

        await rabbitmq_channel.default_exchange.publish(
            aio_pika.Message(
                body=json.dumps(reply).encode(),
                correlation_id=message.correlation_id,
            ),
            routing_key=RESPONSE_QUEUE,
        )


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
