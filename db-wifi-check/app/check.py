#!/usr/bin/env python3
"""
DB-Wifi Check  (v0.2)
---------------------
Prueft in einem festen Intervall die Verbindung zu einem DNS-Server, misst die
Latenz und stellt Erreichbarkeit + Latenz dar -- als ASCII-Grafik im Log und als
schickes Web-Dashboard.

Statt ICMP-Ping (braucht die Capability NET_RAW, die unter der
"restricted"-PodSecurity-Policy verboten ist) wird ein TCP-Connect zum
DNS-Port 53 gemessen. Das ergibt dieselbe Aussage -- erreichbar? wie
schnell? -- kommt aber ohne Sonderrechte aus (laeuft als Nicht-Root).

Neu in v0.2:
  * modernes Web-Dashboard (Karten, SVG-Verlaufsdiagramm, DB-Rot-Akzent)
  * Live-Countdown bis zur naechsten automatischen Messung
  * Knopf fuer sofortige (haendische) Messung  -> POST/GET /probe
  * PDF-Export direkt aus dem Browser (Druckansicht, ohne Zusatz-Tools)
  * JSON-Endpunkt /data fuer das Frontend (und eigene Skripte)

Endpunkte:
  /         -> HTML-Dashboard (JS holt /data, kein Full-Page-Reload mehr)
  /data     -> JSON mit Messwerten + Countdown
  /probe    -> loest sofort eine Messung aus, liefert das frische JSON
  /raw      -> reiner ASCII-Text (curl-freundlich)
  /healthz  -> Liveness-/Readiness-Probe

Nur Python-Standardbibliothek, keine Abhaengigkeiten.
"""

import json
import os
import socket
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.2"

TARGET = os.getenv("TARGET", "8.8.8.8")
PROBE_PORT = int(os.getenv("PROBE_PORT", "53"))     # DNS-Port
INTERVAL = int(os.getenv("INTERVAL_SECONDS", "30"))
TIMEOUT = float(os.getenv("TIMEOUT_SECONDS", "2"))
HISTORY = int(os.getenv("HISTORY", "20"))           # gespeicherte Messungen
BAR_WIDTH = int(os.getenv("BAR_WIDTH", "40"))       # Breite der ASCII-Balken
MAX_MS = float(os.getenv("MAX_MS", "100"))          # ms = volle Balkenbreite
PORT = int(os.getenv("PORT", "8080"))

# Jede Messung: (epoch_seconds: float, ms: float | None)
history = deque(maxlen=HISTORY)
_lock = threading.Lock()
_next_probe_at = time.monotonic() + INTERVAL  # monotone Zeit der naechsten Auto-Messung
SPARK = "▁▂▃▄▅▆▇█"


def probe_once():
    """Ein TCP-Connect zu TARGET:PROBE_PORT. Latenz in ms, oder None bei Timeout."""
    start = time.perf_counter()
    try:
        with socket.create_connection((TARGET, PROBE_PORT), timeout=TIMEOUT):
            pass
    except OSError:
        return None
    return (time.perf_counter() - start) * 1000.0


def do_probe(source="auto"):
    """Misst einmal und haengt das Ergebnis (mit Zeitstempel) an die History."""
    ms = probe_once()
    ts = time.time()
    with _lock:
        history.append((ts, ms))
    tag = "" if source == "auto" else f" ({source})"
    print(f"\n[{source}]{tag}\n" + render() + "\n", flush=True)
    return ms


def bar(ms):
    if ms is None:
        return "X" * BAR_WIDTH
    filled = min(BAR_WIDTH, max(1, round(ms / MAX_MS * BAR_WIDTH)))
    return "#" * filled + "-" * (BAR_WIDTH - filled)


def sparkchar(ms):
    if ms is None:
        return "!"
    idx = int(min(len(SPARK) - 1, ms / MAX_MS * (len(SPARK) - 1)))
    return SPARK[idx]


def status_label(ms):
    """Gibt (Text, ASCII-Punkt, Schluessel) zurueck."""
    if ms is None:
        return "TIMEOUT", "[!]", "timeout"
    if ms < 20:
        return "SEHR GUT", "[+]", "great"
    if ms < 50:
        return "GUT", "[+]", "good"
    if ms < 100:
        return "OK", "[o]", "ok"
    return "LANGSAM", "[~]", "slow"


def _stats(hist):
    """Berechnet Kennzahlen aus einer Liste von (ts, ms)-Tupeln."""
    values = [ms for _, ms in hist if ms is not None]
    last = hist[-1][1] if hist else None
    avg = sum(values) / len(values) if values else 0.0
    lo = min(values) if values else 0.0
    hi = max(values) if values else 0.0
    losses = sum(1 for _, ms in hist if ms is None)
    loss_pct = losses / len(hist) * 100 if hist else 0.0
    return last, avg, lo, hi, losses, loss_pct


def render():
    """Baut den ASCII-Dashboard-Block als Text (Log + /raw)."""
    with _lock:
        hist = list(history)
    if not hist:
        return "DB-Wifi Check startet ... erste Messung laeuft."

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    last, avg, lo, hi, losses, loss_pct = _stats(hist)
    label, dot, _ = status_label(last)

    L = []
    L.append("+" + "-" * 62 + "+")
    L.append("|  DB-WIFI CHECK  ->  {:<40}|".format(f"{TARGET}:{PROBE_PORT} (DNS)"))
    L.append("+" + "-" * 62 + "+")
    L.append("|  Zeit   : {:<51}|".format(now))
    L.append("|  Status : {} {:<48}|".format(dot, label))
    cur = "n/a" if last is None else f"{last:.1f} ms"
    L.append("|  Latenz : {:<51}|".format(cur))
    L.append("|  Schnitt: {:<51}|".format(f"{avg:.1f} ms  (min {lo:.1f} / max {hi:.1f})"))
    L.append("|  Verlust: {:<51}|".format(f"{loss_pct:.0f} %  ({losses}/{len(hist)})"))
    L.append("+" + "-" * 62 + "+")
    L.append("|  Verlauf (0 .. {:>3.0f} ms je Zeichen):{:>24}|".format(MAX_MS, ""))
    spark = "".join(sparkchar(ms) for _, ms in hist)
    L.append("|  {:<60}|".format(spark))
    L.append("+" + "-" * 62 + "+")
    for i, (_, ms) in enumerate(reversed(hist)):
        tag = "jetzt" if i == 0 else f"-{i*INTERVAL}s"
        val = " TO " if ms is None else f"{ms:4.0f}"
        L.append("  {:>6} {} {}ms".format(tag, bar(ms), val))
    return "\n".join(L)


def data_payload():
    """Baut das JSON-Objekt fuer das Frontend."""
    with _lock:
        hist = list(history)
        next_at = _next_probe_at
    now_mono = time.monotonic()
    next_in = max(0, round(next_at - now_mono))

    last, avg, lo, hi, losses, loss_pct = _stats(hist)
    label, _, key = status_label(last)

    points = []
    now_epoch = time.time()
    for ts, ms in hist:
        points.append({
            "ms": None if ms is None else round(ms, 1),
            "ago": max(0, round(now_epoch - ts)),
        })

    return {
        "version": VERSION,
        "target": TARGET,
        "port": PROBE_PORT,
        "interval": INTERVAL,
        "max_ms": MAX_MS,
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "current_ms": None if last is None else round(last, 1),
        "status": label,
        "status_key": key,
        "avg": round(avg, 1),
        "min": round(lo, 1),
        "max": round(hi, 1),
        "loss_pct": round(loss_pct),
        "losses": losses,
        "count": len(hist),
        "next_probe_in": next_in,
        "history": points,
    }


HTML = r"""<!doctype html>
<html lang="de"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DB-Wifi Check</title>
<style>
  :root{
    --bg:#0b0f14; --panel:#121821; --panel2:#0e141c; --line:#1e2a38;
    --ink:#e8eef5; --muted:#7d8ea3; --db:#ec0016;
    --great:#2ecc71; --good:#8bd450; --ok:#f5c451; --slow:#f39c12; --timeout:#ec0016;
  }
  *{box-sizing:border-box;}
  body{margin:0;background:radial-gradient(1200px 600px at 50% -200px,#16202c,var(--bg));
       color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
       min-height:100vh;padding:28px 16px;}
  .wrap{max-width:860px;margin:0 auto;}
  header{display:flex;align-items:center;gap:14px;margin-bottom:22px;flex-wrap:wrap;}
  .logo{background:var(--db);color:#fff;font-weight:800;letter-spacing:.5px;
        padding:6px 10px;border-radius:8px;font-size:15px;}
  h1{font-size:20px;margin:0;font-weight:700;}
  .ver{color:var(--muted);font-size:12px;border:1px solid var(--line);
       padding:2px 8px;border-radius:999px;}
  .target{color:var(--muted);font-size:13px;margin-left:auto;font-family:ui-monospace,Menlo,Consolas,monospace;}
  .grid{display:grid;grid-template-columns:1.3fr 1fr;gap:16px;}
  @media(max-width:680px){.grid{grid-template-columns:1fr;}}
  .card{background:linear-gradient(180deg,var(--panel),var(--panel2));border:1px solid var(--line);
        border-radius:14px;padding:18px;}
  .statusbig{display:flex;align-items:center;gap:16px;}
  .dot{width:52px;height:52px;border-radius:50%;flex:0 0 auto;position:relative;
       box-shadow:0 0 0 6px rgba(255,255,255,.03);}
  .dot::after{content:"";position:absolute;inset:-6px;border-radius:50%;
              border:2px solid currentColor;opacity:.35;animation:pulse 2s ease-out infinite;}
  @keyframes pulse{0%{transform:scale(.9);opacity:.5}100%{transform:scale(1.5);opacity:0}}
  .statustext .lbl{font-size:24px;font-weight:800;line-height:1.1;}
  .statustext .sub{color:var(--muted);font-size:13px;margin-top:2px;}
  .big{font-size:40px;font-weight:800;font-variant-numeric:tabular-nums;}
  .big small{font-size:16px;color:var(--muted);font-weight:600;margin-left:4px;}
  .tiles{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin-top:16px;}
  .tile{background:#0c121a;border:1px solid var(--line);border-radius:10px;padding:10px 12px;}
  .tile .k{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.5px;}
  .tile .v{font-size:18px;font-weight:700;font-variant-numeric:tabular-nums;margin-top:2px;}
  .count{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px;}
  .ring{--p:0;width:130px;height:130px;border-radius:50%;display:grid;place-items:center;
        background:conic-gradient(var(--db) calc(var(--p)*1%),#1a2330 0);}
  .ring .inner{width:104px;height:104px;border-radius:50%;background:var(--panel);
        display:flex;flex-direction:column;align-items:center;justify-content:center;}
  .ring .num{font-size:34px;font-weight:800;font-variant-numeric:tabular-nums;}
  .ring .unit{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:1px;}
  .count .cap{color:var(--muted);font-size:12px;}
  .chartcard{margin-top:16px;}
  .chartcard h2{font-size:13px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;
                margin:0 0 12px;font-weight:600;}
  svg{width:100%;height:150px;display:block;}
  .bars rect{transition:height .3s ease,y .3s ease;}
  .axis{fill:var(--muted);font-size:10px;font-family:ui-monospace,monospace;}
  .actions{display:flex;gap:10px;margin-top:18px;flex-wrap:wrap;}
  button{font:inherit;font-weight:600;cursor:pointer;border-radius:10px;padding:11px 16px;
         border:1px solid var(--line);background:#0c121a;color:var(--ink);display:inline-flex;
         align-items:center;gap:8px;transition:transform .05s ease,border-color .2s,background .2s;}
  button:hover{border-color:#33455a;}
  button:active{transform:translateY(1px);}
  button.primary{background:var(--db);border-color:var(--db);color:#fff;}
  button.primary:hover{background:#ff1a2e;}
  button:disabled{opacity:.55;cursor:progress;}
  .spin{animation:spin 1s linear infinite;}
  @keyframes spin{to{transform:rotate(360deg)}}
  .foot{color:var(--muted);font-size:12px;margin-top:20px;text-align:center;}
  .foot a{color:var(--muted);}
  .updated{color:var(--muted);font-size:12px;margin-top:10px;text-align:right;font-variant-numeric:tabular-nums;}
  @media print{
    body{background:#fff;color:#000;padding:0;}
    .card{border-color:#ccc;background:#fff;}
    .dot::after{animation:none;}
    .actions,.count .cap{display:none;}
    .tile,button{background:#f4f4f4;color:#000;}
    a[href]:after{content:"";}
  }
</style></head>
<body><div class="wrap">
  <header>
    <span class="logo">DB</span>
    <h1>Wifi&nbsp;Check</h1>
    <span class="ver" id="ver">v0.2</span>
    <span class="target" id="target">—</span>
  </header>

  <div class="grid">
    <div class="card">
      <div class="statusbig">
        <div class="dot" id="dot" style="background:#333;color:#333"></div>
        <div class="statustext">
          <div class="lbl" id="statuslbl">…</div>
          <div class="sub" id="statussub">Messung läuft</div>
        </div>
        <div style="margin-left:auto;text-align:right">
          <div class="big"><span id="cur">–</span><small>ms</small></div>
        </div>
      </div>
      <div class="tiles">
        <div class="tile"><div class="k">Schnitt</div><div class="v" id="avg">– ms</div></div>
        <div class="tile"><div class="k">Verlust</div><div class="v" id="loss">– %</div></div>
        <div class="tile"><div class="k">Min</div><div class="v" id="min">– ms</div></div>
        <div class="tile"><div class="k">Max</div><div class="v" id="max">– ms</div></div>
      </div>
    </div>

    <div class="card count">
      <div class="ring" id="ring"><div class="inner">
        <div class="num" id="cd">–</div><div class="unit">Sek.</div>
      </div></div>
      <div class="cap">bis zur nächsten Messung</div>
    </div>
  </div>

  <div class="card chartcard">
    <h2>Latenzverlauf</h2>
    <svg id="chart" viewBox="0 0 800 150" preserveAspectRatio="none" aria-label="Latenzverlauf"></svg>
    <div class="actions">
      <button class="primary" id="btnRefresh" onclick="manualProbe()">
        <span id="refIco">⟳</span> Jetzt messen
      </button>
      <button id="btnPdf" onclick="window.print()">⭳ Als PDF exportieren</button>
    </div>
    <div class="updated" id="updated"></div>
  </div>

  <div class="foot">
    DB-Wifi Check v<span id="fver">0.2</span> · TCP-Connect zu <span id="ftarget">…</span> ·
    <a href="/raw">/raw</a> · <a href="/data">/data</a>
  </div>
</div>

<script>
const COLORS = {great:'#2ecc71',good:'#8bd450',ok:'#f5c451',slow:'#f39c12',timeout:'#ec0016'};
let interval = 30, maxMs = 100, cd = 0, timer = null;

function fmt(x){ return (x===null||x===undefined) ? '–' : x; }

function bandColor(ms){
  if(ms===null) return COLORS.timeout;
  if(ms<20) return COLORS.great;
  if(ms<50) return COLORS.good;
  if(ms<100) return COLORS.ok;
  return COLORS.slow;
}

function drawChart(hist){
  const W=800, H=150, pad=6, n=hist.length;
  const svg=document.getElementById('chart');
  if(!n){ svg.innerHTML=''; return; }
  const gap=4, bw=(W-2*pad-(n-1)*gap)/n;
  const cap=maxMs*1.5;                      // Skala mit etwas Luft nach oben
  let s='';
  hist.forEach((p,i)=>{
    const x=pad+i*(bw+gap);
    const to=p.ms===null;
    const v=to?maxMs:Math.min(p.ms,cap);
    const h=Math.max(3,(v/cap)*(H-24));
    const y=H-18-h;
    const col=bandColor(p.ms);
    s+=`<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${bw.toFixed(1)}" height="${h.toFixed(1)}" rx="2"
         fill="${col}" ${to?'fill-opacity="0.25" stroke="'+col+'" stroke-width="1.5"':''}/>`;
    if(to) s+=`<text class="axis" x="${(x+bw/2).toFixed(1)}" y="${(y-3).toFixed(1)}" text-anchor="middle">TO</text>`;
  });
  // Basislinie + Beschriftung "jetzt"
  s+=`<line x1="${pad}" y1="${H-18}" x2="${W-pad}" y2="${H-18}" stroke="#1e2a38"/>`;
  s+=`<text class="axis" x="${W-pad}" y="${H-4}" text-anchor="end">jetzt →</text>`;
  s+=`<text class="axis" x="${pad}" y="${H-4}">← älter</text>`;
  svg.innerHTML=s;
}

function setStatus(d){
  const col = COLORS[d.status_key] || '#333';
  const dot=document.getElementById('dot');
  dot.style.background=col; dot.style.color=col;
  document.getElementById('statuslbl').textContent=d.status;
  document.getElementById('statuslbl').style.color=col;
  document.getElementById('statussub').textContent =
     d.current_ms===null ? 'keine Antwort' : ('Latenz zu '+d.target);
  document.getElementById('cur').textContent = fmt(d.current_ms);
  document.getElementById('avg').textContent = d.avg+' ms';
  document.getElementById('loss').textContent = d.loss_pct+' %';
  document.getElementById('min').textContent = d.min+' ms';
  document.getElementById('max').textContent = d.max+' ms';
  document.getElementById('target').textContent = d.target+':'+d.port+' (DNS)';
  document.getElementById('ftarget').textContent = d.target+':'+d.port;
  document.getElementById('ver').textContent='v'+d.version;
  document.getElementById('fver').textContent=d.version;
  document.getElementById('updated').textContent='Stand: '+d.time;
}

function tick(){
  cd = Math.max(0, cd-1);
  const el=document.getElementById('cd');
  el.textContent = cd;
  const pct = interval>0 ? (1-cd/interval)*100 : 0;
  document.getElementById('ring').style.setProperty('--p', pct.toFixed(0));
  if(cd<=0){ clearInterval(timer); timer=null; load(); }
}

function startCountdown(sec){
  cd = sec; interval = Math.max(interval, sec, 1);
  if(timer) clearInterval(timer);
  document.getElementById('cd').textContent = cd;
  timer = setInterval(tick, 1000);
}

async function load(){
  try{
    const r = await fetch('/data', {cache:'no-store'});
    const d = await r.json();
    interval = d.interval; maxMs = d.max_ms;
    setStatus(d); drawChart(d.history);
    startCountdown(d.next_probe_in);
  }catch(e){
    document.getElementById('statussub').textContent='Verbindung zum Server verloren…';
    setTimeout(load, 3000);
  }
}

async function manualProbe(){
  const btn=document.getElementById('btnRefresh');
  const ico=document.getElementById('refIco');
  btn.disabled=true; ico.classList.add('spin');
  try{
    const r=await fetch('/probe',{method:'POST',cache:'no-store'});
    const d=await r.json();
    interval=d.interval; maxMs=d.max_ms;
    setStatus(d); drawChart(d.history); startCountdown(d.next_probe_in);
  }catch(e){}
  finally{ btn.disabled=false; ico.classList.remove('spin'); }
}

load();
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="text/plain; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj):
        self._send(200, json.dumps(obj), "application/json; charset=utf-8")

    def do_GET(self):
        if self.path.startswith("/healthz"):
            self._send(200, "ok")
        elif self.path.startswith("/data"):
            self._json(data_payload())
        elif self.path.startswith("/probe"):
            do_probe(source="manual")
            self._json(data_payload())
        elif self.path.startswith("/raw"):
            self._send(200, render() + "\n")
        else:
            self._send(200, HTML, "text/html; charset=utf-8")

    def do_POST(self):
        if self.path.startswith("/probe"):
            do_probe(source="manual")
            self._json(data_payload())
        else:
            self._send(404, "not found")

    def log_message(self, *args):
        pass


def checker_loop():
    global _next_probe_at
    while True:
        do_probe(source="auto")
        with _lock:
            _next_probe_at = time.monotonic() + INTERVAL
        time.sleep(INTERVAL)


def main():
    print(f"DB-Wifi Check v{VERSION}: Ziel={TARGET}:{PROBE_PORT}, "
          f"Intervall={INTERVAL}s, HTTP-Port={PORT}", flush=True)
    threading.Thread(target=checker_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
