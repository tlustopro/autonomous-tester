"""
FastAPI server — REST API + SSE streaming for the QA agent.

Endpoints:
  POST /runs              — start a new test run (returns SSE stream)
  GET  /runs              — list all runs (history)
  GET  /runs/{id}         — get run detail + steps
  GET  /screenshots/{file} — serve screenshot images
  GET  /health            — healthcheck
"""
import asyncio
import json
import os
from pathlib import Path

# Load .env if present (local development)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db
from agent import run_test

db.init_db()

SCREENSHOTS_DIR = Path(os.getenv("SCREENSHOTS_DIR", "screenshots"))

app = FastAPI(title="QA Agent API", version="2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class RunRequest(BaseModel):
    scenario: str
    base_url: str


# ── Run execution ────────────────────────────────────────────────────────────

@app.post("/runs")
async def start_run(req: RunRequest):
    """
    Start a test run and stream events as Server-Sent Events.

    Event types:
      { "type": "step",  "data": { step, tool, input, result, screenshot } }
      { "type": "done",  "data": { run_id, passed, summary, failures, screenshots } }
      { "type": "error", "data": { run_id, message } }
    """
    queue: asyncio.Queue = asyncio.Queue()

    async def on_step(step: dict):
        await queue.put({"type": "step", "data": step})

    async def run_and_signal():
        try:
            result = await run_test(req.scenario, req.base_url, on_step=on_step)
            await queue.put({"type": "done", "data": result})
        except Exception as e:
            await queue.put({"type": "error", "data": {"message": str(e)}})
        finally:
            await queue.put(None)

    async def event_stream():
        asyncio.create_task(run_and_signal())
        while True:
            event = await queue.get()
            if event is None:
                break
            yield f"data: {json.dumps(event, default=str)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── History ──────────────────────────────────────────────────────────────────

@app.get("/runs")
def list_runs(limit: int = 50):
    return db.list_runs(limit)


@app.get("/runs/{run_id}")
def get_run(run_id: int):
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    steps = db.get_steps(run_id)
    return {**run, "steps": steps}


# ── Screenshots ───────────────────────────────────────────────────────────────

@app.get("/screenshots/{filename}")
def get_screenshot(filename: str):
    path = SCREENSHOTS_DIR / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "Screenshot not found")
    return FileResponse(str(path), media_type="image/png")


# ── Healthcheck ───────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


# ── Frontend ──────────────────────────────────────────────────────────────────

@app.get("/")
def frontend():
    return FileResponse(str(Path(__file__).parent / "index.html"))
