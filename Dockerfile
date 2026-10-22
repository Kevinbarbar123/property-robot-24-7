FROM python:3.12-slim-bookworm

WORKDIR /app
ENV TZ=Asia/Beirut

COPY requirements-recovery.txt ./
RUN pip install --no-cache-dir -r requirements-recovery.txt \
    && python -m playwright install --with-deps chromium \
    && apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends xvfb tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY . .

ENV PYTHONUNBUFFERED=1
ENV RECOVERY_BROWSER_CHANNEL="" DISPLAY=:99
ENV BOT_ACTIVE=false REQUIRE_PERSISTENT_DATA=true REQUIRE_MIGRATED_STATE=true PERSIST_RUNTIME_DIRS=true

EXPOSE 8787

CMD ["python", "-u", "railway_start.py"]
