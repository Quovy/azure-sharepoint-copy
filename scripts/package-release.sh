#!/usr/bin/env bash
# Build the customer-facing archive from committed content only.
set -euo pipefail

project_root="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
version="$(tr -d '[:space:]' <"$project_root/VERSION")"
if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  printf 'VERSION must contain a semantic version.\n' >&2
  exit 1
fi

if ! git -C "$project_root" diff --quiet || ! git -C "$project_root" diff --cached --quiet; then
  printf 'Commit or stash changes before creating a release archive.\n' >&2
  exit 1
fi

# A release must not ship a placeholder image reference.
image="$(python3 -c '
import json, pathlib, sys
path = pathlib.Path(sys.argv[1]) / "infra" / "main.parameters.json"
print(json.loads(path.read_text())["parameters"]["containerImage"]["value"])
' "$project_root")"
if [[ ! "$image" =~ @sha256:[a-f0-9]{64}$ ]]; then
  printf 'infra/main.parameters.json has no digest-pinned image. Run scripts/publish-image.sh first.\n' >&2
  exit 1
fi

"$project_root/scripts/validate.sh"

mkdir -p "$project_root/dist"
archive_name="azure-sharepoint-copy-v${version}.zip"
archive_path="$project_root/dist/$archive_name"
release_root="azure-sharepoint-copy-v${version}"
release_temp="$(mktemp -d)"
trap 'rm -rf "$release_temp"' EXIT HUP INT TERM

# Export a tar tree first: git archive --format=zip embeds the commit hash as
# the archive comment, which the customer artifact should not carry.
git -C "$project_root" archive --format=tar --prefix="${release_root}/" HEAD |
  tar -xf - -C "$release_temp"

(
  cd "$release_temp"
  zip -X -q -r "$release_temp/$archive_name" "$release_root"
)
mv "$release_temp/$archive_name" "$archive_path"

if command -v sha256sum >/dev/null 2>&1; then
  (cd "$project_root/dist" && sha256sum "$archive_name" >"${archive_name}.sha256")
else
  archive_hash="$(shasum -a 256 "$archive_path" | awk '{print $1}')"
  printf '%s  %s\n' "$archive_hash" "$archive_name" >"${archive_path}.sha256"
fi

printf 'Release archive: %s\n' "$archive_path"
printf 'SHA-256 file:    %s.sha256\n' "$archive_path"
printf 'Pinned image:    %s\n' "$image"
