#!/usr/bin/env bash
# Publisher-side only. Customers never run this.
#
# Builds the copy image, pushes it, and prints the digest-pinned reference to
# paste into infra/main.parameters.json and the createUiDefinition default.
set -euo pipefail

project_root="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

registry="${IMAGE_REGISTRY:-}"
if [[ -z "$registry" ]]; then
  printf 'Set IMAGE_REGISTRY, for example: IMAGE_REGISTRY=ghcr.io/your-org %s\n' "$0" >&2
  exit 2
fi

version="$(tr -d '[:space:]' <VERSION)"
repository="${registry}/azure-sharepoint-copy"
tag="${repository}:${version}"

if ! command -v docker >/dev/null 2>&1; then
  printf 'docker is required to publish the image.\n' >&2
  exit 1
fi

printf 'Building %s\n' "$tag"
docker buildx build \
  --platform linux/amd64 \
  --tag "$tag" \
  --push \
  --provenance=false \
  .

digest="$(docker buildx imagetools inspect "$tag" --format '{{.Manifest.Digest}}')"
if [[ ! "$digest" =~ ^sha256:[a-f0-9]{64}$ ]]; then
  printf 'Could not read a sha256 digest for %s\n' "$tag" >&2
  exit 1
fi

pinned="${repository}@${digest}"
printf '\nPublished: %s\n' "$pinned"
printf '\nUpdate these two places, then re-run scripts/validate.sh:\n'
printf '  1. infra/main.parameters.json -> parameters.containerImage.value\n'
printf '  2. infra/createUiDefinition.json -> steps.advanced.containerImage.defaultValue\n'
printf '\nCustomers who block public registries can import the same bytes:\n'
printf '  az acr import --name <their-acr> --source %s\n' "$pinned"
