FROM python:3.12-slim

# Reine Standardbibliothek, keine System-Pakete noetig
# (die App misst per TCP-Connect zum DNS-Port, nicht per ICMP-Ping).
WORKDIR /app
COPY app/check.py /app/check.py

# Nicht als root laufen
RUN useradd -u 10001 -m appuser
USER 10001

EXPOSE 8080
ENV PYTHONUNBUFFERED=1 TARGET=8.8.8.8 INTERVAL_SECONDS=30 PORT=8080

ENTRYPOINT ["python", "/app/check.py"]
