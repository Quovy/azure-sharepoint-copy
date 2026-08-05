#!/usr/bin/env bash
# Survey every deployed copy job, stop what is running, and park schedules.
#
# Takes no arguments. Jobs are discovered from Azure by the same workload tag
# copyctl.py uses, so no resource group, job, or account name is written down
# here and the script is identical for every deployment.
#
#   scripts/incident.sh                 report only; changes nothing
#   scripts/incident.sh --stop          also stop executions still running
#   scripts/incident.sh --park          also park every schedule
#   scripts/incident.sh -g RG --park    limit any of it to one deployment
#   scripts/incident.sh --inherit-tags  copy the resource group's tags onto jobs
#
# Reporting is the default because the survey is the part you want first: a
# run that is already hours long is better understood than interrupted blind.
# One subscription can hold several deployments, so anything that acts should
# usually be scoped with -g.
set -euo pipefail

WORKLOAD_TAG="azure-sharepoint-copy"
# The same never-occurring date copyctl.py parks a schedule with.
PARKED_CRON="0 0 31 2 *"

stop=false
park=false
inherit=false
rg_filter=""
while [ $# -gt 0 ]; do
  case "$1" in
    --stop | -s) stop=true ;;
    --park | -p) park=true ;;
    --inherit-tags) inherit=true ;;
    --resource-group | -g)
      shift
      [ $# -gt 0 ] || {
        printf -- '-g needs a resource group name\n' >&2
        exit 2
      }
      rg_filter="$1"
      ;;
    --help | -h)
      sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      printf 'Unknown option: %s (try --help)\n' "$1" >&2
      exit 2
      ;;
  esac
  shift
done

command -v az >/dev/null 2>&1 || {
  printf 'The Azure CLI is required.\n' >&2
  exit 1
}

subscription="$(az account show --query name -o tsv --only-show-errors 2>/dev/null || true)"
[ -n "$subscription" ] || {
  printf 'Not signed in. Run: az login\n' >&2
  exit 1
}
printf 'Subscription: %s\n\n' "$subscription"

jobs="$(az containerapp job list \
  --query "[?tags.workload=='${WORKLOAD_TAG}' && tags.copyJob != null].[resourceGroup,name,tags.copyJob]" \
  -o tsv --only-show-errors)"

if [ -n "$rg_filter" ]; then
  jobs="$(printf '%s\n' "$jobs" | awk -v rg="$rg_filter" 'tolower($1) == tolower(rg)')"
fi

[ -n "$jobs" ] || {
  printf 'No deployed copy jobs found%s.\n' "${rg_filter:+ in $rg_filter}" >&2
  exit 1
}

deployments="$(printf '%s\n' "$jobs" | cut -f1 | sort -u | grep -c . || true)"
if { [ "$stop" = true ] || [ "$park" = true ] || [ "$inherit" = true ]; } && [ "$deployments" -gt 1 ]; then
  printf 'Refusing to act on %s deployments at once. Re-run with -g to choose one:\n' "$deployments" >&2
  printf '%s\n' "$jobs" | cut -f1 | sort -u | sed 's/^/  /' >&2
  exit 2
fi

exit_code=0

while IFS=$'\t' read -r rg resource job; do
  [ -n "$resource" ] || continue
  printf '=== %s (%s/%s)\n' "$job" "$rg" "$resource"

  cron="$(az containerapp job show -g "$rg" -n "$resource" \
    --query "properties.configuration.scheduleTriggerConfig.cronExpression" \
    -o tsv --only-show-errors 2>/dev/null || true)"
  dry="$(az containerapp job show -g "$rg" -n "$resource" \
    --query "properties.template.containers[0].env[?name=='COPY_DRY_RUN'].value|[0]" \
    -o tsv --only-show-errors 2>/dev/null || true)"

  if [ "$cron" = "$PARKED_CRON" ]; then
    printf '  schedule   parked\n'
  else
    printf '  schedule   %s UTC\n' "${cron:-unknown}"
  fi
  printf '  mode       %s\n' "$([ "$dry" = "false" ] && echo "LIVE COPY" || echo "dry run")"

  # Overlapping executions are the failure mode worth catching early: nothing
  # in Container Apps serialises them, so a run longer than its own interval
  # stacks copies of itself.
  running="$(az containerapp job execution list -g "$rg" -n "$resource" \
    --query "[?properties.status=='Running'].name" -o tsv --only-show-errors 2>/dev/null || true)"
  running_count="$(printf '%s' "$running" | grep -c . || true)"
  printf '  running    %s execution(s)\n' "$running_count"

  if [ "$running_count" -gt 1 ]; then
    printf '  WARNING    executions are overlapping; each repeats the others work\n'
    exit_code=1
  fi

  # A tenant policy can require tags this template never sets, which blocks
  # every later write to the job - including parking it. Comparing against the
  # rest of the resource group shows what is expected without naming it here.
  az resource list -g "$rg" --query "[].{name:name,tags:tags}" -o json --only-show-errors 2>/dev/null |
    RESOURCE="$resource" WORKLOAD_TAG="$WORKLOAD_TAG" python3 -c '
import json, os, sys

rows = json.load(sys.stdin) or []
workload = os.environ["WORKLOAD_TAG"].lower()

def keys(row):
    # Azure tag names are case-insensitive, so compare them folded or a
    # template "workload" reads as missing beside a tenant "Workload".
    return {k.lower(): k for k in (row.get("tags") or {})}

def is_ours(row):
    tags = {k.lower(): v for k, v in (row.get("tags") or {}).items()}
    return str(tags.get("workload", "")).lower() == workload

me = next((r for r in rows if r["name"] == os.environ["RESOURCE"]), None)
mine = keys(me or {})
print("  tags       " + (", ".join(sorted((me or {}).get("tags") or {})) or "(none)"))

# Compare against resources this template did not create. The rest of the
# deployment carries the same two tags, so intersecting everything would hide
# exactly the tenant-mandated set a deny-without-tags policy cares about.
outside = [keys(r) for r in rows if not is_ours(r)]
if not outside:
    print("  tag hint   nothing outside this deployment to compare against")
    sys.exit(0)

# A simple majority rather than a unanimous one: incidental resources such as a
# NIC or an OS disk are often left untagged, and requiring every one of them to
# agree lets a single bare resource hide the whole mandated set.
counts, casing = {}, {}
for row in outside:
    for lowered, original in row.items():
        counts[lowered] = counts.get(lowered, 0) + 1
        casing.setdefault(lowered, original)
threshold = max(1, len(outside) // 2)
missing = sorted(
    casing[k] for k, n in counts.items() if n >= threshold and k not in mine
)
print("  tag hint   compared against %d resource(s) outside this deployment" % len(outside))
if missing:
    print("  MISSING    most of them also carry: " + ", ".join(missing))
    print("             a deny-without-tags policy would block updates to this job")
' || printf '  tags       unreadable\n'

  if [ "$inherit" = true ]; then
    # Azure does not push resource group tags down to the resources inside it.
    # Where a tenant policy demands tags the group already carries, copying
    # them down is both the fix and a request that policy will accept.
    group_tags="$(az group show -n "$rg" --query tags -o json --only-show-errors 2>/dev/null || true)"
    own_tags="$(az containerapp job show -g "$rg" -n "$resource" --query tags -o json --only-show-errors 2>/dev/null || true)"
    tag_args=()
    while IFS= read -r -d '' pair; do
      tag_args+=("$pair")
    done < <(
      GROUP_TAGS="$group_tags" OWN_TAGS="$own_tags" python3 -c '
import json, os, sys

def load(name):
    try:
        return json.loads(os.environ.get(name) or "{}") or {}
    except json.JSONDecodeError:
        return {}

group, mine = load("GROUP_TAGS"), load("OWN_TAGS")
# The jobs own tags win: workload and copyJob identify the deployment and
# discovery depends on them. Folded comparison so a group "Workload" does not
# arrive alongside the template lowercase "workload" as a second tag.
folded = {k.lower() for k in mine}
merged = dict(mine)
merged.update({k: v for k, v in group.items() if k.lower() not in folded})
for key, value in merged.items():
    sys.stdout.write("%s=%s\0" % (key, value))
' 2>/dev/null || true
    )
    if [ "${#tag_args[@]}" -eq 0 ]; then
      printf '  tags       nothing to inherit; the resource group has no tags\n'
    elif tag_error="$(az containerapp job update -g "$rg" -n "$resource" \
      --tags "${tag_args[@]}" --only-show-errors -o none 2>&1)"; then
      printf '  tagged     now carries %s tag(s), inherited from %s\n' "${#tag_args[@]}" "$rg"
    else
      printf '  FAILED     could not set tags\n'
      printf '             %s\n' "$(printf '%s' "$tag_error" | head -3)"
      exit_code=1
    fi
  fi

  if [ "$stop" = true ] && [ -n "$running" ]; then
    while IFS= read -r execution; do
      [ -n "$execution" ] || continue
      printf '  stopping   %s\n' "$execution"
      az containerapp job stop -g "$rg" -n "$resource" \
        --job-execution-name "$execution" --only-show-errors -o none 2>/dev/null ||
        printf '  FAILED     could not stop %s\n' "$execution"
    done <<<"$running"
  fi

  if [ "$park" = true ] && [ "$cron" != "$PARKED_CRON" ]; then
    # Restating the existing tags keeps a request a deny-without-tags policy
    # will accept. Tag values routinely contain spaces and commas, so they are
    # carried as separate array elements rather than one split-on-whitespace
    # string - the latter turns "Contoso Inc, Ltd" into four bad arguments.
    tag_args=()
    while IFS= read -r -d '' pair; do
      tag_args+=("$pair")
    done < <(
      az containerapp job show -g "$rg" -n "$resource" \
        --query "tags" -o json --only-show-errors 2>/dev/null |
        python3 -c '
import json, sys
for k, v in (json.load(sys.stdin) or {}).items():
    sys.stdout.write("%s=%s\0" % (k, v))
' 2>/dev/null || true
    )

    park_error=""
    if [ ${#tag_args[@]} -gt 0 ]; then
      park_error="$(az containerapp job update -g "$rg" -n "$resource" \
        --cron-expression "$PARKED_CRON" --tags "${tag_args[@]}" \
        --only-show-errors -o none 2>&1)" || park_error="${park_error:-unknown error}"
    else
      park_error="$(az containerapp job update -g "$rg" -n "$resource" \
        --cron-expression "$PARKED_CRON" \
        --only-show-errors -o none 2>&1)" || park_error="${park_error:-unknown error}"
    fi

    # Confirm against Azure rather than the exit status: a request can be
    # accepted and still not be the schedule you asked for.
    now="$(az containerapp job show -g "$rg" -n "$resource" \
      --query "properties.configuration.scheduleTriggerConfig.cronExpression" \
      -o tsv --only-show-errors 2>/dev/null || true)"
    if [ "$now" = "$PARKED_CRON" ]; then
      printf '  parked     schedule will not fire until copyctl.py enable\n'
    else
      printf '  FAILED     schedule is still %s\n' "${now:-unknown}"
      [ -n "$park_error" ] && printf '             %s\n' "$(printf '%s' "$park_error" | head -3)"
      exit_code=1
    fi
  fi

  printf '\n'
done <<<"$jobs"

if [ "$stop" = false ] && [ "$park" = false ] && [ "$inherit" = false ]; then
  printf 'Report only. Re-run with --stop and/or --park to act.\n'
fi

exit "$exit_code"
