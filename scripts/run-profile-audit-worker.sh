#!/usr/bin/env bash
set -Eeuo pipefail

readonly TARGET_SHA="${TARGET_SHA:-}"
readonly PROFILE_OPERATION="${PROFILE_OPERATION:-}"
readonly AUDIT_RUN_ID="${AUDIT_RUN_ID:-}"
readonly DEPLOY_HOST="${DEPLOY_HOST:-}"
readonly DEPLOY_USER="${DEPLOY_USER:-}"

if [[ ! "$TARGET_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  printf 'Invalid profile-audit target SHA.\n' >&2
  exit 64
fi
if [[ ! "$PROFILE_OPERATION" =~ ^(audit|repair)$ ]]; then
  printf 'Invalid profile operation.\n' >&2
  exit 64
fi
if [[ ! "$AUDIT_RUN_ID" =~ ^(auto|[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$ ]]; then
  printf 'Invalid profile-audit run id.\n' >&2
  exit 64
fi
if [[ ! "$DEPLOY_HOST" =~ ^[A-Za-z0-9._:@-]{1,255}$ \
  || ! "$DEPLOY_USER" =~ ^[A-Za-z0-9._-]{1,64}$ ]]; then
  printf 'Invalid profile-audit SSH destination.\n' >&2
  exit 64
fi
for variable in \
  ACTIONS_ID_TOKEN_REQUEST_TOKEN \
  ACTIONS_ID_TOKEN_REQUEST_URL \
  DEPLOY_KEY \
  DEPLOY_KNOWN_HOSTS \
  OPENAI_API_KEY \
  RUNNER_TEMP; do
  if [[ -z "${!variable:-}" ]]; then
    printf 'Required worker input is missing: %s\n' "$variable" >&2
    exit 64
  fi
done

umask 077
temporary_directory=$(mktemp -d "$RUNNER_TEMP/obrabot4-profile-audit.XXXXXX")
readonly temporary_directory
cleanup() {
  rm -rf "$temporary_directory"
}
trap cleanup EXIT

identity_file="$temporary_directory/regru_profile_audit"
known_hosts_file="$temporary_directory/regru_known_hosts"
oidc_response="$temporary_directory/oidc-response.json"
oidc_token_file="$temporary_directory/oidc.jwt"
readonly identity_file known_hosts_file oidc_response oidc_token_file
printf '%s\n' "$DEPLOY_KEY" >"$identity_file"
printf '%s\n' "$DEPLOY_KNOWN_HOSTS" >"$known_hosts_file"
chmod 600 "$identity_file" "$known_hosts_file"

audience="kakpeople-profile-audit:$TARGET_SHA:$PROFILE_OPERATION:$AUDIT_RUN_ID"
encoded_audience=$(python - "$audience" <<'PY'
import sys
from urllib.parse import quote

print(quote(sys.argv[1], safe=""))
PY
)
readonly audience encoded_audience
curl --fail --silent --show-error \
  --header "Authorization: Bearer $ACTIONS_ID_TOKEN_REQUEST_TOKEN" \
  "${ACTIONS_ID_TOKEN_REQUEST_URL}&audience=$encoded_audience" \
  >"$oidc_response"
python - "$oidc_response" "$oidc_token_file" <<'PY'
import json
from pathlib import Path
import sys

response = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
token = response.get("value")
if not isinstance(token, str) or not token:
    raise SystemExit("OIDC response did not contain a token.")
Path(sys.argv[2]).write_text(token, encoding="ascii")
PY
rm -f "$oidc_response"
chmod 600 "$oidc_token_file"

python -m obrabot4.github_worker \
  --host "$DEPLOY_HOST" \
  --user "$DEPLOY_USER" \
  --identity-file "$identity_file" \
  --known-hosts-file "$known_hosts_file" \
  --oidc-token-file "$oidc_token_file" \
  --target-sha "$TARGET_SHA" \
  --operation "$PROFILE_OPERATION" \
  --run-id "$AUDIT_RUN_ID" \
  --concurrency 4

