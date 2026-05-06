# Documentation index

Read **top to bottom** within each block, or jump by role at the end.

---

## 1. First 10 minutes (orientation)

| Order | File | Why open it |
|-------|------|-------------|
| 1 | [../README.md](../README.md) | Demo artifacts, gates one-liner, quick start |
| 2 | [PIPELINE_FLOW.md](PIPELINE_FLOW.md) | **Chronological** path: Gmail ? gates ? DB ? Notion |
| 3 | [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md) | What this repo is *not* (prototype boundaries) |

---

## 2. How screening works (depth)

### 2.1 Product & behavior

| File | Contents |
|------|----------|
| [PRD.md](PRD.md) | Full contract: gates, pipeline statuses, Notion, Fireflies, non-goals, acceptance-style notes |
| [LLM_SCREENING_SPEC.md](LLM_SCREENING_SPEC.md) | Analytical question map per Gate (1, 2A/B/C, 2.5, website) |
| [SCREENING_RUBRIC.md](SCREENING_RUBRIC.md) | 1–10 semantics per dimension |
| [SCREENING_SCORECARD.md](SCREENING_SCORECARD.md) | Scorecard shape / partner summary |

### 2.2 Runtime & wiring

| File | Contents |
|------|----------|
| [ARCHITECTURE.md](../ARCHITECTURE.md) | ASCII pipeline diagram, technology stack, **LLM vs Tavily/SerpAPI**, costs, `.env` table, repo layout |
| [PIPELINE_FLOW.md](PIPELINE_FLOW.md) | Step-by-step order aligned with `main.py` stages |

### 2.3 Integrations & ops

| File | Contents |
|------|----------|
| [CURSOR_NOTION_INSTRUCTIONS.md](CURSOR_NOTION_INSTRUCTIONS.md) | Notion subpage sections, upsert rules, sync pitfalls |
| Code: `agents/notion_sync.py`, `tools/gmail_client.py`, `tools/fireflies_*.py` | Source of truth for API behavior |

### 2.4 Context & use cases (narrative)

| File | Contents |
|------|----------|
| [../APPLICATION_NOTE.md](../APPLICATION_NOTE.md) | Application / portfolio context |
| [../APPLICATION_USE_CASES.md](../APPLICATION_USE_CASES.md) | VC ops use cases (markdown) |
| [use_cases_overview.txt](use_cases_overview.txt) | Short use-case list (plain text) |

---

## 3. Stubs / legacy filenames

| File | Points to |
|------|-----------|
| [PRODUCT_REQUIREMENTS.md](PRODUCT_REQUIREMENTS.md) | Canonical **PRD.md** |
| [SYSTEM_ARCHITECTURE.md](SYSTEM_ARCHITECTURE.md) | Canonical **ARCHITECTURE.md** (repo root) |

---

## 4. Suggested paths by role

**Reviewer (30–45 min):** [README](../README.md) ? [PIPELINE_FLOW](PIPELINE_FLOW.md) ? [PRD](PRD.md) (skim § gates & Notion) ? [ARCHITECTURE](../ARCHITECTURE.md) (diagram + LLM/search section).

**Implementer / debugger:** [PIPELINE_FLOW](PIPELINE_FLOW.md) ? [LLM_SCREENING_SPEC](LLM_SCREENING_SPEC.md) ? `main.py` ? `agents/screener.py` ? [ARCHITECTURE](../ARCHITECTURE.md) env table.

**Notion-heavy:** [CURSOR_NOTION_INSTRUCTIONS](CURSOR_NOTION_INSTRUCTIONS.md) + PRD § Notion ? `agents/notion_sync.py`.
