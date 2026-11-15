FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

# Reine Standardbibliothek, keine System-Pakete noetig
# (die App misst per TCP-Connect zum DNS-Port, nicht per ICMP-Ping).
WORKDIR /app
COPY app/check.py /app/check.py

# Nicht als root laufen
RUN useradd -u 10001 -m appuser
USER 10001

EXPOSE 8080
ENV PYTHONUNBUFFERED=1 TARGETS=8.8.8.8:53,1.1.1.1:53 INTERVAL_SECONDS=30 PORT=8080 DATA_DIR=/data

ENTRYPOINT ["python", "/app/check.py"]
