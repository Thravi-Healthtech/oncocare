#!/bin/bash
# Store OncoCare's WhatsApp settings in SSM Parameter Store (run once on your machine; needs the AWS CLI signed in).
# Secrets are typed at hidden prompts and go straight to SSM - they are never written to disk or shown.
# The next ./deploy/aws/deploy.sh picks them up.
#
#   ./deploy/aws/whatsapp-ssm-setup.sh
#
# Defaults are for the temporary setup on +91 93467 66320 (shared with another Thravi product).
# To switch to +91 97055 39190 later: PHONE_NUMBER_ID=<its id> DISPLAY_NUMBER="+91 97055 39190" SHARED=0 ./deploy/aws/whatsapp-ssm-setup.sh
set -euo pipefail
export AWS_REGION="${AWS_REGION:-ap-south-1}" AWS_PAGER=""
PHONE_NUMBER_ID="${PHONE_NUMBER_ID:-779611615245927}"
DISPLAY_NUMBER="${DISPLAY_NUMBER:-+91 93467 66320}"
SHARED="${SHARED:-1}"

put() { aws ssm put-parameter --name "$1" --type "$2" --value "$3" --overwrite >/dev/null && echo "  stored $1"; }
exists() { aws ssm get-parameter --name "$1" >/dev/null 2>&1; }

echo "WhatsApp settings for OncoCare (region $AWS_REGION)"
put /oncocare/whatsapp/phone_number_id String "$PHONE_NUMBER_ID"
put /oncocare/whatsapp/display_number String "$DISPLAY_NUMBER"
put /oncocare/whatsapp/shared_number String "$SHARED"
put /oncocare/whatsapp/template_lang String en

if ! exists /oncocare/whatsapp/verify_token; then
  put /oncocare/whatsapp/verify_token SecureString "oncocare-wh-$(openssl rand -hex 16)"
fi

read -r -s -p "System User access token (Enter to keep the stored one): " TOKEN; echo
[ -n "$TOKEN" ] && put /oncocare/whatsapp/token SecureString "$TOKEN"
read -r -s -p "App secret of the 'Thravi' app (Enter to keep the stored one): " SECRET; echo
[ -n "$SECRET" ] && put /oncocare/whatsapp/app_secret SecureString "$SECRET"
read -r -s -p "Sarvam AI API key, optional (Enter to skip): " SARVAM; echo
[ -n "$SARVAM" ] && put /oncocare/sarvam_api_key SecureString "$SARVAM"

if exists /oncocare/whatsapp/token && exists /oncocare/whatsapp/app_secret; then
  put /oncocare/whatsapp/mode String live
else
  echo "  token or app secret missing: leaving WhatsApp in simulator mode"
fi
echo
echo "Webhook verify token (enter it in the Meta App Dashboard with the callback URL https://onco.thravi.in/api/whatsapp/webhook):"
echo "  aws ssm get-parameter --name /oncocare/whatsapp/verify_token --with-decryption --query Parameter.Value --output text"
echo "Done. Now run ./deploy/aws/deploy.sh"
