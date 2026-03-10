# ATS Resume Evaluator — AI-Powered Resume Screening, Running Locally on Your Machine

**Stop guessing whether your resume will pass the ATS.** This MCP server plugs directly into Claude Desktop and uses a local LLM to score your resume against any job description — requirement by requirement — with zero data leaving your machine.

Drop in a job description and your resume. Get back a detailed fit report with per-requirement scores, evidence from your resume, identified gaps, and an overall fit percentage. All powered by Ollama running locally.

---

## Why This Exists

Most job seekers tailor resumes blindly. Recruiters use ATS software to filter candidates before a human ever reads the application. This tool flips the script — it lets you **run the same kind of structured evaluation recruiters use**, so you can see exactly where your resume hits and where it misses, *before* you apply.

**Key benefits:**

- **Privacy-first** — Everything runs locally via Ollama. Your resume and job descriptions never leave your machine.
- **Requirement-level scoring** — Each job requirement is individually scored on a 0–4 fit scale with verbatim evidence pulled from your resume.
- **Actionable gaps** — Instantly see which requirements you're missing or only partially meeting, so you know exactly what to fix.
- **Works inside Claude Desktop** — Just ask Claude to evaluate your resume. The MCP integration means zero context-switching.
- **Deterministic results** — Temperature-zero inference gives you consistent, reproducible evaluations.

---

## What You Get

For each evaluation, the server produces a structured report containing:

| Output | Description |
|---|---|
| **Per-requirement fit scores** | Each requirement scored 0–4 (none → exact match) with status, evidence, and rationale |
| **Overall fit percentage** | Aggregate score across all requirements |
| **Top gaps** | The requirements where your resume falls short |
| **Top strengths** | The requirements where your resume is strongest |
| **Raw + JSON reports** | Saved to disk for comparison across resume versions |

---

## Quick Start

### Prerequisites

- **Python 3.10+**
- **Ollama** — `brew install ollama` or [ollama.com](https://ollama.com)
- **A model pulled** — `ollama pull qwen3.5:9b` (recommended for 16 GB RAM)

### Install

```bash
git clone https://github.com/JamisonProctor/ats-mcp-server.git
cd ats-mcp-server
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Register with Claude Desktop

Edit your Claude Desktop MCP config:

**macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`

Add the `ats-evaluator` entry to the `mcpServers` section:

```json
{
  "mcpServers": {
    "ats-evaluator": {
      "command": "<path-to>/ats-mcp-server/.venv/bin/python3",
      "args": ["<path-to>/ats-mcp-server/ats_mcp_server.py"],
      "env": {
        "ATS_WORKSPACE": "<path-to-your-job-search-workspace>",
        "OLLAMA_MODEL": "qwen3.5:9b",
        "OLLAMA_HOST": "http://localhost:11434"
      }
    }
  }
}
```

Replace `<path-to>` with the actual paths on your machine. Restart Claude Desktop after saving.

### Set Up Your Workspace

```
<workspace>/
├── prompts/
│   └── ats_eval.md          # Eval prompt template (included in this repo)
└── jobs/
    └── <company__role>/
        ├── jd.md             # Paste the job description here
        └── resume.pdf        # Your resume
```

### Run an Evaluation

Just ask Claude:

> "Evaluate my resume against the google__product_manager job"

Claude will queue the evaluation, poll for results, and present you with the full fit report — scores, gaps, strengths, and all.

---

## How It Works

1. **You ask Claude** to evaluate your resume against a specific job folder.
2. **Claude calls `run_ats_eval`** — the server reads the JD, extracts text from your resume PDF, and sends everything to Ollama with a structured evaluation prompt.
3. **The LLM analyzes the match** — it identifies 6–10 key requirements from the JD, then scores each one against evidence found (or not found) in your resume.
4. **Results come back structured** — per-requirement fit scores, overall fit percentage, top gaps, top strengths.
5. **Reports are saved** — both raw text and parsed JSON are written to the job folder for future reference.

Evaluations run in a sequential queue to respect GPU constraints. You can version outputs (`v1`, `v2`, etc.) to compare how resume edits affect your scores.

---

## Available MCP Tools

| Tool | Description |
|---|---|
| `run_ats_eval` | Start an ATS evaluation for a job folder. Returns immediately; evaluation runs in the background. |
| `check_ats_eval` | Poll the status of a running evaluation. Returns the full report when complete. |

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `ATS_WORKSPACE` | `~/Desktop/Job Search 2026` | Root directory containing `jobs/` and `prompts/` |
| `OLLAMA_MODEL` | `qwen3.5:9b` | Ollama model to use for evaluations |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama API endpoint |

---

## Troubleshooting

- **"Ollama not running"** — Run `ollama serve` in a terminal, or the server will try to start it automatically.
- **"Model not found"** — Run `ollama pull qwen3.5:9b` (or your configured model).
- **"No PDF found"** — Make sure your resume PDF is in the job folder before running the eval.
- **Slow first run** — The model loads into memory on first request. Subsequent runs are faster.

---

## Built With

- [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) — Tool interface for Claude Desktop
- [Ollama](https://ollama.com) — Local LLM inference
- [PyMuPDF](https://pymupdf.readthedocs.io/) — PDF text extraction

---

## License

MIT
