# kubernetes-fun

Kleine Kubernetes-Spielprojekte zum Ausprobieren.

## Projekte

| Projekt | Beschreibung |
|---------|--------------|
| [`db-wifi-check`](./db-wifi-check) | App, die per TCP-Connect die Erreichbarkeit + Latenz zu einem DNS-Server misst und als ASCII-Dashboard (Log + Web auf Port 8080) darstellt. Läuft restricted-PodSecurity-konform, ohne eigenes Image (Code via ConfigMap). |

## Lizenz

[MIT](./LICENSE)
