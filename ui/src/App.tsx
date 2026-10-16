// TRADE page: search-first. Everything below the bar is generated per query as OpenUI (see ask/).
import { useEffect, useState } from "react";
import AskTrader from "./ask/AskTrader";
import { MOCK, setMockScale } from "./api";
import { BatteryPanel } from "./BatteryPanel";
import { DEFAULT, loadBattery, saveBattery, type Battery, type BatteryChange } from "./battery";
import { useLive } from "./Landing";

function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
  useEffect(() => {
    const k = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [onClose]);
  return (
    <div className="modal-bg" onClick={onClose}>
      <div className="modal" role="dialog" aria-label={title} onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span>{title}</span>
          <button className="x" onClick={onClose} aria-label="Close">×</button>
        </div>
        {children}
      </div>
    </div>
  );
}

function StatusLine({ battery, onEdit }: { battery: Battery; onEdit: () => void }) {
  const live = useLive();
  const z = live?.zones.find((x) => x.name === battery.zone) ?? live?.zones.find((x) => x.name === "N.Y.C.");
  const series = live?.nycToday.map((p) => p.usd_mwh) ?? [];
  const rank = z && series.length ? series.filter((v) => v < z.usd_mwh).length / series.length : 0.5;
  const signal = !z ? null : rank <= 0.3 ? "BUY" : rank >= 0.7 ? "SELL" : "HOLD";
  return (
    <p className="statusline">
      {z ? (
        <span>
          {z.name === "N.Y.C." ? "NYC" : z.name} power <b className="mono">${z.usd_mwh.toFixed(2)}</b>/MWh now
        </span>
      ) : (
        <span>reading live prices</span>
      )}
      {signal && (
        <span className={`sig ${signal.toLowerCase()}`} title={`${Math.round(rank * 100)}th percentile of today's prices`}>
          <i />
          {signal}
        </span>
      )}
      <span>
        battery {battery.power_mw} MW / {battery.energy_mwh} MWh · {battery.zone} ·{" "}
        <button className="inline" onClick={onEdit}>edit</button>
      </span>
    </p>
  );
}

export default function App() {
  const [battery, setBattery] = useState<Battery>(DEFAULT);
  const [hist, setHist] = useState<BatteryChange[]>([]);
  const [src, setSrc] = useState<"api" | "local">("local");
  const [ready, setReady] = useState(false);
  const [saving, setSaving] = useState(false);
  const [modal, setModal] = useState<"battery" | "how" | null>(null);

  useEffect(() => {
    loadBattery().then((r) => {
      setBattery(r.battery);
      setHist(r.history);
      setSrc(r.source);
      setMockScale(r.battery.power_mw);
      setReady(true);
    });
  }, []);

  async function onSave(b: Battery) {
    setSaving(true);
    const r = await saveBattery(battery, b);
    setBattery(b);
    setHist(r.history);
    setSrc(r.source);
    setMockScale(b.power_mw);
    setSaving(false);
    setModal(null);
    window.dispatchEvent(new CustomEvent("grid:replan"));
  }

  return (
    <div className="page clean">
      <header className="masthead">
        <a className="wordmark" href="/">GRID</a>
        <nav>
          {MOCK && <span className="tag">mock</span>}
          <a href="/" className="active">Trade</a>
          <a href="/backend">Backend</a>
        </nav>
      </header>

      <h1 className="headline">Ask your battery trader.</h1>
      <StatusLine battery={battery} onEdit={() => setModal("battery")} />

      {ready && <AskTrader battery={{ power_mw: battery.power_mw, energy_mwh: battery.energy_mwh, reserve_pct: battery.reserve_pct, zone: battery.zone }} />}

      <footer className="foot">
        <button className="inline" onClick={() => setModal("how")}>How it works</button>
      </footer>

      {modal === "battery" && (
        <Modal title="My battery" onClose={() => setModal(null)}>
          <BatteryPanel battery={battery} history={hist} source={src} saving={saving} onSave={onSave} />
        </Modal>
      )}
      {modal === "how" && (
        <Modal title="How it works" onClose={() => setModal(null)}>
          <div className="how">
            <p>Every day NYISO publishes tomorrow's hourly prices for each zone. The trader solves a linear program that buys when power is cheap and sells when it is expensive, inside your battery's limits.</p>
            <p>Each plan is simulated in a Daytona sandbox before you see it: fill stays within your reserve and 90%, at most your cycle limit, never buy and sell in the same hour, ends at least 50% full. A risk agent can veto and ask for a re-plan.</p>
            <p>Profit = energy sold x price minus energy bought x price minus $8/MWh battery wear. The 30-day history plans on day-ahead prices and settles on what real-time prices actually were. Prices: NYISO public LBMP (mis.nyiso.com). Memory of your rules and past plans: SurrealDB, with valid time.</p>
            <p>Nothing is traded until you approve. Bids are submitted via a NYISO market participant, your utility, or an aggregator.</p>
          </div>
        </Modal>
      )}
    </div>
  );
}
