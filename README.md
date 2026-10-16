# grid
## Run it

Needs Docker. Keys are optional: with an empty `.env` the trader still runs on real NYISO prices with local fallbacks.

```bash
cp .env.example .env          # fill any sponsor keys you have
docker compose up -d --build  # hatchet + worker + api + agentglow + ui
```

| What | URL |
| --- | --- |
| UI | http://localhost:5520 |
| API | http://localhost:8520 |
| AgentGlow (3D backend view) | http://localhost:8104 |
| Hatchet dashboard | http://localhost:8388 (admin@example.com / Admin123!!) |

Hatchet runs (namespace `grid`):

```bash
# 30-night backtest: one Daytona sandbox per night, 8 at a time, streamed as SSE
curl -N -X POST localhost:8520/api/runs/backtest -H 'content-type: application/json' -d '{"days":30}'
# tomorrow's plan now (also scheduled daily 11:30 New York time, after NYISO day-ahead); stored pending approval
curl -X POST localhost:8520/api/runs/nightly -H 'content-type: application/json' -d '{}'
```

Without Docker (dev): `uv run uvicorn grid.api:app --port 8520`, `uv run python -m grid.worker` (needs the
compose Hatchet services and `./data/hatchet/token`), `cd ui && npm i && npm run dev`, `uvx agentglow==0.4.9 serve --port 8104`.
