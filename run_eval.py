#!/usr/bin/env python3
"""
CLI wrapper for the ATS evaluation — reuses the MCP server's core logic directly.

Usage:
    python3 run_eval.py <job_folder_name> [v2]

Examples:
    python3 run_eval.py finapi__product_manager_open_banking_payments
    python3 run_eval.py finapi__product_manager_open_banking_payments v2
"""

import asyncio
import json
import logging
import sys
import time
from pathlib import Path

# Import everything from the MCP server
from ats_mcp_server import (
    OLLAMA_MODEL,
    find_jd,
    find_resume,
    read_ats_prompt,
    resolve_workspace,
    parse_ats_report,
    compute_fit_score,
    _ensure_ollama,
    _ensure_model,
    _generate,
)

logger = logging.getLogger("ats-cli")


async def run(job_folder_name: str, output_version: str = "") -> None:
    workspace = resolve_workspace()
    job_folder = workspace / "jobs" / job_folder_name

    if not job_folder.exists():
        logger.error(f"Job folder not found: {job_folder}")
        sys.exit(1)

    # Resolve inputs
    logger.info(f"Job folder: {job_folder}")
    jd_text = find_jd(job_folder)
    logger.info(f"JD loaded ({len(jd_text)} chars)")

    resume_text, used_resume = find_resume(job_folder)
    logger.info(f"Resume: {used_resume} ({len(resume_text)} chars)")

    ats_prompt = read_ats_prompt()

    full_prompt = (
        f"{ats_prompt}\n\n"
        f"--- JOB_DESCRIPTION ---\n{jd_text}\n\n"
        f"--- RESUME ---\n{resume_text}\n"
    )

    # Ensure Ollama is ready
    await _ensure_ollama()
    await _ensure_model(OLLAMA_MODEL)

    # Generate
    logger.info(f"Running eval with model={OLLAMA_MODEL} (this may take a few minutes)...")
    t0 = time.monotonic()
    raw_report = await _generate(full_prompt, OLLAMA_MODEL)
    elapsed = time.monotonic() - t0
    logger.info(f"Generation complete in {elapsed:.1f}s ({len(raw_report)} chars)")

    # Parse and score
    parsed = parse_ats_report(raw_report)
    parsed["model_used"] = OLLAMA_MODEL
    parsed["resume_used"] = used_resume
    fit = compute_fit_score(parsed)
    parsed.update(fit)

    # Save — auto-detect version if not specified
    if output_version:
        suffix = f"_{output_version}"
    else:
        # Find next available version: ats_report.json → ats_report_v2.json → ats_report_v3.json ...
        base = job_folder / "ats_report.json"
        if base.exists():
            v = 2
            while (job_folder / f"ats_report_v{v}.json").exists():
                v += 1
            suffix = f"_v{v}"
            print(f"Existing report found — saving as v{v} to avoid overwriting.")
        else:
            suffix = ""

    txt_path = job_folder / f"ats_report{suffix}.txt"
    txt_path.write_text(raw_report, encoding="utf-8")

    json_path = job_folder / f"ats_report{suffix}.json"
    json_path.write_text(json.dumps(parsed, indent=2, ensure_ascii=False), encoding="utf-8")

    # Summary
    reqs = parsed.get("requirements", [])
    met = sum(1 for r in reqs if r.get("status") == "met")
    partial = sum(1 for r in reqs if r.get("status") == "partial")
    missing = sum(1 for r in reqs if r.get("status") == "missing")

    logger.info(f"Overall fit: {parsed.get('overall_fit_pct', '?')}% ({parsed.get('total_score', '?')}/{parsed.get('max_possible', '?')})")
    logger.info(f"Requirements: {met} met, {partial} partial, {missing} missing")
    logger.info(f"Top gaps: {parsed.get('top_gaps', 'None')}")
    logger.info(f"Top strengths: {parsed.get('top_strengths', 'None')}")
    logger.info(f"Saved: {txt_path}")
    logger.info(f"Saved: {json_path}")


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <job_folder_name> [v2]")
        sys.exit(1)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    job_folder_name = sys.argv[1]
    output_version = sys.argv[2] if len(sys.argv) > 2 else ""

    logger.info(f"Model locked to: {OLLAMA_MODEL}")
    asyncio.run(run(job_folder_name, output_version))


if __name__ == "__main__":
    main()
