#!/usr/bin/env bash
set -Eeuo pipefail

readonly PRIVATE_APP_DIR="${PRIVATE_APP_DIR:-}"
readonly TARGET_SHA="${TARGET_SHA:-}"
readonly DEPLOY_HOST="${DEPLOY_HOST:-}"
readonly DEPLOY_USER="${DEPLOY_USER:-}"

if [[ ! "$TARGET_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  printf 'Invalid deployment target SHA.\n' >&2
  exit 64
fi
if [[ ! "$DEPLOY_HOST" =~ ^[A-Za-z0-9._:@-]{1,255}$ \
  || ! "$DEPLOY_USER" =~ ^[A-Za-z0-9._-]{1,64}$ ]]; then
  printf 'Invalid deployment SSH destination.\n' >&2
  exit 64
fi
if [[ ! -d "$PRIVATE_APP_DIR" ]]; then
  printf 'Private application checkout is missing.\n' >&2
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
    printf 'Required deployment input is missing: %s\n' "$variable" >&2
    exit 64
  fi
done
if [[ ${#OPENAI_API_KEY} -lt 20 || ${#OPENAI_API_KEY} -gt 4096 \
  || "$OPENAI_API_KEY" != sk-* \
  || "$OPENAI_API_KEY" == *$'\n'* \
  || "$OPENAI_API_KEY" == *$'\r'* ]]; then
  printf 'OpenAI runtime credential is invalid.\n' >&2
  exit 64
fi
for command in curl docker git gzip npm python sha256sum ssh tar uv; do
  if ! command -v "$command" >/dev/null; then
    printf 'Required deployment command is missing: %s\n' "$command" >&2
    exit 69
  fi
done
if [[ ! -d "$PRIVATE_APP_DIR/.git" ]]; then
  printf 'Private application git checkout is invalid.\n' >&2
  exit 66
fi

checked_out_sha=$(git -C "$PRIVATE_APP_DIR" rev-parse HEAD)
readonly checked_out_sha
if [[ "$checked_out_sha" != "$TARGET_SHA" ]]; then
  printf 'Requested SHA is not the checked-out private branch head.\n' >&2
  exit 65
fi
if ! git -C "$PRIVATE_APP_DIR" diff --quiet \
  || ! git -C "$PRIVATE_APP_DIR" diff --cached --quiet; then
  printf 'Private application checkout contains uncommitted changes.\n' >&2
  exit 65
fi

umask 077
temporary_directory=$(mktemp -d "$RUNNER_TEMP/obrabot4-deploy.XXXXXX")
readonly temporary_directory
target_image="kakpeople-app:$TARGET_SHA"
readonly target_image
cleanup() {
  rm -rf "$temporary_directory"
  docker image rm "$target_image" >/dev/null 2>&1 || true
}
trap cleanup EXIT

cd "$PRIVATE_APP_DIR"
uv sync --locked --extra test
uv run python -m pytest tests_web tests -q -W error
npm --prefix frontend ci
npm --prefix frontend test -- --run
npm --prefix frontend run build

docker build \
  --label "org.opencontainers.image.revision=$TARGET_SHA" \
  --tag "$target_image" \
  .
docker save "$target_image" \
  | gzip -1 >"$temporary_directory/kakpeople-app.tar.gz"
test -s "$temporary_directory/kakpeople-app.tar.gz"

printf '%s' "$OPENAI_API_KEY" >"$temporary_directory/openai-api-key"
unset OPENAI_API_KEY
artifact_sha256=$(sha256sum "$temporary_directory/kakpeople-app.tar.gz" \
  | awk '{print $1}')
runtime_secret_sha256=$(sha256sum "$temporary_directory/openai-api-key" \
  | awk '{print $1}')
readonly artifact_sha256 runtime_secret_sha256
audience="kakpeople-regru-production:$artifact_sha256:$runtime_secret_sha256"
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
  >"$temporary_directory/oidc-response.json"
python - "$temporary_directory/oidc-response.json" \
  "$temporary_directory/github-oidc.jwt" <<'PY'
import json
from pathlib import Path
import sys

response = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
token = response.get("value")
if not isinstance(token, str) or not token:
    raise SystemExit("OIDC response did not contain a token.")
Path(sys.argv[2]).write_text(token, encoding="ascii")
PY
rm -f "$temporary_directory/oidc-response.json"

tar --create \
  --file "$temporary_directory/deploy-bundle.tar" \
  --directory "$temporary_directory" \
  kakpeople-app.tar.gz github-oidc.jwt openai-api-key

printf '%s\n' "$DEPLOY_KEY" >"$temporary_directory/regru_deploy"
printf '%s\n' "$DEPLOY_KNOWN_HOSTS" >"$temporary_directory/regru_known_hosts"
unset DEPLOY_KEY DEPLOY_KNOWN_HOSTS
chmod 600 \
  "$temporary_directory/regru_deploy" \
  "$temporary_directory/regru_known_hosts"

set -o pipefail
cat "$temporary_directory/deploy-bundle.tar" |
  ssh \
    -i "$temporary_directory/regru_deploy" \
    -o BatchMode=yes \
    -o IdentitiesOnly=yes \
    -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile="$temporary_directory/regru_known_hosts" \
    "$DEPLOY_USER@$DEPLOY_HOST" \
    "deploy $TARGET_SHA ops/regru-migration"

printf 'Production deployment completed for %s.\n' "$TARGET_SHA"
