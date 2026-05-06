# Pipeline flow (chronological)

Single narrative of what happens from **intake** to **persistence** and **optional exports**. Implementation entry point for the email path: `main.py` ? `process_email()` ? inner `_pipeline()`.

---

## A. Triggers (how a run starts)

| Trigger | What runs |
|---------|-----------|
| `python main.py` | Polling loop: Gmail fetch on interval |
| `python main.py --once` | Single poll pass |
| `python main.py --rescan <message_id>` | Re-fetch one Gmail thread, re-run pipeline, optional Notion |
| `python main.py --pick` / `--pick-mail` | Pick message interactively, rescan + optional Notion |
| `python main.py assess-url <url>` | Website-only path (no email): crawl ? facts ? scoring ? DB |
| `python main.py --test <file.pdf>` | Local PDF through Gate 2-style analysis (dev) |

---

## B. Email path ù order of operations

Roughly matches console stages you see in a full run.

### 0. Persist & metadata

- Insert/update SQLite row for the Gmail `message_id` (new deal).
- Attach run metadata (models, optional `CODE_VERSION`, external search mode).

### 1. Deterministic pre-filter (before any LLM)

- Legal / NDA / non-pitch heuristics (`_should_run_ai_on_email` in `main.py`).
- If skip: status **SKIPPED**, optionally Gmail ùneeds reviewù; **no** Gate 1.

### 2. Gate 1 ù mandate triage (LLM, email body only)

- **Input:** subject/body + attachment **metadata** (e.g. PDF name); **not** full deck text.
- **Output:** PASS / UNCERTAIN_READ_DECK / FAIL_CONFIDENT + geo/stage/sector hints.
- **Persistence:** `save_gate1` to SQLite.
- **Hard stop:** `FAIL_CONFIDENT` ends here unless debug/manual override.

### 3. PDF fetch & text extraction (no LLM for OCR engine)

- Download attachment via **Gmail API** (`tools/gmail_client.py`).
- **PDF ? markdown:** `tools/pdf_utils.py` (pymupdf4llm; optional Tesseract OCR path).
- Deck text capped by `MAX_MARKDOWN_CHARS`; excerpt may land in `logs/`.

### 4. Gate 2 ù deck analysis (LLM, multi-step)

- **2A** Fact JSON from deck markdown (no VC scores in that step).
- **2B** 11-dimension scorecard from facts (+ rubric); **overall score from code** (`config/scoring.py`).
- **2C** Partner brief composition (no new world facts).
- **Persistence:** `save_gate2` (facts, dimensions, snapshot, telemetry).

### 5. Auto-enrichment (optional web snippets, not LLM)

- If `AUTO_ENRICHMENT=1` and facts are ùthinù: run `agents/auto_enrichment.py`.
- Uses **Tavily and/or SerpAPI** via `agents/external_research.py` (`get_research_provider`). No-op if no API keys / disabled.
- Merges enriched fields back into `facts_json` for downstream steps.

### 6. Website crawl (optional, email flow)

- If `EMAIL_AUTO_WEBCRAWL=1` and a company URL is known: HTTP crawl ? combined markdown (`tools/website_to_markdown.py` etc.).
- May extract extra facts via `WebsiteScreeningAgent` when deck left gaps.
- Crawl markdown feeds traction helpers and Notion memo later.

### 7. Fund mandate summary (deterministic presentation)

- Prints mandate/traction summary; persists traction report when applicable.
- Final composite decisions for DB (fund fit / deck evidence / generic VC interest) are finalized around Gate 2 + policy helpers.

### 8. Gate 2.5 ù external check (optional)

- Runs when **ENRICHED** depth and `ENABLE_EXTERNAL_CHECK` (and cost caps allow).
- **LLM** plans/interprets; **optional** web snippets from same provider stack as step 5.
- **Persistence:** `save_gate25`, may auto-reject or queue for HITL.

### 9. HITL ù human-in-the-loop (optional)

- If `HITL_MODE=interactive`: terminal brief + Approve/Reject ? **Gmail drafts only** (no auto-send).
- If `HITL_MODE=skip`: status left for async review; no blocking prompt.

### 10. Notion (optional, separate from LLM)

- **Not** part of the core scoring call graph. Sync reads **SQLite** and writes rows + child memo pages (`agents/notion_sync.py`).
- Modes: `NOTION_AUTO_SYNC` (per-deal or batch after poll), `python main.py --sync-notion`, `--sync-notion-deal`, rescan hooks (`NOTION_SYNC_ON_RESCAN`).

### 11. Founder calls / Fireflies (optional, async)

- Webhook or CLI pulls transcript; appends to `founder_calls_json`; can re-sync Notion for that deal. See PRD Fireflies section and `tools/fireflies_*.py`.

---

## C. Website-only path (short)

`assess-url` / website mode: crawl first, Gate 1-style mandate on **site facts**, then VC extraction/scoring aligned with pipeline; same SQLite **`deals`** model. Details: `agents/website_screener.py`, `main.py` website branch.

---

## D. Where to read more

| Topic | Document |
|-------|----------|
| Product contract & states | [PRD.md](PRD.md) |
| Diagram + stack + env vars | [ARCHITECTURE.md](../ARCHITECTURE.md) |
| Per-gate LLM questions | [LLM_SCREENING_SPEC.md](LLM_SCREENING_SPEC.md) |
| Tavily vs SerpAPI vs LLM-only | [ARCHITECTURE.md](../ARCHITECTURE.md) (subsection *LLM vs search APIs (precisely)*) |
| Notion layout rules | [CURSOR_NOTION_INSTRUCTIONS.md](CURSOR_NOTION_INSTRUCTIONS.md) |
| Doc index by category | [README.md](README.md) |
