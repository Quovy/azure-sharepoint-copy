#!/bin/sh
# Exercises app/transfer.sh against the exact environment copyctl.py generates,
# so the deployment tool and the runtime cannot drift apart.
#
# Each case runs in a ( ... ) subshell so its environment overrides cannot leak
# into the next one. shellcheck flags that isolation as a possible mistake here,
# and reads the exported JSON manifest as if it were a command.
# shellcheck disable=SC2030,SC2031,SC2089,SC2090
set -eu

project_root="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
test_root="$(mktemp -d)"
trap 'rm -rf "$test_root"' EXIT HUP INT TERM

failures=0

fake_rclone="$test_root/rclone"
cat >"$fake_rclone" <<'SCRIPT'
#!/bin/sh
printf 'SOURCE_TYPE_CONFIG=%s\n' "${RCLONE_CONFIG_SOURCE_TYPE:-}"
printf 'SOURCE_ENV_AUTH=%s\n' "${RCLONE_CONFIG_SOURCE_ENV_AUTH:-}"
printf 'SOURCE_SHARE_NAME=%s\n' "${RCLONE_CONFIG_SOURCE_SHARE_NAME:-}"
printf 'SOURCE_USE_MSI=%s\n' "${RCLONE_CONFIG_SOURCE_USE_MSI:-}"
printf 'SOURCE_MSI_CLIENT_ID=%s\n' "${RCLONE_CONFIG_SOURCE_MSI_CLIENT_ID:-}"
printf 'DEST_DRIVE_ID=%s\n' "${RCLONE_CONFIG_DESTINATION_DRIVE_ID:-}"
printf 'AZURE_CLIENT_ID=%s\n' "${AZURE_CLIENT_ID:-}"
printf '%s\n' "$@"
exit "${FAKE_RCLONE_EXIT_CODE:-0}"
SCRIPT
chmod 0755 "$fake_rclone"

# Stands in for Microsoft Entra and Microsoft Graph.
fake_curl="$test_root/curl"
cat >"$fake_curl" <<'SCRIPT'
#!/bin/sh
url=""
for argument in "$@"; do
  case "$argument" in
    https://*) url="$argument" ;;
  esac
done
case "$url" in
  *login.microsoftonline.com*)
    [ "${FAKE_TOKEN_FAIL:-false}" != "true" ] || exit 22
    printf '{"access_token":"fake-token","expires_in":3599}\n'
    ;;
  *graph.microsoft.com/v1.0/sites/*/drives*)
    printf '{"value":[{"id":"drive-other","name":"Site Assets"},{"id":"%s","name":"%s"}]}\n' \
      "${FAKE_DRIVE_ID:-drive-1}" "${FAKE_LIBRARY_NAME:-Documents}"
    ;;
  *graph.microsoft.com/v1.0/sites/*)
    printf '{"id":"contoso.sharepoint.com,site-guid,web-guid"}\n'
    ;;
  *)
    printf 'unexpected url: %s\n' "$url" >&2
    exit 1
    ;;
esac
SCRIPT
chmod 0755 "$fake_curl"

base_env() {
  # The job configuration comes from copyctl.py rather than a hand-written
  # fixture, so a new or renamed field fails this test immediately.
  # COPY_JOBS_DIR is pinned to the repo's samples: an inherited value would
  # silently test some other job file.
  eval "$(
    cd "$project_root" && COPY_JOBS_DIR="$project_root/jobs" python3 copyctl.py env default |
      sed "s/^\([A-Z_]*\)=\(.*\)$/export \1='\2'/"
  )"
  export RCLONE_BIN="$fake_rclone"
  export CURL_BIN="$fake_curl"
  export JQ_BIN="${JQ_BIN:-jq}"
  export AZURE_MANAGED_IDENTITY_CLIENT_ID=00000000-0000-0000-0000-000000000003
  export RCLONE_CONFIG_DESTINATION_CLIENT_SECRET='test-only-secret~with+special/chars='
  unset FAKE_RCLONE_EXIT_CODE FAKE_TOKEN_FAIL FAKE_DRIVE_ID FAKE_LIBRARY_NAME || true
}

report() {
  printf '  %s: %s\n' "$1" "$2" >&2
  failures=$((failures + 1))
}

assert_contains() {
  printf '%s\n' "$2" | grep -F -- "$3" >/dev/null || report "$1" "expected to find '$3'"
}

assert_missing() {
  printf '%s\n' "$2" | grep -F -- "$3" >/dev/null && report "$1" "did not expect '$3'"
  return 0
}

run_transfer() {
  "$project_root/app/transfer.sh" 2>&1
}

# --- the default job file, unmodified ---------------------------------------
(
  base_env
  output="$(run_transfer)" || report "default" "exited non-zero"
  assert_contains "default" "$output" "SOURCE_TYPE_CONFIG=azurefiles"
  assert_contains "default" "$output" "SOURCE_ENV_AUTH=true"
  assert_contains "default" "$output" "SOURCE_SHARE_NAME=source-share"
  assert_contains "default" "$output" "AZURE_CLIENT_ID=00000000-0000-0000-0000-000000000003"
  assert_contains "default" "$output" "DEST_DRIVE_ID=drive-1"
  assert_contains "default" "$output" "--dry-run"
  assert_contains "default" "$output" "--ignore-existing"
  # SharePoint rewrites Office files, so size and hash comparison must stay off
  # or every such file is re-uploaded on every run.
  assert_contains "default" "$output" "--ignore-size"
  assert_contains "default" "$output" "--ignore-checksum"
  assert_contains "default" "$output" "--create-empty-src-dirs"
  assert_contains "default" "$output" "dry_run=true"
  assert_missing "default" "$output" "sync"
  [ "$failures" -eq 0 ] || exit 1
) || failures=$((failures + 1))

# --- live mode drops --dry-run ----------------------------------------------
(
  base_env
  export COPY_DRY_RUN=false
  output="$(run_transfer)" || report "live" "exited non-zero"
  assert_missing "live" "$output" "--dry-run"
  assert_contains "live" "$output" "dry_run=false"
  [ "$failures" -eq 0 ] || exit 1
) || failures=$((failures + 1))

# --- replace_if_changed drops --ignore-existing -----------------------------
(
  base_env
  export COPY_EXISTING_FILES=replace_if_changed
  output="$(run_transfer)" || report "replace" "exited non-zero"
  assert_missing "replace" "$output" "--ignore-existing"
  assert_contains "replace" "$output" "copy_mode=copy_changed"
  [ "$failures" -eq 0 ] || exit 1
) || failures=$((failures + 1))

# --- ADLS Gen2 uses the blob backend and an explicit MSI client id ----------
(
  base_env
  export SOURCE_TYPE=adls_gen2
  output="$(run_transfer)" || report "adls" "exited non-zero"
  assert_contains "adls" "$output" "SOURCE_TYPE_CONFIG=azureblob"
  assert_contains "adls" "$output" "SOURCE_USE_MSI=true"
  assert_contains "adls" "$output" "SOURCE_MSI_CLIENT_ID=00000000-0000-0000-0000-000000000003"
  assert_contains "adls" "$output" "source:source-share"
  [ "$failures" -eq 0 ] || exit 1
) || failures=$((failures + 1))

# --- an include manifest switches selection and adds --files-from-raw -------
(
  base_env
  export SOURCE_INCLUDE_PATHS='["Invoices/2026","Reports/monthly.xlsx"]'
  output="$(run_transfer)" || report "manifest" "exited non-zero"
  assert_contains "manifest" "$output" "--files-from-raw"
  assert_contains "manifest" "$output" "--no-traverse"
  assert_contains "manifest" "$output" "selection=manifest"
  [ "$failures" -eq 0 ] || exit 1
) || failures=$((failures + 1))

# --- a modified-on-or-after cutoff becomes --max-age ------------------------
(
  base_env
  export SOURCE_MODIFIED_ON_OR_AFTER=2026-07-01
  output="$(run_transfer)" || report "max-age" "exited non-zero"
  assert_contains "max-age" "$output" "--max-age"
  # A date cutoff filters files only; recreating every visited directory would
  # mirror the full empty folder tree, so the flag must be dropped here.
  assert_missing "max-age" "$output" "--create-empty-src-dirs"
  [ "$failures" -eq 0 ] || exit 1
) || failures=$((failures + 1))

# --- emptyFolders overrides the automatic behaviour in both directions ------
(
  base_env
  export SOURCE_MODIFIED_ON_OR_AFTER=2026-07-01
  export COPY_EMPTY_FOLDERS=always
  output="$(run_transfer)" || report "empty-folders-always" "exited non-zero"
  assert_contains "empty-folders-always" "$output" "--create-empty-src-dirs"
  [ "$failures" -eq 0 ] || exit 1
) || failures=$((failures + 1))

(
  base_env
  export COPY_EMPTY_FOLDERS=never
  output="$(run_transfer)" || report "empty-folders-never" "exited non-zero"
  assert_missing "empty-folders-never" "$output" "--create-empty-src-dirs"
  [ "$failures" -eq 0 ] || exit 1
) || failures=$((failures + 1))

# --- rejected configurations ------------------------------------------------
expect_rejection() {
  label="$1"
  expected="$2"
  output="$(run_transfer)" && {
    report "$label" "expected a non-zero exit"
    return 0
  }
  assert_contains "$label" "$output" "$expected"
}

(
  base_env
  export SOURCE_PATH=../outside
  expect_rejection "traversal" "SOURCE_PATH_contains_parent_traversal"
  [ "$failures" -eq 0 ] || exit 1
) || failures=$((failures + 1))

(
  base_env
  export SOURCE_INCLUDE_PATHS='["a"]'
  export SOURCE_MODIFIED_ON_OR_AFTER=2026-07-01
  expect_rejection "combination" "cannot_be_combined"
  [ "$failures" -eq 0 ] || exit 1
) || failures=$((failures + 1))

(
  base_env
  export COPY_DRY_RUN=True
  expect_rejection "boolean-case" "COPY_DRY_RUN_must_be_true_or_false"
  [ "$failures" -eq 0 ] || exit 1
) || failures=$((failures + 1))

(
  base_env
  export COPY_EMPTY_FOLDERS=sometimes
  expect_rejection "bad-empty-folders" "unsupported_COPY_EMPTY_FOLDERS"
  [ "$failures" -eq 0 ] || exit 1
) || failures=$((failures + 1))

(
  base_env
  export DEST_LIBRARY="No Such Library"
  expect_rejection "missing-library" "document_library_not_found"
  [ "$failures" -eq 0 ] || exit 1
) || failures=$((failures + 1))

(
  base_env
  export FAKE_TOKEN_FAIL=true
  expect_rejection "bad-credential" "entra_rejected_the_client_id_or_secret"
  [ "$failures" -eq 0 ] || exit 1
) || failures=$((failures + 1))

# --- rclone's exit status is propagated -------------------------------------
(
  base_env
  export FAKE_RCLONE_EXIT_CODE=7
  output="$(run_transfer)" && report "exit-code" "expected a non-zero exit"
  printf '%s\n' "$output" | grep -F 'exit_code=7' >/dev/null ||
    report "exit-code" "expected the completion record to carry exit_code=7"
  [ "$failures" -eq 0 ] || exit 1
) || failures=$((failures + 1))

if [ "$failures" -ne 0 ]; then
  printf 'transfer tests FAILED (%s)\n' "$failures" >&2
  exit 1
fi
printf 'transfer tests passed\n'
