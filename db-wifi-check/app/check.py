#!/usr/bin/env python3
"""
DB-Wifi Check  (v0.3)
---------------------
Prueft in einem festen Intervall die Verbindung zu einem oder mehreren
DNS-Servern, misst die Latenz und stellt Erreichbarkeit + Latenz dar -- als
ASCII-Grafik im Log und als schickes Web-Dashboard.

Statt ICMP-Ping (braucht die Capability NET_RAW, die unter der
"restricted"-PodSecurity-Policy verboten ist) wird ein TCP-Connect zum
DNS-Port 53 gemessen. Das ergibt dieselbe Aussage -- erreichbar? wie
schnell? -- kommt aber ohne Sonderrechte aus (laeuft als Nicht-Root).

Neu in v0.3:
  * mehrere Ziele gleichzeitig (TARGETS), im Dashboard als Tabs
  * persistenter Verlauf: wird auf ein Volume (DATA_DIR) geschrieben und beim
    Start wieder geladen -- ueberlebt Pod-Neustarts. Faellt sauber auf
    "nur im Speicher" zurueck, wenn das Verzeichnis nicht schreibbar ist.
  * Knopf "Verlauf loeschen" je Ziel  ->  /clear

Aus v0.2: modernes Dashboard, Live-Countdown, Knopf fuer sofortige Messung,
PDF-Export (Druckansicht), JSON-Endpunkt /data.

Endpunkte:
  /            -> HTML-Dashboard (JS holt /data; Tabs, Countdown, Refresh, PDF)
  /data        -> JSON mit allen Zielen, Messwerten + Countdown
  /probe       -> loest sofort eine Messung aller Ziele aus, liefert das JSON
  /clear       -> loescht den Verlauf (?target=host:port fuer ein Ziel, sonst alle)
  /raw         -> reiner ASCII-Text (curl-freundlich)
  /healthz     -> Liveness-/Readiness-Probe

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
from urllib.parse import urlparse, parse_qs

VERSION = "0.3"

# --- Konfiguration ----------------------------------------------------------
PROBE_PORT = int(os.getenv("PROBE_PORT", "53"))     # Default-Port (DNS)
INTERVAL = int(os.getenv("INTERVAL_SECONDS", "30"))
TIMEOUT = float(os.getenv("TIMEOUT_SECONDS", "2"))
HISTORY = int(os.getenv("HISTORY", "20"))           # gespeicherte Messungen je Ziel
BAR_WIDTH = int(os.getenv("BAR_WIDTH", "40"))       # Breite der ASCII-Balken
MAX_MS = float(os.getenv("MAX_MS", "100"))          # ms = volle Balkenbreite
PORT = int(os.getenv("PORT", "8080"))
DATA_DIR = os.getenv("DATA_DIR", "/data")           # Ablage fuer persistenten Verlauf
STATE_FILE = os.path.join(DATA_DIR, "history.json")


def parse_targets():
    """
    Liest die Ziele aus der Umgebung.
      TARGETS = "8.8.8.8:53, 1.1.1.1:53, dns.intern"   (Komma-separiert)
    Ohne Port wird PROBE_PORT genommen. Faellt auf TARGET:PROBE_PORT zurueck.
    Rueckgabe: Liste (host, port, key) -- key == "host:port".
    """
    raw = os.getenv("TARGETS", "").strip()
    if not raw:
        raw = os.getenv("TARGET", "8.8.8.8").strip()
    result = []
    seen = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            host, _, p = part.rpartition(":")
            host = host.strip()
            try:
                port = int(p)
            except ValueError:
                host, port = part, PROBE_PORT
        else:
            host, port = part, PROBE_PORT
        key = f"{host}:{port}"
        if key not in seen:
            seen.add(key)
            result.append((host, port, key))
    return result


TARGETS = parse_targets()

# Je Ziel eine History mit (epoch_seconds: float, ms: float | None)
histories = {key: deque(maxlen=HISTORY) for (_, _, key) in TARGETS}
_lock = threading.Lock()
_next_probe_at = time.monotonic() + INTERVAL
_persist_ok = False  # wird beim Start bestimmt
SPARK = "▁▂▃▄▅▆▇█"


# --- Persistenz -------------------------------------------------------------
def init_persistence():
    """Prueft, ob DATA_DIR beschreibbar ist, und laedt vorhandenen Verlauf."""
    global _persist_ok
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        probe = os.path.join(DATA_DIR, ".write-test")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        _persist_ok = True
    except OSError as e:
        _persist_ok = False
        print(f"[persist] {DATA_DIR} nicht beschreibbar ({e}) "
              f"-> Verlauf nur im Speicher", flush=True)
        return

    load_state()
    print(f"[persist] aktiv -> {STATE_FILE}", flush=True)


def load_state():
    """Laedt gespeicherten Verlauf in die passenden Histories (falls vorhanden)."""
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return
    with _lock:
        for key, points in data.items():
            if key not in histories:
                continue  # Ziel nicht mehr konfiguriert -> ignorieren
            dq = histories[key]
            for entry in points[-HISTORY:]:
                try:
                    ts, ms = entry
                except (ValueError, TypeError):
                    continue
                dq.append((ts, None if ms is None else float(ms)))


def save_state():
    """Schreibt den aktuellen Verlauf atomar nach STATE_FILE (falls persistent)."""
    if not _persist_ok:
        return
    with _lock:
        snapshot = {key: list(dq) for key, dq in histories.items()}
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(snapshot, f)
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        print(f"[persist] Schreiben fehlgeschlagen: {e}", flush=True)


# --- Messung ----------------------------------------------------------------
def probe_once(host, port):
    """Ein TCP-Connect zu host:port. Latenz in ms, oder None bei Timeout."""
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT):
            pass
    except OSError:
        return None
    return (time.perf_counter() - start) * 1000.0


def probe_targets(source="auto"):
    """Misst alle Ziele einmal, haengt die Ergebnisse an und speichert."""
    for host, port, key in TARGETS:
        ms = probe_once(host, port)
        ts = time.time()
        with _lock:
            histories[key].append((ts, ms))
    save_state()
    print(f"\n[{source}] {datetime.now(timezone.utc):%H:%M:%S} UTC\n"
          + render() + "\n", flush=True)


# --- Auswertung / Formatierung ---------------------------------------------
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
    """Kennzahlen aus einer Liste von (ts, ms)-Tupeln."""
    values = [ms for _, ms in hist if ms is not None]
    last = hist[-1][1] if hist else None
    avg = sum(values) / len(values) if values else 0.0
    lo = min(values) if values else 0.0
    hi = max(values) if values else 0.0
    losses = sum(1 for _, ms in hist if ms is None)
    loss_pct = losses / len(hist) * 100 if hist else 0.0
    return last, avg, lo, hi, losses, loss_pct


def render_one(host, port, key):
    """ASCII-Block fuer ein Ziel."""
    with _lock:
        hist = list(histories[key])
    L = []
    L.append("+" + "-" * 62 + "+")
    L.append("|  DB-WIFI CHECK  ->  {:<40}|".format(f"{key} (DNS)"))
    L.append("+" + "-" * 62 + "+")
    if not hist:
        L.append("|  {:<60}|".format("noch keine Messung ..."))
        L.append("+" + "-" * 62 + "+")
        return "\n".join(L)

    last, avg, lo, hi, losses, loss_pct = _stats(hist)
    label, dot, _ = status_label(last)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    L.append("|  Zeit   : {:<51}|".format(now))
    L.append("|  Status : {} {:<48}|".format(dot, label))
    cur = "n/a" if last is None else f"{last:.1f} ms"
    L.append("|  Latenz : {:<51}|".format(cur))
    L.append("|  Schnitt: {:<51}|".format(f"{avg:.1f} ms  (min {lo:.1f} / max {hi:.1f})"))
    L.append("|  Verlust: {:<51}|".format(f"{loss_pct:.0f} %  ({losses}/{len(hist)})"))
    L.append("+" + "-" * 62 + "+")
    spark = "".join(sparkchar(ms) for _, ms in hist)
    L.append("|  Verlauf: {:<51}|".format(spark))
    L.append("+" + "-" * 62 + "+")
    return "\n".join(L)


def render():
    """ASCII-Dashboard fuer Log + /raw -- alle Ziele untereinander."""
    if not TARGETS:
        return "DB-Wifi Check: keine Ziele konfiguriert."
    persist = "persistent" if _persist_ok else "nur im Speicher"
    head = f"DB-Wifi Check v{VERSION}  ({len(TARGETS)} Ziel(e), Verlauf: {persist})"
    blocks = [render_one(h, p, k) for (h, p, k) in TARGETS]
    return head + "\n" + "\n".join(blocks)


def target_payload(host, port, key):
    with _lock:
        hist = list(histories[key])
    last, avg, lo, hi, losses, loss_pct = _stats(hist)
    label, _, skey = status_label(last)
    now_epoch = time.time()
    points = [{"ms": None if ms is None else round(ms, 1),
               "ago": max(0, round(now_epoch - ts))} for ts, ms in hist]
    return {
        "key": key, "host": host, "port": port,
        "current_ms": None if last is None else round(last, 1),
        "status": label, "status_key": skey,
        "avg": round(avg, 1), "min": round(lo, 1), "max": round(hi, 1),
        "loss_pct": round(loss_pct), "losses": losses, "count": len(hist),
        "history": points,
    }


def data_payload():
    with _lock:
        next_at = _next_probe_at
    next_in = max(0, round(next_at - time.monotonic()))
    return {
        "version": VERSION,
        "interval": INTERVAL,
        "max_ms": MAX_MS,
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "next_probe_in": next_in,
        "persist": _persist_ok,
        "targets": [target_payload(h, p, k) for (h, p, k) in TARGETS],
    }


def clear_history(target=None):
    """Loescht den Verlauf eines Ziels (target=key) oder aller Ziele."""
    with _lock:
        if target and target in histories:
            histories[target].clear()
        else:
            for dq in histories.values():
                dq.clear()
    save_state()


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
  header{display:flex;align-items:center;gap:14px;margin-bottom:18px;flex-wrap:wrap;}
  .logo{background:var(--db);color:#fff;font-weight:800;letter-spacing:.5px;
        padding:6px 10px;border-radius:8px;font-size:15px;}
  h1{font-size:20px;margin:0;font-weight:700;}
  .ver{color:var(--muted);font-size:12px;border:1px solid var(--line);
       padding:2px 8px;border-radius:999px;}
  .persist{margin-left:auto;font-size:12px;color:var(--muted);display:flex;align-items:center;gap:6px;}
  .persist .pill{width:8px;height:8px;border-radius:50%;background:var(--great);}
  .persist.off .pill{background:var(--slow);}
  .tabs{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px;}
  .tab{border:1px solid var(--line);background:#0c121a;color:var(--muted);
       padding:8px 14px;border-radius:999px;cursor:pointer;font:inherit;font-size:13px;
       display:flex;align-items:center;gap:8px;transition:border-color .2s,color .2s;}
  .tab:hover{border-color:#33455a;}
  .tab.active{color:var(--ink);border-color:var(--db);background:rgba(236,0,22,.08);}
  .tab .tdot{width:9px;height:9px;border-radius:50%;background:#444;flex:0 0 auto;}
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
  .count .cap{color:var(--muted);font-size:12px;text-align:center;}
  .chartcard{margin-top:16px;}
  .chartcard h2{font-size:13px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;
                margin:0 0 12px;font-weight:600;}
  svg{width:100%;height:150px;display:block;}
  .axis{fill:var(--muted);font-size:10px;font-family:ui-monospace,monospace;}
  .actions{display:flex;gap:10px;margin-top:18px;flex-wrap:wrap;}
  button{font:inherit;font-weight:600;cursor:pointer;border-radius:10px;padding:11px 16px;
         border:1px solid var(--line);background:#0c121a;color:var(--ink);display:inline-flex;
         align-items:center;gap:8px;transition:transform .05s ease,border-color .2s,background .2s;}
  button:hover{border-color:#33455a;}
  button:active{transform:translateY(1px);}
  button.primary{background:var(--db);border-color:var(--db);color:#fff;}
  button.primary:hover{background:#ff1a2e;}
  button.danger{color:#ffb3b8;border-color:#5a2a2f;}
  button.danger:hover{border-color:var(--db);background:rgba(236,0,22,.1);}
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
    .actions,.count .cap,.tab:not(.active){display:none;}
    .tile,button{background:#f4f4f4;color:#000;}
  }
</style></head>
<body><div class="wrap">
  <header>
    <span class="logo">DB</span>
    <h1>Wifi&nbsp;Check</h1>
    <span class="ver" id="ver">v0.3</span>
    <span class="persist" id="persist" title="Verlauf-Speicherung">
      <span class="pill"></span><span id="persistTxt">…</span>
    </span>
  </header>

  <div class="tabs" id="tabs"></div>

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
      <div class="cap">bis zur nächsten Messung<br>(alle Ziele)</div>
    </div>
  </div>

  <div class="card chartcard">
    <h2>Latenzverlauf · <span id="chartTarget">—</span></h2>
    <svg id="chart" viewBox="0 0 800 150" preserveAspectRatio="none" aria-label="Latenzverlauf"></svg>
    <div class="actions">
      <button class="primary" id="btnRefresh" onclick="manualProbe()">
        <span id="refIco">⟳</span> Jetzt messen
      </button>
      <button id="btnPdf" onclick="window.print()">⭳ Als PDF exportieren</button>
      <button class="danger" id="btnClear" onclick="clearHistory()">🗑 Verlauf löschen</button>
    </div>
    <div class="updated" id="updated"></div>
  </div>

  <div class="foot">
    DB-Wifi Check v<span id="fver">0.3</span> · TCP-Connect zum DNS-Port ·
    <a href="/raw">/raw</a> · <a href="/data">/data</a>
  </div>
</div>

<script>
const COLORS = {great:'#2ecc71',good:'#8bd450',ok:'#f5c451',slow:'#f39c12',timeout:'#ec0016'};
let interval = 30, maxMs = 100, cd = 0, timer = null;
let lastData = null, active = null;

function fmt(x){ return (x===null||x===undefined) ? '–' : x; }

function bandColor(ms){
  if(ms===null) return COLORS.timeout;
  if(ms<20) return COLORS.great;
  if(ms<50) return COLORS.good;
  if(ms<100) return COLORS.ok;
  return COLORS.slow;
}

function currentTarget(){
  if(!lastData) return null;
  return lastData.targets.find(t=>t.key===active) || lastData.targets[0] || null;
}

function drawChart(hist){
  const W=800, H=150, pad=6, n=hist.length;
  const svg=document.getElementById('chart');
  if(!n){ svg.innerHTML='<text class="axis" x="12" y="80">noch keine Messwerte</text>'; return; }
  const gap=4, bw=(W-2*pad-(n-1)*gap)/n;
  const cap=maxMs*1.5;
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
  s+=`<line x1="${pad}" y1="${H-18}" x2="${W-pad}" y2="${H-18}" stroke="#1e2a38"/>`;
  s+=`<text class="axis" x="${W-pad}" y="${H-4}" text-anchor="end">jetzt →</text>`;
  s+=`<text class="axis" x="${pad}" y="${H-4}">← älter</text>`;
  svg.innerHTML=s;
}

function renderTabs(){
  const box=document.getElementById('tabs');
  box.innerHTML='';
  if(!lastData) return;
  lastData.targets.forEach(t=>{
    const b=document.createElement('button');
    b.className='tab'+(t.key===active?' active':'');
    b.onclick=()=>{ active=t.key; renderTabs(); renderActive(); };
    const col=COLORS[t.status_key]||'#444';
    b.innerHTML=`<span class="tdot" style="background:${col}"></span>${t.key}`;
    box.appendChild(b);
  });
}

function renderActive(){
  const d=currentTarget();
  if(!d) return;
  const col = COLORS[d.status_key] || '#333';
  const dot=document.getElementById('dot');
  dot.style.background=col; dot.style.color=col;
  document.getElementById('statuslbl').textContent=d.status;
  document.getElementById('statuslbl').style.color=col;
  document.getElementById('statussub').textContent =
     d.current_ms===null ? 'keine Antwort' : ('Latenz zu '+d.key);
  document.getElementById('cur').textContent = fmt(d.current_ms);
  document.getElementById('avg').textContent = d.avg+' ms';
  document.getElementById('loss').textContent = d.loss_pct+' %';
  document.getElementById('min').textContent = d.min+' ms';
  document.getElementById('max').textContent = d.max+' ms';
  document.getElementById('chartTarget').textContent = d.key;
  drawChart(d.history);
}

function applyData(d){
  lastData=d; interval=d.interval; maxMs=d.max_ms;
  if(!active || !d.targets.some(t=>t.key===active))
    active = d.targets.length ? d.targets[0].key : null;
  document.getElementById('ver').textContent='v'+d.version;
  document.getElementById('fver').textContent=d.version;
  const pe=document.getElementById('persist');
  pe.classList.toggle('off', !d.persist);
  document.getElementById('persistTxt').textContent =
     d.persist ? 'Verlauf: dauerhaft gespeichert' : 'Verlauf: nur im Speicher';
  document.getElementById('updated').textContent='Stand: '+d.time;
  renderTabs(); renderActive(); startCountdown(d.next_probe_in);
}

function tick(){
  cd = Math.max(0, cd-1);
  document.getElementById('cd').textContent = cd;
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
    applyData(await r.json());
  }catch(e){
    document.getElementById('statussub').textContent='Verbindung zum Server verloren…';
    setTimeout(load, 3000);
  }
}

async function post(url){
  const r = await fetch(url, {method:'POST', cache:'no-store'});
  return r.json();
}

async function manualProbe(){
  const btn=document.getElementById('btnRefresh');
  const ico=document.getElementById('refIco');
  btn.disabled=true; ico.classList.add('spin');
  try{ applyData(await post('/probe')); }catch(e){}
  finally{ btn.disabled=false; ico.classList.remove('spin'); }
}

async function clearHistory(){
  const d=currentTarget();
  if(!d) return;
  if(!confirm('Verlauf für '+d.key+' wirklich löschen?')) return;
  try{ applyData(await post('/clear?target='+encodeURIComponent(d.key))); }catch(e){}
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

    def _query(self):
        return parse_qs(urlparse(self.path).query)

    def _route(self):
        path = urlparse(self.path).path
        if path.startswith("/healthz"):
            self._send(200, "ok")
        elif path.startswith("/data"):
            self._json(data_payload())
        elif path.startswith("/probe"):
            probe_targets(source="manual")
            self._json(data_payload())
        elif path.startswith("/clear"):
            target = self._query().get("target", [None])[0]
            clear_history(target)
            self._json(data_payload())
        elif path.startswith("/raw"):
            self._send(200, render() + "\n")
        else:
            self._send(200, HTML, "text/html; charset=utf-8")

    def do_GET(self):
        self._route()

    def do_POST(self):
        self._route()

    def log_message(self, *args):
        pass


def checker_loop():
    global _next_probe_at
    while True:
        probe_targets(source="auto")
        with _lock:
            _next_probe_at = time.monotonic() + INTERVAL
        time.sleep(INTERVAL)


def main():
    names = ", ".join(k for (_, _, k) in TARGETS) or "(keine)"
    print(f"DB-Wifi Check v{VERSION}: Ziele={names}, "
          f"Intervall={INTERVAL}s, HTTP-Port={PORT}", flush=True)
    init_persistence()
    threading.Thread(target=checker_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
