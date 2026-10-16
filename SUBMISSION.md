# Grid: Devpost submission

**Project name:** Grid
**Tagline:** An AI trader for grid batteries: buy power when the city doesn't need it, sell it when it does, safely, with a human approving.

## Summary
Grid batteries make money by charging when electricity is cheap and discharging when it's expensive, but most run on
fixed rules like "charge at night, sell 5 to 9pm". Grid is an AI trading desk for your battery: a team of agents reads
tomorrow's published New York grid prices, plans every hour's buy and sell with an optimizer, verifies the plan in an
isolated Daytona sandbox, checks live grid alerts, and asks a human to approve by voice. On real NYISO prices it earns
2.3x the fixed rule tomorrow ($419 vs $186 for a 10 MW / 40 MWh battery in NYC) and $14,055 vs $6,081 over the last 30
real days (+131%), with zero safety violations.

## The problem and its impact
- New York is building about 6,000 MW of battery storage by 2030. Every battery has to decide, every hour, when to buy
  and when to sell. Simple rules leave most of the money on the table.
- The same decisions also help the grid: batteries that sell at the evening peak replace dirty peaker plants and lower
  peak prices for everyone. Tomorrow's evening sale from one 10 MW battery covers about 9,000 NYC homes at the peak.
- Anyone with a battery can use it: a 10 MW grid battery, a 2 MW warehouse battery or a 0.5 MW building battery. You
  enter your size, zone, current charge and rules ("only sell above $60"), and the plan respects them.

## How it works
1. **Ask or command in plain English** (search bar or voice): "How much will my battery make tomorrow?", "keep 30% for
   the storm", "only sell above $60", "approve". Every answer is generated on the fly as a custom page (OpenUI).
2. **The trading desk (deepagents on Claude via AWS Bedrock):** a market analyst reads NYISO day-ahead prices for your
   zone, a trader asks the optimizer for the best schedule, and a risk officer checks it against hard limits, your
   rules and live grid alerts, and can veto and send it back.
3. **The optimizer decides the numbers, not the LLM:** a mixed-integer program over 24 hours (charge, discharge, state of
   charge), solved in milliseconds. Hard limits: 10 to 90% charge (or your reserve), at most one cycle a day, never buy
   and sell in the same hour, end the day at least half full, your price rules.
4. **Daytona verifies every plan:** the plan is replayed in an isolated sandbox and only that result is shown. The
   30-day backtest fans out across parallel sandboxes (30 nights in about 25 seconds), orchestrated by Hatchet.
5. **Anchor reads live grid alerts:** a cloud browser opens NYISO's System Conditions page. On a Thunderstorm Alert or
   emergency for your zone, the risk officer raises the reserve to 30% and vetoes plans that don't keep it.
6. **ElevenLabs voice agent:** talk to the trader. It presents the plan, re-plans on your instructions and records your
   approval. Nothing is executed without approval.
7. **SurrealDB Agent Memory:** your battery profile, rules and approved plans are time-aware facts; a new rule supersedes
   the old one and the history stays queryable.
8. **Arize AX:** every agent step is traced; evals check that every dollar figure the agents say matches the sandbox
   simulation and that no unsafe plan is called safe. A prompt experiment improved the "voice-ready, grounded" score
   from 0.0 to 1.0.
9. **Hatchet** runs the nightly plan after NYISO publishes tomorrow's prices, and the parallel backtest.
10. **AgentGlow** shows the whole backend live: agents, sandboxes, models, memory.

## Safeguards
- The LLMs never set megawatts. Only the optimizer does, inside hard physical and operator limits.
- Every plan is independently simulated in a Daytona sandbox; the risk officer must cite its violations list.
- Live grid alerts tighten the rules automatically (reserve 30% on a Thunderstorm Alert).
- A human approves every plan (button or voice).
- Arize evals verify every claimed number against the simulation.
- Honest scoring: plans are made on day-ahead prices and scored on what actually happened (real-time prices).
- If any external service is down, the system degrades safely and says so on screen.

## Sponsor tools
- **Daytona:** plan verification sandbox; parallel backtest fan-out.
- **AWS:** Amazon Bedrock (Claude Sonnet) powers the trading desk, the search answers and the evals' judge.
- **ElevenLabs:** ElevenAgents conversational voice trader with client tools (get plan, re-plan, approve).
- **Arize AX:** OpenInference tracing, evals, prompt experiment comparison.
- **SurrealDB Agent Memory:** battery profiles, rules and plans with supersession.
- **Anchor Browser:** reads NYISO's live System Conditions page for grid alerts.

## Built with
Python, FastAPI, deepagents, LangChain, Hatchet, HiGHS (MILP), React, Vite, OpenUI, Recharts, AgentGlow, Docker Compose,
NYISO public data.

## Run it
`cp .env.example .env` (fill the keys), then `docker compose up -d --build`, open http://localhost:5520.

Repo: https://github.com/Nideesh1/grid

---

# Video script (under 2 minutes)

0:00 "Grid batteries make money by buying power when it's cheap and selling when it's expensive. Most run on a fixed
schedule. We built an AI trading desk for them."
0:10 Type "How much will my battery make tomorrow?" -> $419, buy/sell chart, safety checks, verified in Daytona.
0:30 Say into the mic: "Storm coming, keep 30% in reserve." Show the status lines: risk officer vetoes, trader
re-plans, Daytona re-verifies. Voice: "Done, $418, approve?" "Approve."
0:55 Type "simulate a thunderstorm alert": Anchor reads NYISO's live page, risk officer raises the reserve.
1:10 "How did we do the last 30 days?" -> $14,055 vs $6,081, +131%, 30 nights simulated in parallel sandboxes.
1:25 Flip to /backend (AgentGlow): agents, sandboxes, Bedrock, memory lighting up. Then Arize: traces and the eval
experiment.
1:45 "The AI proposes. Math decides. A sandbox verifies. A human approves."

# Slides (3-minute pitch)
1. Problem: batteries run on fixed rules; one battery leaves ~$8k/month on the table; NY is building 6 GW.
2. Grid: an AI trading desk for your battery (diagram of the flow).
3. Live demo.
4. Safeguards: math sets MW, sandbox verifies, alerts tighten rules, human approves, evals check every number.
5. Results: $419 vs $186 tomorrow; $14,055 vs $6,081 over 30 real days; 0 violations.
