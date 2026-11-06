#!/bin/bash
# Runs ON the EC2 instance (as root, via SSM Run Command) to install or update OncoCare.
# Usage: remote-install.sh <release-dir>   (env: DOMAIN, AWS_REGION)
set -euo pipefail
REL="$1"
DOMAIN="${DOMAIN:?}"
REGION="${AWS_REGION:?}"
AWS=/snap/bin/aws
DATA=/var/lib/oncocare
export DEBIAN_FRONTEND=noninteractive

log() { echo "[oncocare] $*"; }

# 1. System user and data directory (database, uploads, env file, context cache live here and survive redeploys)
id oncocare >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin oncocare
install -d -o oncocare -g oncocare -m 750 "$DATA"
if [ ! -d "$DATA/context" ]; then
  cp -r "$REL/data/context" "$DATA/context" 2>/dev/null || mkdir -p "$DATA/context"
  chown -R oncocare:oncocare "$DATA/context"
fi

# 2. Caddy (reverse proxy + automatic Let's Encrypt HTTPS)
if ! command -v caddy >/dev/null 2>&1; then
  log "Installing Caddy…"
  apt-get update -qq >/var/log/oncocare-apt.log
  apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https curl gnupg >>/var/log/oncocare-apt.log
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt -o /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq >>/var/log/oncocare-apt.log
  apt-get install -y -qq caddy >>/var/log/oncocare-apt.log
fi
python3 -c 'import sys; assert sys.version_info >= (3, 10), sys.version' || { log "Python 3.10+ required"; exit 1; }

# 3. Environment file (secrets come from SSM Parameter Store, never from the repo)
PW=$($AWS ssm get-parameter --region "$REGION" --name /oncocare/demo_password --with-decryption --query Parameter.Value --output text)
KEY=$($AWS ssm get-parameter --region "$REGION" --name /oncocare/anthropic_api_key --with-decryption --query Parameter.Value --output text 2>/dev/null || true)
# Optional settings: written to the env file only when the SSM parameter exists (missing ones keep the app defaults,
# e.g. WhatsApp stays in simulator mode until /oncocare/whatsapp/mode is "live").
OPTIONAL_PARAMS="
/oncocare/sarvam_api_key:SARVAM_API_KEY
/oncocare/whatsapp/mode:WHATSAPP_MODE
/oncocare/whatsapp/token:WHATSAPP_TOKEN
/oncocare/whatsapp/phone_number_id:WHATSAPP_PHONE_NUMBER_ID
/oncocare/whatsapp/display_number:WHATSAPP_DISPLAY_NUMBER
/oncocare/whatsapp/verify_token:WHATSAPP_VERIFY_TOKEN
/oncocare/whatsapp/app_secret:WHATSAPP_APP_SECRET
/oncocare/whatsapp/api_version:WHATSAPP_API_VERSION
/oncocare/whatsapp/template_lang:WHATSAPP_TEMPLATE_LANG
/oncocare/whatsapp/shared_number:WHATSAPP_SHARED_NUMBER
/oncocare/clinic_name:ONCOCARE_CLINIC_NAME
/oncocare/care_phone:ONCOCARE_CARE_PHONE
"
optional_env() {
  local pair name var value
  for pair in $OPTIONAL_PARAMS; do
    name="${pair%%:*}"; var="${pair##*:}"
    value=$($AWS ssm get-parameter --region "$REGION" --name "$name" --with-decryption --query Parameter.Value --output text 2>/dev/null || true)
    [ -n "$value" ] && [ "$value" != "None" ] && echo "$var=$value"
  done
  true
}
umask 077
{
  echo "ONCOCARE_PUBLIC=1"
  echo "ONCOCARE_DEV_PASSWORD=$PW"
  echo "ONCOCARE_DEMO_BANNER=Demo environment - synthetic patients only. Do not enter real patient information."
  echo "ONCOCARE_ENV_FILE=$DATA/oncocare.env"
  echo "ONCOCARE_CONTEXT_DIR=$DATA/context"
  echo "PYTHONDONTWRITEBYTECODE=1"
  [ -n "$KEY" ] && [ "$KEY" != "None" ] && echo "ANTHROPIC_API_KEY=$KEY"
  optional_env
} > "$DATA/oncocare.env"
chown oncocare:oncocare "$DATA/oncocare.env"; chmod 600 "$DATA/oncocare.env"
umask 022

# 4. systemd service
cat > /etc/systemd/system/oncocare.service <<UNIT
[Unit]
Description=Thravi OncoCare
After=network-online.target
Wants=network-online.target

[Service]
User=oncocare
Group=oncocare
WorkingDirectory=/opt/oncocare/current
EnvironmentFile=$DATA/oncocare.env
ExecStart=/usr/bin/python3 -m backend.server --seed --host 127.0.0.1 --port 4173 --db $DATA/oncocare.db
Restart=always
RestartSec=3
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=$DATA

[Install]
WantedBy=multi-user.target
UNIT

# 5. Caddy site
cat > /etc/caddy/Caddyfile <<CADDY
$DOMAIN {
	encode zstd gzip
	request_body {
		max_size 25MB
	}
	header {
		Strict-Transport-Security "max-age=31536000"
		X-Content-Type-Options "nosniff"
		X-Frame-Options "DENY"
		Referrer-Policy "same-origin"
		-Server
	}
	reverse_proxy 127.0.0.1:4173
}
CADDY

# 6. Switch release, restart, health-check
chmod -R a+rX "$REL"
ln -sfn "$REL" /opt/oncocare/current
systemctl daemon-reload
systemctl enable --quiet oncocare caddy
systemctl restart oncocare
systemctl reload caddy 2>/dev/null || systemctl restart caddy
for i in $(seq 1 20); do
  if curl -fsS -o /dev/null http://127.0.0.1:4173/health; then log "App healthy ($(basename "$REL"))."; break; fi
  sleep 1
  [ "$i" = 20 ] && { log "App did not become healthy:"; journalctl -u oncocare -n 40 --no-pager; exit 1; }
done

# 7. Keep the five most recent releases
ls -1dt /opt/oncocare/releases/*/ | tail -n +6 | xargs -r rm -rf
log "Done. https://$DOMAIN"
