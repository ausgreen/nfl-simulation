// What every page uses, as the global `nfl`; hosted/pages.py inlines this file into each page.
const nfl = (() => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const pct = (x, d = 0) => x == null ? "—" : (x * 100).toFixed(d) + "%";
  const cents = (x) => x == null ? "—" : `${Math.round(x * 100)}¢`;
  const ago = (s) => s < 120 ? `${Math.round(s)} s` : s < 7200 ? `${Math.round(s / 60)} min` : s < 172800 ? `${Math.round(s / 3600)} h` : `${Math.round(s / 86400)} days`;
  const kickoff = (t) => new Date(t).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" });
  const store = { get(k) { try { return localStorage.getItem(k); } catch { return null; } },
                  set(k, v) { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch {} } };
  const getJSON = async (path, missing) => {  // missing: what to say if the file isn't there
    const r = await fetch(path, { cache: "no-cache" });
    if (!r.ok) throw new Error(missing && r.status === 404 ? missing : r.statusText);
    return r.json();
  };

  // team, then last name: how a QB is matched to his Kalshi market
  const nameKey = (n) => String(n || "").toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "")
    .replace(/\b(jr|sr|ii|iii|iv)\b\.?/g, "").replace(/[^a-z ]/g, " ").trim().split(/\s+/).pop();

  // a cue when new numbers arrive: what changed flashes, and the dot in the tab bar pulses
  const seen = new Map();
  const restart = (el) => { el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash"); };
  function cue(...selectors) {
    let changed = false;
    for (const sel of selectors) document.querySelectorAll(sel).forEach((el, i) => {
      const key = `${sel}|${el.dataset.key || i}`, html = el.dataset.cue ?? el.innerHTML;
      if (seen.has(key) && seen.get(key) !== html) { restart(el); changed = true; }
      seen.set(key, html);
    });
    if (changed) restart($("beat"));
  }

  // pushed updates from the worker (Server-Sent Events) when it offers them; the pages poll only while none arrive
  let stream = null, streamAt = 0;
  function listen(paths, onFile) {
    if (stream) stream.close();
    stream = null;
    streamAt = 0;
    if (!window.EventSource) return;
    stream = new EventSource(`events?only=${encodeURIComponent(paths.join(","))}`);
    stream.addEventListener("file", (e) => { streamAt = Date.now(); const f = JSON.parse(e.data); onFile(f.path, f.body); });
    stream.addEventListener("ping", () => { streamAt = Date.now(); });
  }
  const streaming = () => Date.now() - streamAt < 40000;  // the worker pings every 15 s

  // the model's chance of a Kalshi score market, from its simulated points (a state's or card's "scores").
  // chance of more than x: [lowest value, counts from there up, games simulated] (older states: chances, no total)
  const above = (pmf, x) => pmf[1].reduce((s, p, i) => pmf[0] + i > x ? s + p : s, 0) / (pmf[2] || 1);
  function scoreChance(d, m, s = d.scores && d.scores[m.part]) {
    if (!s) return null;
    const home = m.team === d.home, tie = above(s.margin, -0.5) - above(s.margin, 0.5);
    if (m.kind === "win") return m.team === "TIE" ? tie
      : (home ? above(s.margin, 0.5) : 1 - above(s.margin, -0.5)) + (m.part === "game" ? tie / 2 : 0);  // a tied game pays half
    if (m.kind === "spread") return home ? above(s.margin, m.x) : 1 - above(s.margin, -m.x);
    if (m.kind === "total") return above(s.total, m.x);
    return above(home ? s.home : s.away, m.x);
  }
  // the chance's likely range: priced with both QBs' form at its low and at its high end (spreads and winners:
  // one QB high and the other low), as the passing-yard dots are
  function scoreRange(d, m) {
    const s = d.scores && d.scores[m.part];
    if (!s || !s.margin_band) return null;
    const at = (i) => ({ margin: s.margin_band[i], total: s.total_band[i],
                         home: s.home_band && s.home_band[i], away: s.away_band && s.away_band[i] });
    const a = scoreChance(d, m, at(0)), b = scoreChance(d, m, at(1));
    return [Math.min(a, b), Math.max(a, b)];
  }
  const marketLabel = (m) => {
    const n = m.x == null ? "" : Math.round(m.x + 0.5);
    return m.kind === "win" ? (m.team === "TIE" ? "Tie" : `${m.team} wins`) : m.kind === "spread" ? `${m.team} by ${n}+`
      : m.kind === "total" ? `${n}+ points` : `${m.team} scores ${n}+`;
  };

  // the viewer's watch list, kept in this browser only (the Watch list page reads it): [{t: ticker, ev, type, ...}]
  const WATCH_KEY = "nflsim:watch";
  const watchStore = {
    all() { try { return JSON.parse(localStorage.getItem(WATCH_KEY)) || []; } catch { return []; } },
    save(list) { try { localStorage.setItem(WATCH_KEY, JSON.stringify(list)); } catch {} },
    has(t) { return this.all().some((e) => e.t === t); },
    toggle(entry) {
      const list = this.all(), i = list.findIndex((e) => e.t === entry.t);
      if (i >= 0) list.splice(i, 1); else list.push({ side: "yes", ...entry, n: null, paid: null, added: Date.now() });
      this.save(list);
    },
  };
  const watchButton = (entry) => entry.t ? `<button type="button" class="watch" data-entry="${esc(JSON.stringify(entry))}" aria-pressed="${watchStore.has(entry.t)}">${watchStore.has(entry.t) ? "Watching" : "Watch"}</button>` : "";

  return { $, esc, pct, cents, ago, kickoff, store, getJSON, nameKey, seen, restart, cue, listen, streaming, above, scoreChance,
           scoreRange, marketLabel, WATCH_KEY, watchStore, watchButton };
})();
