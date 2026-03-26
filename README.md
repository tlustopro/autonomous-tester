# QA Agent v2

Autonomní QA agent postavený na:
- **Playwright MCP** (Microsoft, Docker) — browser automation přes accessibility tree
- **OpenAI gpt-4o** — function-calling agent loop
- **FastAPI + SSE** — live streaming kroků do frontendu
- **SQLite** — persistence runs + steps + screenshotů

## Quickstart

```bash
# 1. Nastav API key
export OPENAI_API_KEY=sk-...

# 2. Spusť vše
docker compose up --build

# 3. Otevři frontend
open index.html
# nebo: python -m http.server 3000 a přejdi na localhost:3000
```

Backend běží na `http://localhost:8000`, Playwright MCP na portu `8931`.

## Proč a11y tree místo DOM snapshotu

| DOM snapshot | A11y tree (Playwright MCP) |
|---|---|
| Závisí na CSS třídách a XPath | Závisí na role + accessible name |
| Láme se při refactoru frontendu | Odolný vůči změnám implementace |
| Verbose HTML — plýtvá tokeny | Kompaktní strukturovaný výstup |
| Potřebuje vlastní parser | MCP server to řeší za nás |

## Architektura

```
index.html  ──POST /runs──►  FastAPI (server.py)
                                  │
                            agent loop (agent.py)
                                  │
                    ┌─────────────┴─────────────┐
                    │                           │
             OpenAI API                  Playwright MCP
             (function calling)          (Docker :8931)
                    │                           │
                    └──── a11y snapshot ◄───────┘
                          + tool calls
                                  │
                              SQLite (db.py)
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
| `snapshot` | Získá a11y tree aktuální stránky |
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

- Přidat `keyboard` nástroj (`browser_press_key`) pro klávesové zkratky
- Parallel runs: spustit více `PlaywrightMCPClient` instancí (každá dostane vlastní session)
- Scheduled runs: přidat cron endpoint
- CI integrace: `POST /runs` vrací run ID, pak `GET /runs/{id}` pro výsledek
