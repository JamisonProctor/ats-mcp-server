#!/usr/bin/env python3
"""
ATS Evaluation MCP Server

An MCP server that exposes ATS (Applicant Tracking System) evaluation
as a tool callable from Claude Desktop / Cowork. Uses Ollama for local
LLM inference to evaluate a resume against a job description.

Usage:
    Registered in Claude Desktop's MCP config. Runs on the host machine
    alongside Ollama.
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3.5:9b")
WORKSPACE = os.environ.get(
    "ATS_WORKSPACE",
    str(Path.home() / "Desktop" / "Job Search 2026"),
)

OLLAMA_TIMEOUT = 600  # seconds — large models can be slow on first load
OLLAMA_START_TIMEOUT = 30  # seconds to wait for ollama serve to come up

logger = logging.getLogger("ats-mcp")
logging.basicConfig(level=logging.INFO, stream=sys.stderr)

# ---------------------------------------------------------------------------
# In-flight job tracking + sequential queue
# ---------------------------------------------------------------------------

_running_jobs: dict[str, dict] = {}  # job_key -> status dict
_job_queue: asyncio.Queue | None = None
_queue_order: list[str] = []  # ordered list of queued job keys


def _sanitize_model_name(model: str) -> str:
    """Sanitize a model name for use in filenames (e.g. 'qwen3.5:9b' → 'qwen3.5-9b')."""
    return model.replace(":", "-").replace("/", "-")


def _job_key(folder_name: str, model: str = "") -> str:
    """Build a unique job key from folder name + model."""
    return f"{folder_name}::{model}" if model else folder_name

# ---------------------------------------------------------------------------
# Ollama helpers
# ---------------------------------------------------------------------------


async def _ollama_is_running() -> bool:
    """Check if the Ollama API is reachable."""
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
            return r.status_code == 200
    except (httpx.ConnectError, httpx.TimeoutException):
        return False


async def _ensure_ollama() -> None:
    """Start Ollama if it isn't already running."""
    if await _ollama_is_running():
        logger.info("Ollama is already running.")
        return

    logger.info("Ollama not running — starting 'ollama serve' …")
    subprocess.Popen(
        ["ollama", "serve"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    deadline = time.time() + OLLAMA_START_TIMEOUT
    while time.time() < deadline:
        if await _ollama_is_running():
            logger.info("Ollama is now running.")
            return
        await asyncio.sleep(1)

    raise RuntimeError(
        f"Ollama did not start within {OLLAMA_START_TIMEOUT}s. "
        "Please start it manually with 'ollama serve'."
    )


async def _ensure_model(model: str) -> None:
    """Pull the model if it isn't available locally."""
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{OLLAMA_HOST}/api/tags", timeout=10)
        tags = r.json()
        available = {m["name"] for m in tags.get("models", [])}

    if model in available or f"{model}:latest" in available:
        logger.info(f"Model '{model}' is available.")
        return

    base = model.split(":")[0]
    if any(m.startswith(base) for m in available):
        logger.info(f"Model '{model}' variant found.")
        return

    logger.info(f"Pulling model '{model}' — this may take a while …")
    async with httpx.AsyncClient(timeout=httpx.Timeout(600)) as client:
        r = await client.post(
            f"{OLLAMA_HOST}/api/pull",
            json={"name": model, "stream": False},
            timeout=httpx.Timeout(600),
        )
        if r.status_code != 200:
            raise RuntimeError(f"Failed to pull model '{model}': {r.text}")
    logger.info(f"Model '{model}' pulled successfully.")



async def _generate(prompt: str, model: str) -> str:
    """Send a generate request to Ollama and return the response text."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(OLLAMA_TIMEOUT)) as client:
        r = await client.post(
            f"{OLLAMA_HOST}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "think": False,
                "options": {
                    "temperature": 0,
                    "num_predict": 4096,
                },
            },
        )
        if r.status_code != 200:
            raise RuntimeError(f"Ollama generate failed: {r.text}")
        return r.json()["response"]


# ---------------------------------------------------------------------------
# PDF text extraction
# ---------------------------------------------------------------------------


def extract_pdf_text(pdf_path: str) -> str:
    """Extract text from a PDF file using pymupdf."""
    import fitz  # pymupdf

    doc = fitz.open(pdf_path)
    pages = []
    for page in doc:
        pages.append(page.get_text())
    doc.close()
    return "\n".join(pages)


# ---------------------------------------------------------------------------
# File resolution helpers
# ---------------------------------------------------------------------------


def resolve_workspace() -> Path:
    """Return the workspace root as a Path."""
    ws = Path(WORKSPACE)
    if not ws.exists():
        raise FileNotFoundError(
            f"Workspace not found at '{ws}'. "
            "Set the ATS_WORKSPACE environment variable to the correct path."
        )
    return ws


def find_jd(job_folder: Path) -> str:
    """Find and read the job description text."""
    for name in ("jd.md", "jd.txt"):
        jd_path = job_folder / name
        if jd_path.exists():
            return jd_path.read_text(encoding="utf-8")

    raise FileNotFoundError(
        f"No JD found in '{job_folder}'. Expected jd.md or jd.txt."
    )


def find_resume(job_folder: Path, resume_filename: Optional[str] = None) -> tuple[str, str]:
    """Find and extract text from the resume PDF in the job folder."""
    if resume_filename:
        resume_path = job_folder / resume_filename
        if not resume_path.exists():
            raise FileNotFoundError(f"Resume not found: {resume_path}")
        return extract_pdf_text(str(resume_path)), resume_filename

    pdfs = list(job_folder.glob("*.pdf"))
    if not pdfs:
        raise FileNotFoundError(f"No PDF files found in '{job_folder}'.")

    resume_pdfs = [p for p in pdfs if "jamison" in p.name.lower() or "proctor" in p.name.lower()]
    if resume_pdfs:
        chosen = resume_pdfs[0]
    else:
        non_report = [p for p in pdfs if "report" not in p.name.lower()]
        chosen = non_report[0] if non_report else pdfs[0]

    return extract_pdf_text(str(chosen)), chosen.name


def read_ats_prompt() -> str:
    """Read the ATS evaluation prompt template from the repo."""
    prompt_path = Path(__file__).parent / "prompts" / "ats_eval.md"
    if not prompt_path.exists():
        raise FileNotFoundError(f"ATS eval prompt not found at '{prompt_path}'.")
    return prompt_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# ATS report parsing
# ---------------------------------------------------------------------------


def parse_ats_report(raw_text: str) -> dict:
    """Parse the structured plain-text ATS report into a dict."""
    result = {
        "raw_report": raw_text,
        "company": None,
        "job_title": None,
        "location": None,
        "work_mode": None,
        "employment_type": None,
        "requirements": [],
        "top_gaps": None,
        "top_strengths": None,
        "notes": None,
    }

    lines = raw_text.strip().splitlines()
    line_map = {}
    for line in lines:
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            if value.lower() == "null":
                value = None
            line_map[key] = value

    result["company"] = line_map.get("COMPANY")
    result["job_title"] = line_map.get("JOB_TITLE")
    result["location"] = line_map.get("LOCATION")
    result["work_mode"] = line_map.get("WORK_MODE")
    result["employment_type"] = line_map.get("EMPLOYMENT_TYPE")
    result["top_gaps"] = line_map.get("TOP_GAPS")
    result["top_strengths"] = line_map.get("TOP_STRENGTHS")
    result["notes"] = line_map.get("NOTES")

    # Parse REQ_COUNT if present, otherwise scan up to 10
    try:
        req_count = int(line_map.get("REQ_COUNT", "10"))
    except ValueError:
        req_count = 10

    _valid_statuses = {"met", "partial", "missing"}

    for i in range(1, req_count + 1):
        raw_status = line_map.get(f"REQ_{i}_STATUS")
        status = raw_status if raw_status in _valid_statuses else None
        req = {
            "req_num": i,
            "text": line_map.get(f"REQ_{i}_TEXT"),
            "status": status,
            "status_raw": raw_status if raw_status != status else None,
            "evidence": line_map.get(f"REQ_{i}_EVIDENCE"),
            "rationale": line_map.get(f"REQ_{i}_RATIONALE"),
            "fit_score": None,
        }
        raw_score = line_map.get(f"REQ_{i}_FIT_SCORE")
        if raw_score is not None:
            try:
                score = int(raw_score)
            except ValueError:
                try:
                    score = int(float(raw_score))
                except ValueError:
                    score = 0
            req["fit_score"] = max(0, min(4, score))  # clamp to 0-4
        if req["text"]:
            result["requirements"].append(req)

    return result


# ---------------------------------------------------------------------------
# Deterministic scoring
# ---------------------------------------------------------------------------


def compute_fit_score(parsed: dict) -> dict:
    """Compute overall fit score from per-requirement fit scores."""
    reqs = parsed["requirements"]
    if not reqs:
        return {"overall_fit_pct": 0.0, "total_score": 0, "max_possible": 0}

    total = sum(r.get("fit_score") or 0 for r in reqs)
    max_possible = len(reqs) * 4  # 0-4 scale
    overall = round((total / max_possible) * 100, 1)

    return {
        "overall_fit_pct": overall,
        "total_score": total,
        "max_possible": max_possible,
    }


# ---------------------------------------------------------------------------
# Background eval task
# ---------------------------------------------------------------------------


async def _run_eval_background(
    job_key: str,
    job_folder: Path,
    workspace: Path,
    resume_filename: Optional[str],
    model: str,
) -> None:
    """Run the eval in the background. Saves report to disk regardless of client state."""
    status = _running_jobs[job_key]
    try:
        jd_text = find_jd(job_folder)
        resume_text, used_resume = find_resume(job_folder, resume_filename)
        ats_prompt_template = read_ats_prompt()

        full_prompt = (
            f"{ats_prompt_template}\n\n"
            f"--- JOB_DESCRIPTION ---\n{jd_text}\n\n"
            f"--- RESUME ---\n{resume_text}\n"
        )

        await _ensure_ollama()
        await _ensure_model(model)

        logger.info(f"Running ATS eval with model '{model}' …")
        status["status"] = "generating"
        raw_report = await _generate(full_prompt, model)

        parsed = parse_ats_report(raw_report)
        parsed["model_used"] = model
        parsed["resume_used"] = used_resume

        # Compute deterministic overall fit score
        fit = compute_fit_score(parsed)
        parsed.update(fit)

        model_slug = _sanitize_model_name(model)

        txt_path = job_folder / f"ats_report_{model_slug}.txt"
        txt_path.write_text(raw_report, encoding="utf-8")

        json_path = job_folder / f"ats_report_{model_slug}.json"
        json_path.write_text(
            json.dumps(parsed, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        status["status"] = "complete"
        status["txt_path"] = str(txt_path)
        status["json_path"] = str(json_path)
        status["parsed"] = parsed
        logger.info(f"ATS eval complete — saved to {txt_path}")

    except Exception as e:
        logger.exception("ATS eval failed")
        status["status"] = "error"
        status["error"] = str(e)


# ---------------------------------------------------------------------------
# Sequential job queue worker
# ---------------------------------------------------------------------------


async def _queue_worker():
    """Single worker that processes eval jobs sequentially (GPU constraint)."""
    global _job_queue
    while True:
        job_args = await _job_queue.get()
        job_key = job_args[0]
        try:
            await _run_eval_background(*job_args)
        except Exception:
            logger.exception(f"Queue worker: job '{job_key}' failed unexpectedly")
        finally:
            # Remove from queue order tracking
            if job_key in _queue_order:
                _queue_order.remove(job_key)
            _job_queue.task_done()


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

app = Server("ats-evaluator")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="run_ats_eval",
            description=(
                "Start an ATS evaluation of a resume against a job description. "
                "Returns immediately — the eval runs in the background. "
                "Use check_ats_eval to poll for results."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "job_folder_name": {
                        "type": "string",
                        "description": (
                            "Name of the job folder under jobs/ "
                            "(e.g., 'deeploi__head_of_product_product_lead')"
                        ),
                    },
                    "resume_filename": {
                        "type": "string",
                        "description": (
                            "Optional: specific resume PDF filename in the job folder. "
                            "If omitted, auto-detects the resume."
                        ),
                    },
                },
                "required": ["job_folder_name"],
            },
        ),
        Tool(
            name="check_ats_eval",
            description=(
                "Check the status of a running ATS evaluation. "
                "Returns 'queued', 'pending', 'generating', 'complete', or 'error'. "
                "When complete, returns the full parsed report with fit scores."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "job_folder_name": {
                        "type": "string",
                        "description": "The job folder name passed to run_ats_eval.",
                    },
                },
                "required": ["job_folder_name"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "run_ats_eval":
        return await _handle_run_ats_eval(arguments)
    elif name == "check_ats_eval":
        return await _handle_check_ats_eval(arguments)
    else:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]


async def _handle_run_ats_eval(arguments: dict) -> list[TextContent]:
    """Kick off the eval via the sequential queue and return immediately."""
    global _job_queue

    job_folder_name = arguments["job_folder_name"]
    resume_filename = arguments.get("resume_filename")
    try:
        workspace = resolve_workspace()
        job_folder = workspace / "jobs" / job_folder_name
        if not job_folder.exists():
            return [TextContent(
                type="text",
                text=f"Error: Job folder not found: {job_folder}",
            )]

        key = _job_key(job_folder_name, OLLAMA_MODEL)
        _running_jobs[key] = {"status": "queued", "started": time.time()}
        _queue_order.append(key)

        await _job_queue.put((
            key, job_folder, workspace,
            resume_filename, OLLAMA_MODEL,
        ))

        position = len(_queue_order)
        queue_msg = ""
        if position > 1:
            queue_msg = f" Queue position: {position} of {position}."

        return [TextContent(
            type="text",
            text=(
                f"ATS evaluation queued for '{job_folder_name}' using model '{OLLAMA_MODEL}'.{queue_msg}\n"
                f"Use check_ats_eval with job_folder_name='{job_folder_name}'"
                f" to poll for results.\n"
                f"This typically takes 2-5 minutes per eval."
            ),
        )]

    except Exception as e:
        logger.exception("Failed to start ATS eval")
        return [TextContent(type="text", text=f"Error: {e}")]


async def _handle_check_ats_eval(arguments: dict) -> list[TextContent]:
    """Check the status of a background eval."""
    job_folder_name = arguments["job_folder_name"]
    key = _job_key(job_folder_name, OLLAMA_MODEL)

    status = _running_jobs.get(key)
    if not status:
        return [TextContent(
            type="text",
            text=f"No evaluation found for '{key}'. Run run_ats_eval first.",
        )]

    if status["status"] == "queued":
        elapsed = int(time.time() - status["started"])
        position = _queue_order.index(key) + 1 if key in _queue_order else "?"
        total = len(_queue_order)
        return [TextContent(
            type="text",
            text=f"Status: queued (position {position} of {total}, {elapsed}s elapsed). Try again in 60s.",
        )]

    if status["status"] == "pending":
        elapsed = int(time.time() - status["started"])
        return [TextContent(
            type="text",
            text=f"Status: pending (preparing inputs, {elapsed}s elapsed). Try again in 30s.",
        )]

    if status["status"] == "generating":
        elapsed = int(time.time() - status["started"])
        return [TextContent(
            type="text",
            text=f"Status: generating ({elapsed}s elapsed). Ollama is still working. Try again in 60s.",
        )]

    if status["status"] == "error":
        return [TextContent(
            type="text",
            text=f"Status: error\n{status['error']}",
        )]

    # Complete
    parsed = status["parsed"]
    overall_fit = parsed.get("overall_fit_pct", "?")
    total_score = parsed.get("total_score", "?")
    max_possible = parsed.get("max_possible", "?")
    gaps = parsed.get("top_gaps", "None identified")
    strengths = parsed.get("top_strengths", "None identified")

    reqs_summary = []
    for r in parsed.get("requirements", []):
        icon = {"met": "✓", "partial": "~", "missing": "✗"}.get(r.get("status", ""), "?")
        score = r.get("fit_score", "?")
        req_num = r.get("req_num", "?")
        reqs_summary.append(f"  {icon} REQ_{req_num} [{score}/4] {r.get('text', '?')} [{r.get('status', '?')}]")

    summary = (
        f"Status: complete\n\n"
        f"Job: {parsed.get('company', '?')} — {parsed.get('job_title', '?')}\n"
        f"Model: {parsed.get('model_used', '?')}\n"
        f"Resume: {parsed.get('resume_used', '?')}\n\n"
        f"Overall Fit: {overall_fit}% ({total_score}/{max_possible})\n\n"
        f"Requirements:\n" + "\n".join(reqs_summary) + "\n\n"
        f"Top Gaps: {gaps}\n"
        f"Top Strengths: {strengths}\n\n"
        f"Reports saved to:\n"
        f"  {status['txt_path']}\n"
        f"  {status['json_path']}\n\n"
        f"--- Raw Report ---\n{parsed.get('raw_report', '')}"
    )

    return [TextContent(type="text", text=summary)]



# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main():
    global _job_queue
    _job_queue = asyncio.Queue()
    asyncio.create_task(_queue_worker())

    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
