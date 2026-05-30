#!/usr/bin/env bash
set -euo pipefail

SITE_NAME="${SITE_NAME:-workspace-mcp-gateway}"
SERVER_NAME="${SERVER_NAME:-_}"
UPSTREAM="${UPSTREAM:-127.0.0.1:8000}"
LISTEN_PORT="${LISTEN_PORT:-80}"
DEFAULT_SERVER="${DEFAULT_SERVER:-false}"

SITES_AVAILABLE="${SITES_AVAILABLE:-/etc/nginx/sites-available}"
SITES_ENABLED="${SITES_ENABLED:-/etc/nginx/sites-enabled}"
SITE_FILE="${SITES_AVAILABLE}/${SITE_NAME}"
ENABLED_FILE="${SITES_ENABLED}/${SITE_NAME}"
LISTEN_DIRECTIVE="listen ${LISTEN_PORT};"

if [[ "${DEFAULT_SERVER}" == "true" ]]; then
  LISTEN_DIRECTIVE="listen ${LISTEN_PORT} default_server;"
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

cat >"${SITE_FILE}" <<NGINX
server {
    ${LISTEN_DIRECTIVE}
    server_name ${SERVER_NAME};

    client_max_body_size 10m;

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

        # Strip client-supplied identity headers. Only a trusted upstream should add these.
        proxy_set_header X-Open-WebUI-User-Id "";
        proxy_set_header X-Open-WebUI-User-Email "";
        proxy_set_header X-Open-WebUI-User-Name "";
    }
}
NGINX

ln -sf "${SITE_FILE}" "${ENABLED_FILE}"
nginx -t

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

if [[ "${SERVER_NAME}" == "_" ]]; then
  cat <<EOF

SERVER_NAME is "_", so set app URLs to the real host, IP, or domain users will call.
For example:
  BASE_URL=http://YOUR_HOST_OR_DOMAIN
  TRUSTED_OPEN_WEBUI_ORIGIN=http://YOUR_HOST_OR_DOMAIN
  DEV_TRUST_ALL_ORIGINS=false
EOF
else
  cat <<EOF

Set these app environment values to match the public URL:
  BASE_URL=http://${SERVER_NAME}
  TRUSTED_OPEN_WEBUI_ORIGIN=http://${SERVER_NAME}
  DEV_TRUST_ALL_ORIGINS=false
EOF
fi
