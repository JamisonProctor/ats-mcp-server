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
IDLE_TIMEOUT = int(os.environ.get("ATS_IDLE_TIMEOUT", "300"))  # seconds before auto-shutdown

logger = logging.getLogger("ats-mcp")
logging.basicConfig(level=logging.INFO, stream=sys.stderr)

# ---------------------------------------------------------------------------
# In-flight job tracking
# ---------------------------------------------------------------------------

_running_jobs: dict[str, dict] = {}  # job_folder_name -> status dict

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


def read_ats_prompt(workspace: Path) -> str:
    """Read the ATS evaluation prompt template."""
    prompt_path = workspace / "prompts" / "ats_eval.md"
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
        "screen_out_flags": None,
        "top_gaps": None,
        "top_strengths": None,
        "rejection_likelihood": None,
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
    result["screen_out_flags"] = line_map.get("SCREEN_OUT_FLAGS")
    result["top_gaps"] = line_map.get("TOP_GAPS")
    result["top_strengths"] = line_map.get("TOP_STRENGTHS")
    result["notes"] = line_map.get("NOTES")

    rl = line_map.get("REJECTION_LIKELIHOOD")
    if rl:
        try:
            result["rejection_likelihood"] = float(rl)
        except ValueError:
            result["rejection_likelihood"] = rl

    for i in range(1, 9):
        req = {
            "text": line_map.get(f"REQ_{i}_TEXT"),
            "status": line_map.get(f"REQ_{i}_STATUS"),
            "evidence": line_map.get(f"REQ_{i}_EVIDENCE"),
            "rationale": line_map.get(f"REQ_{i}_RATIONALE"),
            "confidence": None,
        }
        conf = line_map.get(f"REQ_{i}_CONFIDENCE")
        if conf:
            try:
                req["confidence"] = float(conf)
            except ValueError:
                req["confidence"] = conf
        if req["text"]:
            result["requirements"].append(req)

    return result


# ---------------------------------------------------------------------------
# Background eval task
# ---------------------------------------------------------------------------


async def _run_eval_background(
    job_folder_name: str,
    job_folder: Path,
    workspace: Path,
    resume_filename: Optional[str],
    model: str,
    output_version: str,
) -> None:
    """Run the eval in the background. Saves report to disk regardless of client state."""
    status = _running_jobs[job_folder_name]
    try:
        jd_text = find_jd(job_folder)
        resume_text, used_resume = find_resume(job_folder, resume_filename)
        ats_prompt_template = read_ats_prompt(workspace)

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

        suffix = f"_{output_version}" if output_version else ""

        txt_path = job_folder / f"ats_report{suffix}.txt"
        txt_path.write_text(raw_report, encoding="utf-8")

        json_path = job_folder / f"ats_report{suffix}.json"
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
# MCP Server
# ---------------------------------------------------------------------------

app = Server("ats-evaluator")
_last_activity = time.monotonic()


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
                    "output_version": {
                        "type": "string",
                        "description": (
                            "Optional: version suffix for the report file "
                            "(e.g., 'v2' produces ats_report_v2.txt). "
                            "Default: no suffix."
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
                "Returns 'pending', 'generating', 'complete', or 'error'. "
                "When complete, returns the full parsed report."
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
    global _last_activity
    _last_activity = time.monotonic()

    if name == "run_ats_eval":
        return await _handle_run_ats_eval(arguments)
    elif name == "check_ats_eval":
        return await _handle_check_ats_eval(arguments)
    else:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]


async def _handle_run_ats_eval(arguments: dict) -> list[TextContent]:
    """Kick off the eval in the background and return immediately."""
    job_folder_name = arguments["job_folder_name"]
    resume_filename = arguments.get("resume_filename")
    model = OLLAMA_MODEL
    output_version = arguments.get("output_version", "")

    try:
        workspace = resolve_workspace()
        job_folder = workspace / "jobs" / job_folder_name
        if not job_folder.exists():
            return [TextContent(
                type="text",
                text=f"Error: Job folder not found: {job_folder}",
            )]

        _running_jobs[job_folder_name] = {"status": "pending", "started": time.time()}

        asyncio.create_task(_run_eval_background(
            job_folder_name, job_folder, workspace,
            resume_filename, model, output_version,
        ))

        return [TextContent(
            type="text",
            text=(
                f"ATS evaluation started for '{job_folder_name}' using model '{model}'.\n"
                f"Use check_ats_eval with job_folder_name='{job_folder_name}' to poll for results.\n"
                f"This typically takes 2-5 minutes."
            ),
        )]

    except Exception as e:
        logger.exception("Failed to start ATS eval")
        return [TextContent(type="text", text=f"Error: {e}")]


async def _handle_check_ats_eval(arguments: dict) -> list[TextContent]:
    """Check the status of a background eval."""
    job_folder_name = arguments["job_folder_name"]

    status = _running_jobs.get(job_folder_name)
    if not status:
        return [TextContent(
            type="text",
            text=f"No evaluation found for '{job_folder_name}'. Run run_ats_eval first.",
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
    rl = parsed.get("rejection_likelihood", "?")
    gaps = parsed.get("top_gaps", "None identified")
    strengths = parsed.get("top_strengths", "None identified")
    flags = parsed.get("screen_out_flags", "None")

    reqs_summary = []
    for r in parsed.get("requirements", []):
        icon = {"met": "✓", "partial": "~", "missing": "✗"}.get(r.get("status", ""), "?")
        reqs_summary.append(f"  {icon} {r.get('text', '?')} [{r.get('status', '?')}]")

    summary = (
        f"Status: complete\n\n"
        f"Job: {parsed.get('company', '?')} — {parsed.get('job_title', '?')}\n"
        f"Model: {parsed.get('model_used', '?')}\n"
        f"Resume: {parsed.get('resume_used', '?')}\n\n"
        f"Rejection Likelihood: {rl}\n\n"
        f"Requirements:\n" + "\n".join(reqs_summary) + "\n\n"
        f"Top Gaps: {gaps}\n"
        f"Top Strengths: {strengths}\n"
        f"Screen-out Flags: {flags}\n\n"
        f"Reports saved to:\n"
        f"  {status['txt_path']}\n"
        f"  {status['json_path']}\n\n"
        f"--- Raw Report ---\n{parsed.get('raw_report', '')}"
    )

    return [TextContent(type="text", text=summary)]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def _idle_watchdog():
    """Shut down the server after IDLE_TIMEOUT seconds of inactivity."""
    while True:
        await asyncio.sleep(30)
        idle = time.monotonic() - _last_activity
        # Don't shut down if there are running jobs
        active = any(j["status"] in ("pending", "generating") for j in _running_jobs.values())
        if idle >= IDLE_TIMEOUT and not active:
            logger.info(
                f"No activity for {IDLE_TIMEOUT}s — shutting down. "
                "Claude Desktop will restart the server on next tool call."
            )
            os._exit(0)


async def main():
    async with stdio_server() as (read_stream, write_stream):
        asyncio.create_task(_idle_watchdog())
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
