"""Hatchet worker for grid (nightly_plan + backtest fan-out). Run: uv run python -m grid.worker"""
import grid.workflows as wf  # first: sets the Hatchet client env (token, host, namespace "grid")

import logging  # noqa: E402
import os  # noqa: E402

from grid.config import AGENTGLOW_URL  # noqa: E402

log = logging.getLogger("grid.worker")


def _tracing() -> None:
    """agentglow.watch(): Hatchet task spans + agentglow.job per run go to the AgentGlow scene (best effort)."""
    try:
        from grid.sponsors import arize

        arize.setup()
    except Exception as e:  # noqa: BLE001
        log.warning("arize tracing disabled: %s", type(e).__name__)
    try:
        import agentglow

        agentglow.watch(AGENTGLOW_URL, service_name="grid-worker", privacy="standard")
        log.info("agentglow tracing -> %s", AGENTGLOW_URL)
    except Exception as e:  # noqa: BLE001
        log.warning("agentglow disabled: %s", e)


def main() -> None:
    _tracing()
    slots = int(os.environ.get("WORKER_SLOTS", "12"))  # 1 parent + 8 sandbox children + nightly, with headroom
    print("grid worker: " + ", ".join(w.name for w in wf.WORKFLOWS) + f" | slots {slots}", flush=True)
    wf.hatchet.worker("grid", workflows=wf.WORKFLOWS, slots=slots).start()


if __name__ == "__main__":
    main()
