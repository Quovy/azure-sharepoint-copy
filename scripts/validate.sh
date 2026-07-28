#!/usr/bin/env bash
# Run every check that does not need an Azure subscription.
set -euo pipefail

project_root="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

python3 -c 'import ast, pathlib; [ast.parse(pathlib.Path(p).read_text()) for p in (
  "copyctl.py", "tests/config_test.py", "tests/uidefinition_test.py")]'
bash -n scripts/*.sh
sh -n app/transfer.sh tests/transfer_test.sh

python3 copyctl.py validate
python3 tests/config_test.py
python3 tests/uidefinition_test.py
sh tests/transfer_test.sh

if command -v shellcheck >/dev/null 2>&1; then
  shellcheck app/transfer.sh scripts/*.sh tests/transfer_test.sh
else
  printf 'shellcheck not installed; skipping shell lint.\n' >&2
fi

bicep_failed=0
while IFS= read -r bicep_file; do
  if ! az bicep build --file "$bicep_file" --stdout >/dev/null; then
    bicep_failed=1
  fi
done < <(find infra -type f -name '*.bicep' | sort)
if [[ "$bicep_failed" != "0" ]]; then
  printf 'Bicep compilation failed.\n' >&2
  exit 1
fi

python3 -c 'import json,pathlib; json.loads(pathlib.Path("infra/createUiDefinition.json").read_text())'

# infra/main.json is the compiled template the portal downloads. A stale copy
# would deploy something other than what infra/main.bicep says.
compiled="$(mktemp)"
trap 'rm -f "$compiled"' EXIT HUP INT TERM
az bicep build --file infra/main.bicep --stdout >"$compiled"
if ! python3 - "$compiled" infra/main.json <<'PY'
import json, sys

def load(path):
    document = json.loads(open(path).read())
    # The generator stamp changes with the Bicep version and is not meaningful.
    document.get("metadata", {}).pop("_generator", None)
    return document

if load(sys.argv[1]) != load(sys.argv[2]):
    print(
        "infra/main.json is out of date. Regenerate it with:\n"
        "  az bicep build --file infra/main.bicep --outfile infra/main.json",
        file=sys.stderr,
    )
    raise SystemExit(1)
PY
then
  exit 1
fi

# The one behaviour this package must never grow: rclone sync deletes at the
# destination, and copy does not.
if grep -rn 'rclone[[:space:]]*sync\|"sync"' app copyctl.py infra 2>/dev/null; then
  printf 'A destination-deleting sync operation appeared. Refusing.\n' >&2
  exit 1
fi

printf 'validation passed\n'
