# grid: contracts (all build tracks code against this)

An AI energy dispatcher for a NYC electric bus depot. It plans tonight's charging on real NYISO prices, simulates the
plan in a Daytona sandbox, enforces hard safety limits, asks a human to approve by voice, and proves the dollars saved.

Rules: uv for Python (py>=3.11). No em dashes anywhere. Never print or log secrets (.env). Real public data only.
Deadline: 3:30pm today. Prefer working + demo-able over complete. Every sponsor integration must degrade gracefully
when its key is missing in .env (feature flag, clear log line), so the demo never breaks.

## Ports
API 8520 | UI 5520 | AgentGlow 8104

## Data (package `grid/`)
- NYISO zonal prices, public CSV, no key:
  real-time 5-min: `http://mis.nyiso.com/public/csv/realtime/YYYYMMDDrealtime_zone.csv`
  day-ahead hourly: `http://mis.nyiso.com/public/csv/damlbmp/YYYYMMDDdamlbmp_zone.csv`
  columns: "Time Stamp","Name","PTID","LBMP ($/MWHr)",... Zone for the depot: `N.Y.C.`.
  Cache under data/ (gitignored). `grid/prices.py`: `day_ahead(date) -> list[{ts, usd_mwh}]` (hourly),
  `real_time(date) -> list[{ts, usd_mwh}]` (5-min), `history(days=30, kind="rt")`.
- Depot model `grid/depot.py` (realistic, documented constants; cite public MTA / New Flyer / Proterra specs in comments):
  40 buses, battery 440 kWh, chargers 150 kW each (40 chargers), site power cap 3,000 kW, buses arrive 19:00 at
  20-40% SOC (seeded random per date), depart 05:00, target SOC >= 90%.
- Bill model `grid/bill.py`: energy cost = sum(kWh_t * LBMP_t / 1000) + delivery adder $0.05/kWh (constant, documented);
  monthly demand charge $30/kW on the month's peak 15-min kW (ConEd SC9-like, documented as approximate). Report both.

## Optimizer `grid/optimize.py`
- `baseline(depot, prices)`: every bus charges at full power from arrival until full ("plug in at 7pm").
- `optimal(depot, prices, cap_kw)`: LP (scipy.optimize.linprog or PuLP/HiGHS) minimizing energy cost + demand charge
  share, subject to: per-bus charger limit, site cap, every bus >= target SOC by departure, SOC <= 100%.
- `simulate(plan, depot, prices) -> {cost_usd, peak_kw, min_soc_at_departure, violations:[...]} ` (pure function;
  this is what runs inside the Daytona sandbox).
- `savings(baseline, plan) -> {tonight_usd, monthly_usd (x30 + demand-charge delta), pct}`.
- Backtest: run baseline vs optimal over the last 30 days of real-time prices -> per-day savings list.

## Agent + sponsors (`grid/agent.py`, `grid/sponsors/*.py`)
- LLM: Mistral (`MISTRAL_API_KEY`, mistral-large-4 with reasoning_effort none) else Anthropic (`ANTHROPIC_API_KEY`).
  deepagents or a simple tool loop; tools: get_prices, get_depot_state, build_plan, simulate_in_sandbox, savings,
  remember, recall, request_approval.
- Daytona (`DAYTONA_API_KEY`): `simulate_in_sandbox(plan)` uploads grid/optimize.py + inputs to a Daytona sandbox,
  runs `simulate`, returns results. Fallback: local subprocess, flagged "local".
- SurrealDB Agent Memory (`SURREAL_URL`, `SURREAL_TOKEN`): facts with valid time: price forecasts (each new day-ahead
  supersedes the previous forecast for that hour), depot constraints (e.g. "bus 12 must leave at 04:00" supersedes),
  approved plans with source. Fallback: local SQLite with the same valid_from/valid_until/superseded_by shape.
- Arize (`ARIZE_SPACE_ID`, `ARIZE_API_KEY`): OpenInference tracing of the agent + evals: constraint check (simulate
  violations == 0), savings claim matches simulation, explanation grounded in tool results.
- ElevenLabs (`ELEVENLABS_API_KEY`): voice dispatcher. Minimum: TTS of the nightly plan ("Charge 1 to 4am, saves
  $1,840, approve?"). Stretch: conversational agent / phone call to DISPATCH_PHONE with yes/no approval.
- Anchor (`ANCHOR_API_KEY`): optional; fetches the NYISO public price page or a utility tariff page in a cloud browser
  as a secondary source / cross-check. Never logins, never scraping private data.
- AgentGlow: `agentglow.watch()`; spans: agent "dispatcher", inference group "mistral", database "surrealdb",
  `agentglow.job` for sandbox runs (kind "daytona-sim").

## API `grid/api.py` (FastAPI :8520)
- `GET /api/prices?date=` -> day-ahead + real-time series for N.Y.C.
- `POST /api/plan {date?, cap_kw?}` -> SSE events: {"kind":"status","text"} | {"kind":"plan", baseline, optimal,
  schedule:[{hour, kw}], savings, checks} | {"kind":"done"}.
- `GET /api/backtest?days=30` -> per-day {date, baseline_usd, optimal_usd, saved_usd}, totals.
- `POST /api/approve {plan_id, approved}`.

## UI `ui/` (Vite React TS :5520)
One page, dark, editorial (no rounded AI cards). Top: big "$X saved tonight / $Y per month" with the real-price
source. Chart: tonight's NYC price curve (bars) with baseline load (red area at 7pm) vs optimal load (fills the cheap
hours), site cap line. Safety panel: checks (all buses >= 90% by 5am, peak <= cap, simulated in Daytona: id).
Backtest strip: 30 days of real savings. Approve button + voice playback. Link to /backend AgentGlow scene.
