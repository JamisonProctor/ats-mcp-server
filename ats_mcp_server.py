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
import re
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

    # Ollama tags can be "model:tag" or just "model" (defaults to :latest)
    if model in available or f"{model}:latest" in available:
        logger.info(f"Model '{model}' is available.")
        return

    # Check without tag
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
    """Find and read the job description text.

    Priority:
    1. jd.md in the job folder
    2. jd.txt in the job folder
    """
    for name in ("jd.md", "jd.txt"):
        jd_path = job_folder / name
        if jd_path.exists():
            return jd_path.read_text(encoding="utf-8")

    raise FileNotFoundError(
        f"No JD found in '{job_folder}'. Expected jd.md or jd.txt."
    )


def find_resume(job_folder: Path, resume_filename: Optional[str] = None) -> tuple[str, str]:
    """Find and extract text from the resume PDF in the job folder.

    Returns (resume_text, resume_filename).
    """
    if resume_filename:
        resume_path = job_folder / resume_filename
        if not resume_path.exists():
            raise FileNotFoundError(f"Resume not found: {resume_path}")
        return extract_pdf_text(str(resume_path)), resume_filename

    # Auto-detect: look for PDF files that look like resumes
    pdfs = list(job_folder.glob("*.pdf"))
    if not pdfs:
        raise FileNotFoundError(f"No PDF files found in '{job_folder}'.")

    # Prefer files with "Jamison" or "Proctor" in the name
    resume_pdfs = [p for p in pdfs if "jamison" in p.name.lower() or "proctor" in p.name.lower()]
    if resume_pdfs:
        chosen = resume_pdfs[0]
    else:
        # Skip files that look like reports
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

    # Top-level fields
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

    # Requirements 1-8
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
        if req["text"]:  # only add if we got something
            result["requirements"].append(req)

    return result


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
                "Run an ATS (Applicant Tracking System) evaluation of a resume "
                "against a job description using a local LLM via Ollama. "
                "Reads the JD and resume from the job folder, runs the evaluation, "
                "saves the report, and returns structured results."
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
                    "model": {
                        "type": "string",
                        "description": (
                            f"Optional: Ollama model to use. Default: {OLLAMA_MODEL}"
                        ),
                    },
                    "output_version": {
                        "type": "string",
                        "description": (
                            "Optional: version suffix for the report file "
                            "(e.g., 'v2' produces ats_report_v2.txt). "
                            "Default: no suffix (ats_report.txt)."
                        ),
                    },
                },
                "required": ["job_folder_name"],
            },
        ),
        Tool(
            name="list_available_models",
            description="List Ollama models available locally.",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    global _last_activity
    _last_activity = time.monotonic()

    if name == "list_available_models":
        return await _handle_list_models()
    elif name == "run_ats_eval":
        return await _handle_run_ats_eval(arguments)
    else:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]


async def _handle_list_models() -> list[TextContent]:
    """List locally available Ollama models."""
    await _ensure_ollama()
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{OLLAMA_HOST}/api/tags", timeout=10)
        tags = r.json()

    models = []
    for m in tags.get("models", []):
        size_gb = m.get("size", 0) / (1024**3)
        models.append(f"  {m['name']} ({size_gb:.1f} GB)")

    text = "Available Ollama models:\n" + "\n".join(models) if models else "No models installed."
    return [TextContent(type="text", text=text)]


async def _handle_run_ats_eval(arguments: dict) -> list[TextContent]:
    """Run the full ATS evaluation pipeline."""
    job_folder_name = arguments["job_folder_name"]
    resume_filename = arguments.get("resume_filename")
    model = arguments.get("model", OLLAMA_MODEL)
    output_version = arguments.get("output_version", "")

    try:
        # Resolve paths
        workspace = resolve_workspace()
        job_folder = workspace / "jobs" / job_folder_name
        if not job_folder.exists():
            return [TextContent(
                type="text",
                text=f"Error: Job folder not found: {job_folder}",
            )]

        # Read inputs
        logger.info(f"Reading JD from {job_folder} …")
        jd_text = find_jd(job_folder)

        logger.info(f"Reading resume from {job_folder} …")
        resume_text, used_resume = find_resume(job_folder, resume_filename)

        logger.info("Reading ATS eval prompt …")
        ats_prompt_template = read_ats_prompt(workspace)

        # Build the full prompt
        full_prompt = (
            f"{ats_prompt_template}\n\n"
            f"--- JOB_DESCRIPTION ---\n{jd_text}\n\n"
            f"--- RESUME ---\n{resume_text}\n"
        )

        # Ensure Ollama is running and model is available
        await _ensure_ollama()
        await _ensure_model(model)

        # Run the evaluation
        logger.info(f"Running ATS eval with model '{model}' …")
        raw_report = await _generate(full_prompt, model)

        # Parse the report
        parsed = parse_ats_report(raw_report)
        parsed["model_used"] = model
        parsed["resume_used"] = used_resume

        # Save outputs
        suffix = f"_{output_version}" if output_version else ""

        # Save raw text report
        txt_path = job_folder / f"ats_report{suffix}.txt"
        txt_path.write_text(raw_report, encoding="utf-8")

        # Save parsed JSON report
        json_path = job_folder / f"ats_report{suffix}.json"
        json_path.write_text(
            json.dumps(parsed, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        # Build summary for Claude
        rl = parsed.get("rejection_likelihood", "?")
        gaps = parsed.get("top_gaps", "None identified")
        strengths = parsed.get("top_strengths", "None identified")
        flags = parsed.get("screen_out_flags", "None")

        reqs_summary = []
        for r in parsed.get("requirements", []):
            status_icon = {"met": "✓", "partial": "~", "missing": "✗"}.get(
                r.get("status", ""), "?"
            )
            reqs_summary.append(
                f"  {status_icon} {r.get('text', '?')} [{r.get('status', '?')}]"
            )

        summary = (
            f"ATS Evaluation Complete\n"
            f"Job: {parsed.get('company', '?')} — {parsed.get('job_title', '?')}\n"
            f"Model: {model}\n"
            f"Resume: {used_resume}\n"
            f"\n"
            f"Rejection Likelihood: {rl}\n"
            f"\n"
            f"Requirements:\n" + "\n".join(reqs_summary) + "\n"
            f"\n"
            f"Top Gaps: {gaps}\n"
            f"Top Strengths: {strengths}\n"
            f"Screen-out Flags: {flags}\n"
            f"\n"
            f"Reports saved to:\n"
            f"  {txt_path}\n"
            f"  {json_path}\n"
            f"\n"
            f"--- Raw Report ---\n{raw_report}"
        )

        return [TextContent(type="text", text=summary)]

    except Exception as e:
        logger.exception("ATS eval failed")
        return [TextContent(type="text", text=f"Error: {e}")]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def _idle_watchdog():
    """Shut down the server after IDLE_TIMEOUT seconds of inactivity."""
    while True:
        await asyncio.sleep(30)  # check every 30s
        idle = time.monotonic() - _last_activity
        if idle >= IDLE_TIMEOUT:
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
