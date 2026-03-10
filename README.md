# ATS Evaluation MCP Server — Setup Guide

## Prerequisites

- **Python 3.10+** (macOS ships with it, or `brew install python`)
- **Ollama** installed and working (`brew install ollama` or from ollama.com)
- **A model pulled** — recommended: `ollama pull qwen2.5:14b` (best for 16GB RAM)
  - Fallback if too slow: `ollama pull llama3.1:8b`

## Install dependencies

```bash
cd ~/Desktop/Job\ Search\ 2026/tools
pip3 install -r requirements.txt
```

## Test it works

```bash
# Make sure Ollama is running
ollama serve &

# Quick test — should print tool list
python3 ats_mcp_server.py <<< '{"jsonrpc":"2.0","method":"initialize","id":1,"params":{"capabilities":{}}}'
```

## Register with Claude Desktop

Edit your Claude Desktop MCP config:

**macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`

Add the `ats-evaluator` entry to the `mcpServers` section:

```json
{
  "mcpServers": {
    "ats-evaluator": {
      "command": "python3",
      "args": ["/Users/jamisonproctor/Desktop/Job Search 2026/tools/ats_mcp_server.py"],
      "env": {
        "ATS_WORKSPACE": "/Users/jamisonproctor/Desktop/Job Search 2026",
        "OLLAMA_MODEL": "qwen2.5:14b",
        "OLLAMA_HOST": "http://localhost:11434"
      }
    }
  }
}
```

> **Note:** Adjust the paths if your workspace folder is elsewhere. Keep any existing
> MCP servers (like obsidian) in the same config — just add this alongside them.

After saving, **restart Claude Desktop** for the new MCP server to be picked up.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `ATS_WORKSPACE` | `~/Desktop/Job Search 2026` | Path to the workspace root |
| `OLLAMA_MODEL` | `qwen2.5:14b` | Default Ollama model |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama API endpoint |

## Available tools

Once registered, Claude will see two new tools:

### `run_ats_eval`
Runs a full ATS evaluation. Requires `job_folder_name` (the folder name under `jobs/`).
Optional: `resume_filename`, `model`, `output_version`.

### `list_available_models`
Lists locally installed Ollama models.

## How it works

1. Reads the ATS eval prompt from `prompts/ats_eval.md`
2. Reads the JD from `jd.md` in the job folder
3. Extracts text from the resume PDF in the job folder
4. Sends everything to Ollama with temperature=0
5. Parses the structured response
6. Saves both `ats_report.txt` (raw) and `ats_report.json` (parsed) to the job folder
7. Returns the results to Claude for analysis

## Troubleshooting

- **"Ollama not running"** — Run `ollama serve` in a terminal, or the server will try to start it automatically.
- **"Model not found"** — Run `ollama pull qwen2.5:14b` (or your chosen model).
- **"No PDF found"** — Make sure the resume PDF is copied into the job folder before running the eval.
- **Slow first run** — The model loads into memory on first request. Subsequent runs are faster.
