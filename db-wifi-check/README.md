# DB-Wifi Check

Kleine Kubernetes-App, die alle 30 Sekunden die Verbindung zum DNS-Server
**8.8.8.8** prüft, die Latenz misst und das Ganze als **ASCII-Grafik**
darstellt — im Log (`kubectl logs`) und über ein Web-Interface auf
**Port 8080**.

## Warum kein ICMP-Ping?

Der Cluster (`docker-lab`, Talos) erzwingt die **restricted**-PodSecurity-Policy.
Die verbietet die Capability `NET_RAW`, die ein echter ICMP-Ping bräuchte.
Deshalb misst die App die Erreichbarkeit + Latenz per **TCP-Connect zum
DNS-Port 1.1.1.1:53** — gleiche Aussage (erreichbar? wie schnell?), aber ganz
ohne Sonderrechte, als Nicht-Root.

## Warum kein eigenes Image?

`docker-lab` ist ein Talos-in-Docker-Cluster mit mehreren Nodes und **ohne
Registry** — ein lokal gebautes Image kennen die Nodes nicht. Statt das Image
zu verteilen, läuft die App im **öffentlichen `python:3.12-slim`** (das der
Cluster selbst zieht); der Code (`app/check.py`, reine Standardbibliothek)
wird als **ConfigMap** eingehängt. Kein Build, keine Registry.

## Aufbau

```
db-wifi-check/
├── app/check.py      # TCP-Check + ASCII-Dashboard + HTTP-Server (nur stdlib)
├── k8s.yaml          # Deployment (1 Replica, restricted-konform) + Service :8080
├── deploy.sh         # ConfigMap + Apply + Rollout + Port-Forward
├── Dockerfile        # optional: nur falls du doch ein eigenes Image bauen willst
└── README.md
```

## Deployen

```bash
cd db-wifi-check
KUBE_CONTEXT=admin@docker-lab ./deploy.sh
```

Danach im Browser: **http://localhost:8080**

> Anderer Kontext? Einfach `KUBE_CONTEXT=<name> ./deploy.sh`
> (`kubectl config get-contexts` zeigt die verfügbaren).

### Manuell

```bash
kubectl config use-context admin@docker-lab
kubectl create configmap db-wifi-check-src --from-file=check.py=app/check.py \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f k8s.yaml
kubectl rollout restart deploy/db-wifi-check
kubectl rollout status deploy/db-wifi-check
kubectl port-forward svc/db-wifi-check 8080:8080
```

## Endpunkte

| Pfad       | Inhalt                                     |
|------------|--------------------------------------------|
| `/`        | HTML-Seite, ASCII-Grafik, Auto-Refresh 30s |
| `/raw`     | reiner Text (`curl localhost:8080/raw`)    |
| `/healthz` | Liveness-/Readiness-Probe                  |

## Konfiguration (Env-Variablen im Deployment)

| Variable           | Default   | Bedeutung                       |
|--------------------|-----------|---------------------------------|
| `TARGET`           | `8.8.8.8` | Ziel-Host                       |
| `PROBE_PORT`       | `53`      | geprüfter TCP-Port (DNS)        |
| `INTERVAL_SECONDS` | `30`      | Prüf-Intervall                  |
| `TIMEOUT_SECONDS`  | `2`       | Connect-Timeout                 |
| `HISTORY`          | `20`      | Anzahl gespeicherter Messungen  |
| `MAX_MS`           | `100`     | ms = volle Balkenbreite         |
| `PORT`             | `8080`    | HTTP-Port                       |

## Code ändern

`app/check.py` bearbeiten und `deploy.sh` erneut laufen lassen — die ConfigMap
wird aktualisiert und der Pod automatisch neu gestartet.

## Logs / Aufräumen

```bash
kubectl logs -f deploy/db-wifi-check
kubectl delete -f k8s.yaml
kubectl delete configmap db-wifi-check-src
```
