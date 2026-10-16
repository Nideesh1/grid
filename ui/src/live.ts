// Real-time NYISO zone prices. Order of sources: backend GET /api/zones (if present),
// then the public NYISO realtime_zone CSV through the Vite /nyiso proxy, then mock.
/* eslint-disable @typescript-eslint/no-explicit-any */
import { API, MOCK } from "./api";

export const ZONES = ["WEST", "GENESE", "CENTRL", "NORTH", "MHK VL", "CAPITL", "HUD VL", "MILLWD", "DUNWOD", "N.Y.C.", "LONGIL"] as const;
export type ZonePrice = { name: string; usd_mwh: number };
export type Live = {
  ts: string; // latest interval, NY local "MM/DD/YYYY HH:MM:SS" or ISO
  zones: ZonePrice[];
  nycToday: { ts: string; usd_mwh: number }[]; // 5-min N.Y.C. series today
  source: "nyiso" | "mock";
};

function nyDate(d = new Date()): string {
  const p = new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit" }).format(d);
  return p.replace(/-/g, "");
}

export function parseRealtimeCsv(text: string): Omit<Live, "source"> | null {
  const rows = text.trim().split(/\r?\n/).slice(1).map((l) => l.split(",").map((c) => c.replace(/^"|"$/g, "")));
  const good = rows.filter((r) => r.length >= 4 && isFinite(+r[3]));
  if (!good.length) return null;
  const last = good[good.length - 1][0];
  const zones = good.filter((r) => r[0] === last && (ZONES as readonly string[]).includes(r[1])).map((r) => ({ name: r[1], usd_mwh: +r[3] }));
  const nycToday = good.filter((r) => r[1] === "N.Y.C.").map((r) => ({ ts: r[0], usd_mwh: +r[3] }));
  return { ts: last, zones, nycToday };
}

const MOCK_ZONES: Record<string, number> = { WEST: 23.8, GENESE: 24.1, CENTRL: 25.0, NORTH: 22.9, "MHK VL": 26.2, CAPITL: 33.4, "HUD VL": 36.2, MILLWD: 36.6, DUNWOD: 37.0, "N.Y.C.": 42.0, LONGIL: 62.4 };

export async function getLive(): Promise<Live> {
  if (!MOCK) {
    let zones: ZonePrice[] = [];
    let ts = "";
    try {
      const r = await fetch(`${API}/api/prices/zones`);
      if (r.ok) {
        const j: any = await r.json();
        zones = (j.zones ?? [])
          .map((z: any) => ({ name: z.zone ?? z.name, usd_mwh: +(z.usd_mwh ?? z.price) }))
          .filter((z: ZonePrice) => (ZONES as readonly string[]).includes(z.name));
        ts = j.zones?.[0]?.ts ?? j.ts ?? "";
      }
    } catch {
      /* fall through */
    }
    // 5-min N.Y.C. series for today straight from the public CSV (via the dev proxy).
    let nycToday: Live["nycToday"] = [];
    try {
      const r = await fetch(`/nyiso/public/csv/realtime/${nyDate()}realtime_zone.csv`);
      if (r.ok) {
        const p = parseRealtimeCsv(await r.text());
        if (p) {
          nycToday = p.nycToday;
          if (!zones.length) {
            zones = p.zones;
            ts = p.ts;
          }
        }
      }
    } catch {
      /* fall through */
    }
    if (!nycToday.length) {
      try {
        const j: any = await (await fetch(`${API}/api/prices`)).json();
        nycToday = (j.real_time_today ?? []).map((p: any) => ({ ts: p.ts, usd_mwh: +p.usd_mwh }));
      } catch {
        /* none */
      }
    }
    if (zones.length) return { ts, zones, nycToday, source: "nyiso" };
  }
  const nycToday = Array.from({ length: 186 }, (_, i) => {
    const h = i / 12;
    return { ts: `${String(Math.floor(h)).padStart(2, "0")}:${String((i % 12) * 5).padStart(2, "0")}`, usd_mwh: 30 + 8 * Math.sin((h - 9) / 3) + (i % 7) * 0.8 };
  });
  return { ts: "10/16/2026 15:30:00", zones: ZONES.map((n) => ({ name: n, usd_mwh: MOCK_ZONES[n] })), nycToday, source: "mock" };
}
