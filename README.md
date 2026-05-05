# Fund AI Screening Agent

## Teza inwestycyjna funduszu (mandat)

Pipeline jest skalibrowany pod **wczesne startupy software** z **Europy Środkowo-Wschodniej i diasporą** (mocny link do regionu także przy HQ poza CEE), etap **pre-seed / seed** (węższy zakres późniejszych rund tylko tam, gdzie reguły na to pozwalają), typowy **ticket seed VC**, sektory m.in. **developer tools, AI/ML, SaaS, healthcare, marketplace, B2B**. Dokładne reguły i słowniki są w kodzie (`config/fund_thesis.py`, `agents/fund_decision.py`, kryteria w `notion_sync` / Gate 1–2).

Agent **nie podejmuje decyzji inwestycyjnych za partnera**: zbiera fakty z maila + decka PDF albo ze strony WWW, przechodzi przez bramki (Gate 0–2, opcjonalnie 2.5), zapisuje wynik w **`pipeline.db`** i może wypchnąć **uporządkowany widok do Notion**.

---

## 1) Jak to jest zbudowane

### Główne katalogi
- `main.py` — CLI i orchestracja.
- `agents/` — screening (Gate 1/2/2.5, www, scoring, **notion_sync**, raporty).
- `tools/` — Gmail, PDF/OCR, crawl strony → markdown.
- `storage/` — SQLite `pipeline.db`.
- `config/` — prompty, scoring, koszty LLM.
- `tests/` — testy jednostkowe.

### Kluczowe moduły
- `agents/screener.py` — email + deck.
- `agents/website_screener.py` — wejście tylko z URL.
- `agents/external_check.py` + `agents/final_scoring.py` — Gate 2.5.
- `agents/notion_sync.py` — sync do Notion (batch / pojedynczy deal).
- `storage/database.py` — statusy i zapis etapów.

---

## 2) Przepływ (email + deck)

| Etap | Co robi |
|------|--------|
| **Gate 0** | Szybki, deterministyczny prefilter w `main.py` (`_should_run_ai_on_email`): odcina m.in. maile legal/NDA; bez sygnału pitch/deck nie woła LLM. |
| **Gate 1** | Fit do **mandatu funduszu** z treści maila (+ meta PDF, bez pełnego decku). Werdykty m.in. `PASS`, `UNCERTAIN_READ_DECK`, `FAIL_CONFIDENT`. |
| **Gate 2** | PDF → markdown/OCR → fakty + scorecard + decyzje (`fund_fit_decision`, `deck_evidence_decision`, `generic_vc_interest`, `final_action`). |
| **Gate 2.5** | Opcjonalnie: zewnętrzne wzbogacenie (env + głębokość). |
| **HITL** | Opcjonalnie: tryb interaktywny lub `skip`. |

**Website:** `python main.py assess-url https://…` — crawl → fakty → scoring → ten sam model rekordu w `pipeline.db`.

---

## 3) Notion — co widzisz w tabeli

Kolejność **pierwszych kolumn** w widoku tabeli (sync): **title** → **Status** → **Investment thesis** + **Investment thesis rationale** (tekst: czemu Tak/Nie) → **Mandate rationale** (PASS/FAIL z kontekstem CEE/stage) → **Message ID** … (kolumna **Mandate pass** usunięta ze schematu — duplikowała **Meets fund criteria**).

### Status (CRM, prosty język)

| Wartość | Znaczenie dla zespołu |
|--------|------------------------|
| **To be reviewed** | Jeszcze do decyzji — trzeba spojrzeć. |
| **Rejected** | Nie idziemy dalej. |
| **Schedule an intro call** | Jesteśmy gotowi umówić rozmowę z founderem. |
| **Due diligence** | Wchodzimy w głębszy proces po stronie funduszu. |

**Investment thesis** — tylko **`PASS`** w polu `fund_fit_decision` (SQLite `deals`) daje **Tak** (zielony); inaczej **Nie** (czerwony). W Notion mogą nadal występować starsze, „legacy” nazwy właściwości — kanonicznie patrz kod w `agents/notion_sync.py`.

**Meets fund criteria** + **Mandate rationale** — krótka linia (~96 znaków), np. „PASS — Both (HQ CEE); 2 nat.; seed”.

Treść **strony** rekordu w Notion to memo zbudowane z `pipeline.db` w `agents/notion_sync.py` (sekcje m.in. snapshot, źródła, treść maila, OCR decka, crawl www) — **sync nie woła LLM**.

---

## 4) Statusy w SQLite (`deals`)

Pełna lista w `storage/database.py` (m.in. `NEW`, `WAITING_HITL`, `REJECTED_GATE1/2`, `ERROR`, `SKIPPED` …). To **techniczny** stan pipeline’u; **Status** w Notion (powyżej) to uproszczony widok dla partnera.

---

## 5) CLI

```bash
cd /path/to/Fund_AI
./venv/bin/python main.py              # polling Gmail
./venv/bin/python main.py --once
./venv/bin/python main.py --pick
./venv/bin/python main.py --rescan '<MESSAGE_ID>'
./venv/bin/python main.py --sync-notion --days 30 --notion-ensure-schema
./venv/bin/python main.py --sync-notion-deal '<MESSAGE_ID>' --notion-ensure-schema
./venv/bin/python main.py --report --days 7
./venv/bin/python main.py --setup      # OAuth Gmail
./venv/bin/python main.py --fireflies-hook   # HTTP: Fireflies webhook → pipeline (see below)
```

`main.py` ładuje `.env` (`python -c "…"` bez importu `main` **nie** załaduje zmiennych — używaj `main.py` albo `load_dotenv()`).

---

## 5a) Fireflies — notatki z rozmów na stronie deala

**Cel:** po callu z founderem notatka (summary + link + fragment transkryptu) ląduje w **`pipeline.db`** (`founder_calls_json`) i w memo Notion (**🎙 Founder calls**), przy tym samym dealu co screening.

### Tryb A — webhook (production-ready path)

1. W Fireflies [Webhooks V2](https://app.fireflies.ai/integrations/api/webhook) ustaw **HTTPS URL** (np. `https://hooks.twoja-domena.pl/fireflies/webhook`) i **signing secret**.
2. W `.env`: `FIREFLIES_API_KEY`, `FIREFLIES_WEBHOOK_SIGNING_SECRET` (jak w dashboardzie), opcjonalnie `FIREFLIES_HOOK_*`.
3. Uruchom receiver: `./venv/bin/python main.py --fireflies-hook` (za reverse proxy terminating TLS — Fireflies wymaga publicznego HTTPS).
4. Zasubskrybuj zdarzenia **`meeting.summarized`** (zalecane) i/lub **`meeting.transcribed`**.
5. **Mapowanie deala:** najpewniej `client_reference_id` = **`message_id`** z pipeline (wtedy przy uploadach/API); albo automatycznie: **jednoznaczne** dopasowanie tytułu spotkania do `company_name` w ostatnich dealach (patrz `FIREFLIES_TITLE_MATCH_DAYS`). Przy 0 lub >1 dopasowaniu zapis się **nie wykona** — użyj `fireflies-pull` z `--client-ref`.

### Tryb B — ręcznie / CLI

- **`sync-call`** — wklejasz summary, URL, opcjonalnie plik transkryptu: patrz docstring w `main.py`.
- **`fireflies-pull <MEETING_ID>`** — pobiera transkrypt z API Fireflies i robi ten sam zapis co webhook (bez HMAC).

Szczegóły kontraktu: **`PRD.md`** (Fireflies).

---

## 6) `.env` (skrót)

- `OPENAI_API_KEY`, `GMAIL_CREDENTIALS_PATH`, `GMAIL_TOKEN_PATH`, `GMAIL_USER_EMAIL`
- `NOTION_API_KEY`, `NOTION_DATABASE_ID`, `NOTION_AUTO_SYNC`, `NOTION_COMPACT_MODE`
- `SCREENING_DEPTH`, `ENABLE_EXTERNAL_CHECK`, `HITL_MODE`, `POLLING_INTERVAL_MINUTES`

---

## 7) Instalacja lokalna

```bash
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
./venv/bin/python main.py --setup
```

Wymaga m.in. **Python 3.9+**, **Tesseract** (OCR PDF), konto Gmail z OAuth.

---

## 8) launchd (macOS)

Jeśli uruchamiasz agenta przez LaunchAgenta, użyj identyfikatora **`com.fund.screening`** (plik: `~/Library/LaunchAgents/com.fund.screening.plist`). Jeśli wcześniej miałeś inną etykietę, `bootout` podaj **dokładnie** ten sam label co w `plist`.

```bash
launchctl list | grep -i screening
pgrep -fl "main.py"
```

Wyłączenie: `launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.fund.screening.plist`

---

## 9) Testy

```bash
pytest -q
```

---

## 10) Bezpieczeństwo

Nie commituj `.env` ani `pipeline.db` z produkcją. Rotuj klucze po wycieku.

---

## 11) Dokumentacja produktowa

- **`PRD.md`** — kontrakt produktowy i mandat (aktualizowany razem z kodem).
- **`ARCHITECTURE.md`** — struktura repo i przepływy techniczne.

Szczegóły implementacji Notion na dziś: kod **`agents/notion_sync.py`** ma pierwszeństwo przed starszymi opisami w innych plikach markdown, jeśli się rozjeżdżają.
