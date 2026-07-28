#!/usr/bin/env bash
# Build the "Deploy to Azure" portal URL.
#
# The portal fetches both files over HTTPS, so they must be reachable without
# authentication. Any static host works; a public repo's raw URLs are typical.
#
#   BASE_URL=https://raw.githubusercontent.com/your-org/azure-sharepoint-copy/v0.1.0 \
#     ./scripts/deploy-url.sh
set -euo pipefail

base_url="${BASE_URL:-}"
if [[ -z "$base_url" ]]; then
  printf 'Set BASE_URL to the directory holding infra/main.json and infra/createUiDefinition.json.\n' >&2
  printf 'Example: BASE_URL=https://raw.githubusercontent.com/org/repo/v0.1.0 %s\n' "$0" >&2
  exit 2
fi
base_url="${base_url%/}"

template_url="${base_url}/infra/main.json"
ui_url="${base_url}/infra/createUiDefinition.json"

encoded="$(
  python3 - "$template_url" "$ui_url" <<'PY'
import sys, urllib.parse
print(urllib.parse.quote(sys.argv[1], safe=""))
print(urllib.parse.quote(sys.argv[2], safe=""))
PY
)"
template_encoded="$(printf '%s\n' "$encoded" | sed -n 1p)"
ui_encoded="$(printf '%s\n' "$encoded" | sed -n 2p)"

deploy_url="https://portal.azure.com/#create/Microsoft.Template/uri/${template_encoded}/createUIDefinitionUri/${ui_encoded}"

printf 'Deploy to Azure URL:\n\n%s\n\n' "$deploy_url"
printf 'Markdown button:\n\n'
printf '[![Deploy to Azure](https://aka.ms/deploytoazurebutton)](%s)\n\n' "$deploy_url"
printf 'Both files must return HTTP 200 without authentication. Check with:\n'
printf '  curl -sSf -o /dev/null -w "%%{http_code}\\n" %s\n' "$template_url"
printf '  curl -sSf -o /dev/null -w "%%{http_code}\\n" %s\n' "$ui_url"
