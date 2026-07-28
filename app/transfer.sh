#!/bin/sh
# One-way copy from Azure Files or ADLS Gen2 to one SharePoint document library.
#
# All configuration arrives as environment variables set by the Azure Container
# Apps job definition. Nothing is read from disk and nothing is downloaded, so
# `az containerapp job show` is a complete and current record of what this
# container will do.
# jq programs are single-quoted on purpose: $library, $path and $value are jq
# variables bound with --arg, not shell expansions.
# shellcheck disable=SC2016
set -eu

readonly RCLONE_BIN="${RCLONE_BIN:-rclone}"
readonly JQ_BIN="${JQ_BIN:-jq}"
readonly CURL_BIN="${CURL_BIN:-curl}"

fail() {
  printf 'configuration_error=%s\n' "$1" >&2
  exit 64
}

runtime_fail() {
  printf 'runtime_error=%s\n' "$1" >&2
  exit 65
}

require_var() {
  var_name="$1"
  eval "var_value=\${${var_name}:-}"
  [ -n "$var_value" ] || fail "missing_${var_name}"
}

reject_unsafe_text() {
  label="$1"
  value="$2"
  newline='
'
  carriage_return="$(printf '\r')"
  tab="$(printf '\t')"

  case "$value" in
    *"$newline"* | *"$carriage_return"* | *"$tab"*) fail "${label}_contains_control_character" ;;
  esac
}

validate_relative_path() {
  label="$1"
  value="$2"

  reject_unsafe_text "$label" "$value"
  case "$value" in
    /*) fail "${label}_must_be_relative" ;;
    *:*) fail "${label}_contains_colon" ;;
    .. | ../* | */../* | */..) fail "${label}_contains_parent_traversal" ;;
  esac
}

validate_storage_name() {
  value="$1"
  case "$value" in
    "" | *[!a-z0-9-]* | -* | *-) fail "invalid_storage_container_or_share" ;;
  esac
}

for required in \
  COPY_JOB_NAME \
  SOURCE_TYPE \
  SOURCE_STORAGE_ACCOUNT \
  SOURCE_CONTAINER_OR_SHARE \
  DEST_TENANT_ID \
  DEST_CLIENT_ID \
  DEST_SITE_URL \
  DEST_LIBRARY \
  COPY_EXISTING_FILES \
  COPY_DRY_RUN \
  RCLONE_CONFIG_DESTINATION_CLIENT_SECRET \
  AZURE_MANAGED_IDENTITY_CLIENT_ID
do
  require_var "$required"
done

# Optional values still have to be defined so `set -u` stays on.
SOURCE_PATH="${SOURCE_PATH:-}"
DEST_PATH="${DEST_PATH:-}"
SOURCE_INCLUDE_PATHS="${SOURCE_INCLUDE_PATHS:-[]}"
SOURCE_MODIFIED_ON_OR_AFTER="${SOURCE_MODIFIED_ON_OR_AFTER:-}"

case "$SOURCE_TYPE" in
  azure_files | adls_gen2) ;;
  *) fail "unsupported_SOURCE_TYPE" ;;
esac
case "$COPY_EXISTING_FILES" in
  skip) COPY_MODE=new_only ;;
  replace_if_changed) COPY_MODE=copy_changed ;;
  *) fail "unsupported_COPY_EXISTING_FILES" ;;
esac
case "$COPY_DRY_RUN" in
  true | false) ;;
  *) fail "COPY_DRY_RUN_must_be_true_or_false" ;;
esac
case "$SOURCE_STORAGE_ACCOUNT" in
  *[!a-z0-9]* | "") fail "invalid_SOURCE_STORAGE_ACCOUNT" ;;
esac
validate_storage_name "$SOURCE_CONTAINER_OR_SHARE"
validate_relative_path "SOURCE_PATH" "$SOURCE_PATH"
validate_relative_path "DEST_PATH" "$DEST_PATH"
reject_unsafe_text "DEST_LIBRARY" "$DEST_LIBRARY"
reject_unsafe_text "DEST_SITE_URL" "$DEST_SITE_URL"

case "$DEST_SITE_URL" in
  https://*.*/*) ;;
  *) fail "DEST_SITE_URL_must_be_an_https_site_url_with_a_path" ;;
esac
case "$DEST_SITE_URL" in
  *'?'* | *'#'*) fail "DEST_SITE_URL_must_not_contain_a_query_or_fragment" ;;
esac

if ! printf '%s' "$SOURCE_INCLUDE_PATHS" |
  "$JQ_BIN" -e 'type == "array" and all(.[]; type == "string")' >/dev/null 2>&1; then
  fail "SOURCE_INCLUDE_PATHS_must_be_a_JSON_array_of_strings"
fi
include_count="$(printf '%s' "$SOURCE_INCLUDE_PATHS" | "$JQ_BIN" -r 'length')"

if [ "$include_count" -gt 0 ] && [ -n "$SOURCE_MODIFIED_ON_OR_AFTER" ]; then
  fail "SOURCE_INCLUDE_PATHS_and_SOURCE_MODIFIED_ON_OR_AFTER_cannot_be_combined"
fi
if [ -n "$SOURCE_MODIFIED_ON_OR_AFTER" ]; then
  case "$SOURCE_MODIFIED_ON_OR_AFTER" in
    ????-??-?? | ????-??-??T??:??:??Z) ;;
    *) fail "SOURCE_MODIFIED_ON_OR_AFTER_must_be_YYYY-MM-DD_or_UTC_timestamp" ;;
  esac
fi

manifest_file=""
header_file=""
# shellcheck disable=SC2329
cleanup() {
  [ -z "$manifest_file" ] || [ ! -f "$manifest_file" ] || rm -f "$manifest_file"
  [ -z "$header_file" ] || [ ! -f "$header_file" ] || rm -f "$header_file"
}
trap cleanup EXIT HUP INT TERM

# --- Resolve the destination document library -------------------------------
#
# The library is resolved on every run rather than pinned at deployment time.
# A renamed or replaced library fails loudly here instead of copying into the
# wrong place, and the deployment template never has to call Microsoft Graph.

url_encode() {
  "$JQ_BIN" -rn --arg value "$1" '$value | @uri'
}

graph_token() {
  printf 'client_id=%s&client_secret=%s&scope=%s&grant_type=client_credentials' \
    "$(url_encode "$DEST_CLIENT_ID")" \
    "$(url_encode "$RCLONE_CONFIG_DESTINATION_CLIENT_SECRET")" \
    "$(url_encode 'https://graph.microsoft.com/.default')" |
    "$CURL_BIN" \
      --silent --show-error --fail-with-body \
      --request POST \
      --header 'Content-Type: application/x-www-form-urlencoded' \
      --data-binary @- \
      --max-time 60 \
      "https://login.microsoftonline.com/${DEST_TENANT_ID}/oauth2/v2.0/token"
}

graph_get() {
  url="$1"
  # Microsoft Graph supplies continuation URLs. Refuse to follow one anywhere
  # other than Graph so a manipulated response cannot redirect the token.
  case "$url" in
    https://graph.microsoft.com/*) ;;
    *) runtime_fail "graph_returned_an_unsafe_continuation_url" ;;
  esac
  "$CURL_BIN" \
    --silent --show-error --fail-with-body \
    --header "@${header_file}" \
    --header 'Accept: application/json' \
    --max-time 60 \
    "$url"
}

token_response="$(graph_token)" ||
  runtime_fail "entra_rejected_the_client_id_or_secret"
access_token="$(printf '%s' "$token_response" | "$JQ_BIN" -r '.access_token // empty')"
unset token_response
[ -n "$access_token" ] || runtime_fail "entra_returned_no_access_token"

# The bearer token goes in a private file rather than the curl argument list.
header_file="$(mktemp /tmp/azure-sharepoint-copy-header.XXXXXX)"
chmod 0600 "$header_file"
printf 'Authorization: Bearer %s\n' "$access_token" >"$header_file"
unset access_token

site_without_scheme="${DEST_SITE_URL#https://}"
site_host="${site_without_scheme%%/*}"
site_path="${site_without_scheme#*/}"
encoded_site_path="$(
  "$JQ_BIN" -rn --arg path "$site_path" \
    '$path | split("/") | map(select(length > 0) | @uri) | join("/")'
)"
[ -n "$encoded_site_path" ] || fail "DEST_SITE_URL_must_include_a_site_path"

site_response="$(
  graph_get "https://graph.microsoft.com/v1.0/sites/${site_host}:/${encoded_site_path}?\$select=id"
)" || runtime_fail "graph_denied_or_failed_the_site_lookup__confirm_Sites.Selected_and_the_site_grant"
site_id="$(printf '%s' "$site_response" | "$JQ_BIN" -r '.id // empty')"
[ -n "$site_id" ] || runtime_fail "graph_returned_an_incomplete_site_response"

drive_id=""
library_names=""
next_url="https://graph.microsoft.com/v1.0/sites/$(url_encode "$site_id")/drives?\$select=id,name"
while [ -n "$next_url" ]; do
  drives_response="$(graph_get "$next_url")" || runtime_fail "graph_failed_to_list_document_libraries"
  drive_id="$(
    printf '%s' "$drives_response" |
      "$JQ_BIN" -r --arg library "$DEST_LIBRARY" \
        '[.value[]? | select(.name == $library) | .id] | first // empty'
  )"
  [ -z "$drive_id" ] || break
  library_names="${library_names}$(printf '%s' "$drives_response" | "$JQ_BIN" -r '[.value[]?.name] | join(", ")') "
  next_url="$(printf '%s' "$drives_response" | "$JQ_BIN" -r '."@odata.nextLink" // empty')"
done

if [ -z "$drive_id" ]; then
  printf 'visible_libraries=%s\n' "${library_names:-(none)}" >&2
  runtime_fail "document_library_not_found__check_DEST_LIBRARY"
fi
rm -f "$header_file"
header_file=""

# --- Source and destination remotes -----------------------------------------

export RCLONE_CONFIG_SOURCE_ACCOUNT="$SOURCE_STORAGE_ACCOUNT"
case "$SOURCE_TYPE" in
  azure_files)
    # The Azure Files backend authenticates through the ambient environment
    # credential; only the blob backend takes explicit MSI options.
    export RCLONE_CONFIG_SOURCE_TYPE=azurefiles
    export RCLONE_CONFIG_SOURCE_ENV_AUTH=true
    export RCLONE_CONFIG_SOURCE_SHARE_NAME="$SOURCE_CONTAINER_OR_SHARE"
    export AZURE_CLIENT_ID="$AZURE_MANAGED_IDENTITY_CLIENT_ID"
    source_remote="source:"
    [ -z "$SOURCE_PATH" ] || source_remote="${source_remote}${SOURCE_PATH}"
    ;;
  adls_gen2)
    export RCLONE_CONFIG_SOURCE_TYPE=azureblob
    export RCLONE_CONFIG_SOURCE_USE_MSI=true
    export RCLONE_CONFIG_SOURCE_MSI_CLIENT_ID="$AZURE_MANAGED_IDENTITY_CLIENT_ID"
    source_remote="source:${SOURCE_CONTAINER_OR_SHARE}"
    [ -z "$SOURCE_PATH" ] || source_remote="${source_remote}/${SOURCE_PATH}"
    ;;
esac

export RCLONE_CONFIG_DESTINATION_TYPE=onedrive
export RCLONE_CONFIG_DESTINATION_TENANT="$DEST_TENANT_ID"
export RCLONE_CONFIG_DESTINATION_CLIENT_ID="$DEST_CLIENT_ID"
export RCLONE_CONFIG_DESTINATION_CLIENT_CREDENTIALS=true
export RCLONE_CONFIG_DESTINATION_DRIVE_ID="$drive_id"
export RCLONE_CONFIG_DESTINATION_DRIVE_TYPE=documentLibrary
export RCLONE_CONFIG_DESTINATION_DISABLE_SITE_PERMISSION=true

destination_remote="destination:"
[ -z "$DEST_PATH" ] || destination_remote="${destination_remote}${DEST_PATH}"

# `copy` never deletes at the destination. `sync` must never appear here.
set -- copy "$source_remote" "$destination_remote" \
  --create-empty-src-dirs \
  --checkers "${RCLONE_CHECKERS:-8}" \
  --transfers "${RCLONE_TRANSFERS:-4}" \
  --contimeout 30s \
  --timeout 5m \
  --retries 3 \
  --low-level-retries 10 \
  --onedrive-upload-cutoff 4Mi \
  --onedrive-chunk-size 10Mi \
  --stats 30s \
  --use-json-log \
  --log-level INFO

selection=folder
if [ "$include_count" -gt 0 ]; then
  selection=manifest
  manifest_file="$(mktemp /tmp/azure-sharepoint-copy-manifest.XXXXXX)"
  printf '%s' "$SOURCE_INCLUDE_PATHS" | "$JQ_BIN" -r '.[]' >"$manifest_file"
  while IFS= read -r manifest_path || [ -n "$manifest_path" ]; do
    [ -n "$manifest_path" ] || fail "manifest_contains_empty_path"
    validate_relative_path "manifest_path" "$manifest_path"
  done <"$manifest_file"
  # --files-from-raw overrides other filters, which is why it cannot be
  # combined with SOURCE_MODIFIED_ON_OR_AFTER.
  set -- "$@" --files-from-raw "$manifest_file" --no-traverse
fi

if [ "$COPY_MODE" = "new_only" ]; then
  set -- "$@" --ignore-existing
fi
if [ -n "$SOURCE_MODIFIED_ON_OR_AFTER" ]; then
  set -- "$@" --max-age "$SOURCE_MODIFIED_ON_OR_AFTER"
fi
if [ "$COPY_DRY_RUN" = "true" ]; then
  set -- "$@" --dry-run
fi

printf 'transfer_start job=%s source_type=%s source=%s destination=%s library=%s copy_mode=%s selection=%s dry_run=%s\n' \
  "$COPY_JOB_NAME" "$SOURCE_TYPE" "$source_remote" "$destination_remote" \
  "$DEST_LIBRARY" "$COPY_MODE" "$selection" "$COPY_DRY_RUN"

# rclone is expected to fail sometimes. Capture its status rather than letting
# `set -e` exit before the completion record is written.
if "$RCLONE_BIN" "$@"; then
  exit_code=0
else
  exit_code=$?
fi

printf 'transfer_complete job=%s dry_run=%s exit_code=%s\n' \
  "$COPY_JOB_NAME" "$COPY_DRY_RUN" "$exit_code"
exit "$exit_code"
