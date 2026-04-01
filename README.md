# QA Agent v2

Autonomní QA agent postavený na:
- **Playwright (Python)** — přímá browser automation přes accessibility tree
- **OpenAI gpt-4o** — function-calling agent loop
- **FastAPI + SSE** — live streaming kroků do frontendu
- **SQLite** — persistence runs + steps + screenshot paths

## Quickstart

```bash
# 1. Nastav API key
export OPENAI_API_KEY=sk-...

# 2. Nainstaluj závislosti (jednou)
pip install -r requirements.txt
python3 -m playwright install firefox

# 3. Spusť server
./start.sh

# 4. Otevři frontend
open index.html
# nebo: python -m http.server 3000 a přejdi na localhost:3000
```

Backend běží na `http://localhost:8000`.

## Proč a11y tree místo DOM snapshotu

| DOM snapshot | A11y tree (Playwright) |
|---|---|
| Závisí na CSS třídách a XPath | Závisí na role + accessible name |
| Láme se při refactoru frontendu | Odolný vůči změnám implementace |
| Verbose HTML — plýtvá tokeny | Kompaktní strukturovaný výstup |

## Architektura

```
index.html  ──POST /runs──►  FastAPI (server.py)
     ▲                            │
     │  SSE stream                │ asyncio.to_thread()
     │  (live kroky)              ▼
     │                      agent loop (agent.py)
     │                            │
     │           ┌────────────────┴────────────────┐
     │           │                                 │
     │     OpenAI API                    Playwright (Python)
     │     gpt-4o                        Firefox (headless)
     │     function-calling              a11y tree + actions
     │           │                                 │
     │           └──── tool calls + snapshots ────►│
     │                                             │
     │                                      screenshots/
     │                                      (PNG soubory)
     │                                             │
     └─────────────────────────────  SQLite (db.py)
                                     runs + steps + screenshot paths
```

## API

| Endpoint | Popis |
|---|---|
| `POST /runs` | Spustí test, streamuje SSE |
| `GET /runs` | Historie všech runs |
| `GET /runs/{id}` | Detail runu + kroky |
| `GET /screenshots/{file}` | Stažení screenshotu |

## Dostupné QA nástroje (model je vidí jako function calls)

| Nástroj | Co dělá |
|---|---|
| `navigate` | Přejde na URL |
| `snapshot` | Získá a11y tree aktuální stránky (s ref IDs) |
| `click` | Klikne na element (ref nebo popis) |
| `fill` | Vyplní input |
| `select_option` | Vybere v dropdownu |
| `wait_for_load` | Počká N ms |
| `assert_element` | Ověří přítomnost/nepřítomnost elementu |
| `assert_url` | Ověří URL |
| `assert_text_present` | Ověří text na stránce |
| `screenshot` | Uloží screenshot |
| `test_done` | Ukončí test s výsledkem |

## Screenshoty

Automaticky pořízeny při každém `FAIL` nebo `ERROR` assertu.
Uloženy v Docker volume `qa-data:/data/screenshots`, dostupné přes `/screenshots/{file}`.

## Rozšíření

- Přidat `keyboard` nástroj pro klávesové zkratky (`page.keyboard.press()`)
- Parallel runs: každý run spouští vlastní Playwright instanci (already thread-safe)
- Scheduled runs: přidat cron endpoint
- CI integrace: `POST /runs` vrací run ID, pak `GET /runs/{id}` pro výsledek
