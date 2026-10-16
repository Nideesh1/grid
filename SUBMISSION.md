# Grid: Devpost submission draft

**Project name:** Grid
**Tagline:** An AI dispatcher that charges electric bus fleets when power is cheap, and proves the savings on real grid prices.

## Summary (2-3 sentences)
Electric bus depots plug every bus in at 7pm, the most expensive hour of the day, and pay huge utility demand charges for
that one spike. Grid is an AI dispatcher that plans each night's charging on real NYISO wholesale prices, simulates the plan
in a Daytona sandbox, enforces hard safety limits (every bus at least 90% charged by 5am, never over the site's power cap),
and asks a human to approve by voice. On real New York City prices it cuts a 40-bus depot's charging bill by about 48%:
about $2,000 a night, $59,000 over the last 30 nights.

## The problem and its impact
- Cities are electrifying bus fleets (NYC's MTA is converting its whole fleet). Depots charge on fixed schedules, so every bus
  draws power at once in the evening peak.
- Large customers pay a monthly demand charge on their single highest 15-minute peak. One evening spike sets the bill for
  the whole month. The expensive part isn't the electricity, it's everyone plugging in at once.
- Grid shifts and flattens charging into the cheap overnight hours with the same buses, the same mileage and the same
  departure times. Measured on real NYISO N.Y.C. zone prices: baseline $4,181 vs agent plan $2,148 tonight; 30-night
  backtest $123,389 vs $64,414 (saved $58,974, 47.8%), with 0 safety violations.
- Taxpayers fund transit, so every dollar saved goes back to service.

## How it works (architecture)
1. **Prices:** NYISO publishes 5-minute real-time and next-day hourly wholesale prices as public files. Grid pulls the N.Y.C.
   zone for tonight.
2. **Plan:** a linear program schedules 40 buses x 15-minute slots (solves in about 0.01s) to minimize energy cost plus
   demand charge, subject to per-charger limits, the site power cap and every bus at least 90% by 5am.
3. **Simulate (Daytona):** the plan and the simulator run inside an isolated Daytona sandbox before anyone sees it. The
   sandbox result is the source of truth for the safety checks.
4. **Explain (AWS Bedrock, Claude):** the dispatcher agent explains the plan using only numbers from the tools.
5. **Approve (ElevenLabs):** a voice dispatcher presents tonight's plan. The operator can ask "what if bus 12 leaves at
   4am?" and the agent re-plans and re-simulates live, then the operator approves by voice.
6. **Remember (SurrealDB Agent Memory):** constraints, forecasts and approved plans are time-aware facts. A new constraint
   supersedes the old one without erasing history, so the dispatcher can say what changed since last night and why.
7. **Evaluate (Arize AX):** every run is traced. Evals check that the dollar figures the agent says match the sandbox
   simulation and that it never claims a plan is safe when it isn't. We ran the loop: trace, eval, find failures, fix the
   prompt, rerun as an experiment and compare.
8. **Cross-check (Anchor Browser):** the agent opens NYISO's public live price page in a cloud browser and confirms the
   price in the data feed matches what the grid operator is showing.
9. **Observe (AgentGlow):** a live 3D view of the dispatcher, sandbox runs, model calls and memory.

## Safeguards
- Hard constraints are enforced by the optimizer and verified again by an independent simulation in a sandbox. The LLM never
  sets the schedule.
- Human approval is required. Nothing executes without it.
- Every number on screen and in the voice message comes from a tool result, and Arize evals check that.
- If any sponsor service is unavailable the system degrades safely (local simulation, flagged as such).

## Sponsor tools used
- **Daytona:** sandboxed simulation of every plan; safety checks come from the sandbox run.
- **Arize AX:** OpenInference tracing of the agent, plus evals (grounded savings, constraint check) and a before/after prompt
  experiment.
- **AWS:** Amazon Bedrock (Claude Sonnet) powers the dispatcher's reasoning.
- **ElevenLabs:** ElevenAgents conversational voice dispatcher with client tools (get plan, re-plan with a constraint,
  approve).
- **SurrealDB Agent Memory:** time-aware constraints, forecasts and approvals with supersession.
- **Anchor Browser:** live cross-check of the price against NYISO's public page.

## Built with
Python, FastAPI, scipy/HiGHS, React, Vite, Recharts, AgentGlow, NYISO public data.

## Repo
https://github.com/Nideesh1/grid

---

# Slides (3 minutes)

1. **The problem.** "Electric bus depots plug in at 7pm: the most expensive hour, and one spike sets the whole month's bill."
   Chart: baseline spike at 7pm over the price curve.
2. **Grid.** "An AI dispatcher that charges when power is cheap, inside hard safety limits, with a human approving."
   Flow diagram: NYISO prices -> plan -> Daytona simulation -> voice approval -> memory -> Arize evals.
3. **Live demo.** Talk to the dispatcher, add "bus 12 leaves at 4am", watch it re-plan, approve.
4. **Safeguards.** Optimizer sets the schedule (not the LLM); every plan simulated in a sandbox; every bus at least 90% by
   5am; never over the cap; human approval; Arize checks every claimed dollar.
5. **Results.** "$2,032 tonight. $58,974 over the last 30 real nights. 0 violations." One depot; NYC has dozens.
