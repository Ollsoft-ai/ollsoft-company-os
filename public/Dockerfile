# kb-share — the only Company OS process that faces the open internet.
#
# Nothing in here knows about /srv/kb. It serves the per-share directories
# bind-mounted at /data, configured by the JSON files at /conf, and has no
# database, no session key, no shell and no route back to the host.
FROM python:3.12-alpine AS base

# markdown-it-py is pure Python and renders with raw HTML DISABLED, so a
# document's bytes can never become script. Pinned; nothing else is installed.
RUN pip install --no-cache-dir "markdown-it-py==3.0.0" "mdurl==0.1.2" \
 && adduser -S -u 10001 -H -s /sbin/nologin kbshare \
 && rm -rf /root/.cache

COPY serve.py /app/serve.py

USER 10001
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8080
ENTRYPOINT ["python", "/app/serve.py"]
