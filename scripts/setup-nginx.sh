#!/usr/bin/env bash
set -euo pipefail

SITE_NAME="${SITE_NAME:-workspace-mcp-gateway}"
SERVER_NAME="${SERVER_NAME:-_}"
# Default to loopback so the only public route to the gateway is through nginx.
# Bind the app itself to 127.0.0.1:8000 (not 0.0.0.0) and firewall the port.
UPSTREAM="${UPSTREAM:-127.0.0.1:8000}"
LISTEN_PORT="${LISTEN_PORT:-80}"
DEFAULT_SERVER="${DEFAULT_SERVER:-false}"

# TLS: set both SSL_CERT and SSL_KEY to terminate HTTPS on :443 and redirect :80.
SSL_CERT="${SSL_CERT:-}"
SSL_KEY="${SSL_KEY:-}"

# Loopback TLS: when something else owns public :443 (e.g. an nginx `stream` SNI router in
# /etc/nginx/stream-conf.d/ that forwards TLS to a local port), set TLS_LISTEN to the address
# it forwards to, e.g. TLS_LISTEN=127.0.0.1:9444. The TLS server then listens there instead of
# on 443 (together with SSL_CERT/SSL_KEY), and the plain-HTTP server on LISTEN_PORT is kept
# as a proxy instead of becoming a redirect. PLAIN_SERVER_NAME names that plain server
# (default `_`).
TLS_LISTEN="${TLS_LISTEN:-}"
PLAIN_SERVER_NAME="${PLAIN_SERVER_NAME:-_}"

# PRINT_ONLY=true prints the generated site file to stdout and exits (no root, no nginx).
PRINT_ONLY="${PRINT_ONLY:-false}"

SITES_AVAILABLE="${SITES_AVAILABLE:-/etc/nginx/sites-available}"
SITES_ENABLED="${SITES_ENABLED:-/etc/nginx/sites-enabled}"
SITE_FILE="${SITES_AVAILABLE}/${SITE_NAME}"
ENABLED_FILE="${SITES_ENABLED}/${SITE_NAME}"

DEFAULT_SUFFIX=""
if [[ "${DEFAULT_SERVER}" == "true" ]]; then
  DEFAULT_SUFFIX=" default_server"
fi

USE_TLS="false"
if [[ -n "${SSL_CERT}" && -n "${SSL_KEY}" ]]; then
  USE_TLS="true"
fi

if [[ -n "${TLS_LISTEN}" && "${USE_TLS}" != "true" ]]; then
  echo "TLS_LISTEN needs SSL_CERT and SSL_KEY" >&2
  exit 1
fi

# The location blocks are identical for every server block.
IFS= read -r -d '' SERVER_BODY <<BODY || true
    client_max_body_size 16m;  # inline receipts: 10 MB file = ~14 MB base64 JSON

    location /health {
        allow 127.0.0.1;
        allow 10.0.0.0/8;
        allow 172.16.0.0/12;
        allow 192.168.0.0/16;
        deny all;

        proxy_pass http://${UPSTREAM};
    }

    location /ready {
        allow 127.0.0.1;
        allow 10.0.0.0/8;
        allow 172.16.0.0/12;
        allow 192.168.0.0/16;
        deny all;

        proxy_pass http://${UPSTREAM};
    }

    location / {
        proxy_pass http://${UPSTREAM};

        proxy_http_version 1.1;
        proxy_buffering off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;

        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header X-Forwarded-Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;

        # Strip client-supplied identity + trust headers. Public callers must
        # authenticate with an OAuth or static Bearer token; only on-host Open WebUI
        # (which bypasses this proxy) may assert header identity.
        proxy_set_header X-Open-WebUI-User-Id "";
        proxy_set_header X-Open-WebUI-User-Email "";
        proxy_set_header X-Open-WebUI-User-Name "";
        proxy_set_header X-Open-WebUI-Chat-Id "";
        proxy_set_header X-Open-WebUI-Message-Id "";
        proxy_set_header X-Open-WebUI-Automation-Id "";
        proxy_set_header X-OpenWebUI-User-Id "";
        proxy_set_header X-OpenWebUI-User-Email "";
        proxy_set_header X-OpenWebUI-User-Name "";
        proxy_set_header X-OpenWebUI-Chat-Id "";
        proxy_set_header X-OpenWebUI-Message-Id "";
        proxy_set_header X-OpenWebUI-Automation-Id "";
        proxy_set_header X-Gateway-Auth "";
    }
BODY
SERVER_BODY="${SERVER_BODY%$'\n'}"

TLS_DIRECTIVES="    ssl_certificate ${SSL_CERT};
    ssl_certificate_key ${SSL_KEY};
    ssl_protocols TLSv1.2 TLSv1.3;
"

if [[ -n "${TLS_LISTEN}" ]]; then
  # Public :443 belongs to the stream SNI router; terminate TLS on loopback for it.
  SITE_CONFIG="# Public :443 is owned by the nginx stream SNI router, which forwards
# ${SERVER_NAME} TLS traffic to ${TLS_LISTEN}. Do not listen on 443 here.
server {
    listen ${TLS_LISTEN} ssl;
    server_name ${SERVER_NAME};

${TLS_DIRECTIVES}
${SERVER_BODY}
}

server {
    listen ${LISTEN_PORT}${DEFAULT_SUFFIX};
    server_name ${PLAIN_SERVER_NAME};

${SERVER_BODY}
}
"
elif [[ "${USE_TLS}" == "true" ]]; then
  SITE_CONFIG="server {
    listen 80${DEFAULT_SUFFIX};
    server_name ${SERVER_NAME};
    return 301 https://\$host\$request_uri;
}

server {
    listen 443 ssl${DEFAULT_SUFFIX};
    server_name ${SERVER_NAME};

${TLS_DIRECTIVES}
${SERVER_BODY}
}
"
else
  SITE_CONFIG="server {
    listen ${LISTEN_PORT}${DEFAULT_SUFFIX};
    server_name ${SERVER_NAME};

${SERVER_BODY}
}
"
fi

if [[ "${PRINT_ONLY}" == "true" ]]; then
  printf '%s' "${SITE_CONFIG}"
  exit 0
fi

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this script with sudo:" >&2
  echo "  sudo SERVER_NAME=${SERVER_NAME} UPSTREAM=${UPSTREAM} $0" >&2
  exit 1
fi

if ! command -v nginx >/dev/null 2>&1; then
  echo "nginx is not installed or not on PATH" >&2
  exit 1
fi

mkdir -p "${SITES_AVAILABLE}" "${SITES_ENABLED}"

# Keep the previous site file so a bad regeneration can be rolled back.
if [[ -f "${SITE_FILE}" ]]; then
  BACKUP_FILE="${SITE_FILE}.bak.$(date -u +%Y%m%dT%H%M%SZ)"
  cp -p "${SITE_FILE}" "${BACKUP_FILE}"
  echo "Backed up previous site file to ${BACKUP_FILE}"
fi

printf '%s' "${SITE_CONFIG}" >"${SITE_FILE}"

ln -sf "${SITE_FILE}" "${ENABLED_FILE}"
if ! nginx -t; then
  if [[ -n "${BACKUP_FILE:-}" ]]; then
    cp -p "${BACKUP_FILE}" "${SITE_FILE}"
    echo "nginx -t failed; restored ${BACKUP_FILE}" >&2
  fi
  exit 1
fi

if command -v systemctl >/dev/null 2>&1; then
  systemctl reload nginx
else
  nginx -s reload
fi

cat <<EOF
Installed nginx site: ${SITE_FILE}
Enabled nginx site:   ${ENABLED_FILE}
Server name:          ${SERVER_NAME}
Upstream:             http://${UPSTREAM}

EOF

SCHEME="http"
if [[ "${USE_TLS}" == "true" ]]; then
  SCHEME="https"
fi

if [[ "${SERVER_NAME}" == "_" ]]; then
  HOST_HINT="YOUR_HOST_OR_DOMAIN"
else
  HOST_HINT="${SERVER_NAME}"
fi

cat <<EOF

Set these app environment values to match the public URL:
  BASE_URL=${SCHEME}://${HOST_HINT}
  TRUSTED_OPEN_WEBUI_ORIGIN=${SCHEME}://${HOST_HINT}
  DEV_TRUST_ALL_ORIGINS=false

Generate a shared secret and give the SAME value to the gateway and to Open WebUI
(Open WebUI must send it as the X-Gateway-Auth header on MCP requests):
  GATEWAY_SHARED_SECRET=\$(python -c "import secrets; print(secrets.token_urlsafe(48))")

Open WebUI must reach the gateway directly (e.g. http://127.0.0.1:8000), bypassing
this proxy, so its X-Gateway-Auth and X-Open-WebUI-* headers are preserved.
EOF
