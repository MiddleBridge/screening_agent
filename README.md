# Fund Screening Agent

AI-powered deal screening pipeline for early-stage VC funds.

Reads inbound pitch emails (Gmail + PDF deck), runs multi-gate screening against the fund's investment thesis, scores deals, and syncs structured output to a Notion CRM — no investment decisions, just clean decision support.

Built for a CEE-focused seed fund mandate (configurable in `config/fund_thesis.py`).

## How it works

| Gate | What it does |
|------|-------------|
| **Gate 0** | Deterministic pre-filter: skips legal/NDA emails, only calls LLM when pitch signals are present |
| **Gate 1** | Mandate fit check from email body + PDF metadata (no full deck parse yet) |
| **Gate 2** | Full deck → markdown/OCR → fact extraction, scorecard, fund fit decision |
| **Gate 2.5** | Optional: external enrichment (web crawl, deeper context) |
| **HITL** | Optional: interactive review or skip |

**Website mode:** `python main.py assess-url https://…` — crawl → facts → scoring → same pipeline record.

## Key integrations

- **Gmail** — OAuth polling for inbound pitches
- **PDF/OCR** — Tesseract-based deck extraction
- **Notion** — auto-sync deals to a CRM table (status, thesis fit, rationale, memo)
- **Fireflies** — webhook or CLI pull for founder call notes, attached to the deal record

## Repo structure

```
agents/       — screening logic (Gate 1/2/2.5, website, scoring, Notion sync)
tools/        — Gmail, PDF/OCR, web crawl
storage/      — SQLite pipeline.db
config/       — prompts, scoring weights, LLM cost tracking
tests/        — unit tests
docs/         — PRD, detailed specs, rubrics, scorecard
```

## Quick start

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # fill in API keys
python main.py --setup  # Gmail OAuth
python main.py --once   # single polling run
```

Requires Python 3.9+, Tesseract (for PDF OCR), Gmail OAuth credentials.

## Example output (Notion)

Pipeline produces a structured Notion table with columns:
**Deal name → Status → Investment thesis (Yes/No) → Rationale → Mandate fit → Source**

Each deal page contains a full memo: company snapshot, email content, deck OCR, web crawl, and (if available) founder call notes.

## Docs

- [APPLICATION_NOTE.md](APPLICATION_NOTE.md) — context on this project as an application artifact
- [APPLICATION_USE_CASES.md](APPLICATION_USE_CASES.md) — top AI use cases for VC workflows
- [ARCHITECTURE.md](ARCHITECTURE.md) — technical architecture and data flows
- [docs/](docs/) — PRD, screening rubric, scorecard, detailed specs
