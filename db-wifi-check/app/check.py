#!/usr/bin/env python3
"""
DB-Wifi Check
-------------
Prueft alle 30 Sekunden die Verbindung zum DNS-Server 8.8.8.8, misst die
Latenz und stellt Erreichbarkeit + Latenz als ASCII-Grafik dar.

Statt ICMP-Ping (braucht die Capability NET_RAW, die unter der
"restricted"-PodSecurity-Policy verboten ist) wird ein TCP-Connect zum
DNS-Port 53 gemessen. Das ergibt dieselbe Aussage -- erreichbar? wie
schnell? -- kommt aber ohne Sonderrechte aus (laeuft als Nicht-Root).

Ausgaben:
  * stdout (kubectl logs)
  * HTTP-Interface auf Port 8080  (ASCII-Dashboard, Auto-Refresh)
      /        -> HTML-Seite mit ASCII-Grafik
      /raw     -> reiner Text (curl-freundlich)
      /healthz -> Liveness-/Readiness-Probe

Nur Python-Standardbibliothek, keine Abhaengigkeiten.
"""

import os
import socket
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TARGET = os.getenv("TARGET", "8.8.8.8")
PROBE_PORT = int(os.getenv("PROBE_PORT", "53"))     # DNS-Port
INTERVAL = int(os.getenv("INTERVAL_SECONDS", "30"))
TIMEOUT = float(os.getenv("TIMEOUT_SECONDS", "2"))
HISTORY = int(os.getenv("HISTORY", "20"))           # gespeicherte Messungen
BAR_WIDTH = int(os.getenv("BAR_WIDTH", "40"))       # Breite der ASCII-Balken
MAX_MS = float(os.getenv("MAX_MS", "100"))          # ms = volle Balkenbreite
PORT = int(os.getenv("PORT", "8080"))

history = deque(maxlen=HISTORY)  # Werte: float ms  oder  None (Timeout)
_lock = threading.Lock()
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
    if ms is None:
        return "TIMEOUT", "[!]"
    if ms < 20:
        return "SEHR GUT", "[+]"
    if ms < 50:
        return "GUT", "[+]"
    if ms < 100:
        return "OK", "[o]"
    return "LANGSAM", "[~]"


def render():
    """Baut den ASCII-Dashboard-Block als Text."""
    with _lock:
        hist = list(history)
    if not hist:
        return "DB-Wifi Check startet ... erste Messung laeuft."

    values = [v for v in hist if v is not None]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    last = hist[-1]
    label, dot = status_label(last)

    avg = sum(values) / len(values) if values else 0
    lo = min(values) if values else 0
    hi = max(values) if values else 0
    losses = sum(1 for v in hist if v is None)
    loss_pct = losses / len(hist) * 100 if hist else 0

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
    spark = "".join(sparkchar(v) for v in hist)
    L.append("|  {:<60}|".format(spark))
    L.append("+" + "-" * 62 + "+")
    for i, v in enumerate(reversed(hist)):
        tag = "jetzt" if i == 0 else f"-{i*INTERVAL}s"
        val = " TO " if v is None else f"{v:4.0f}"
        L.append("  {:>6} {} {}ms".format(tag, bar(v), val))
    return "\n".join(L)


HTML = """<!doctype html>
<html lang="de"><head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="{interval}">
<title>DB-Wifi Check</title>
<style>
  body{{background:#0b0f14;color:#c8ffcc;font-family:'DejaVu Sans Mono',Menlo,Consolas,monospace;
       margin:0;padding:24px;display:flex;justify-content:center;}}
  pre{{font-size:15px;line-height:1.35;white-space:pre;}}
  .foot{{color:#5a6b5a;font-size:12px;margin-top:12px;}}
</style></head>
<body><div>
<pre>{body}</pre>
<div class="foot">Auto-Refresh alle {interval}s &middot; Ziel {target}:{port} &middot; /raw fuer Klartext</div>
</div></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="text/plain; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/healthz"):
            self._send(200, "ok")
        elif self.path.startswith("/raw"):
            self._send(200, render() + "\n")
        else:
            page = HTML.format(interval=INTERVAL, body=render(), target=TARGET, port=PROBE_PORT)
            self._send(200, page, "text/html; charset=utf-8")

    def log_message(self, *args):
        pass


def checker_loop():
    while True:
        ms = probe_once()
        with _lock:
            history.append(ms)
        print("\n" + render() + "\n", flush=True)
        time.sleep(INTERVAL)


def main():
    print(f"DB-Wifi Check: Ziel={TARGET}:{PROBE_PORT}, Intervall={INTERVAL}s, HTTP-Port={PORT}", flush=True)
    threading.Thread(target=checker_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
