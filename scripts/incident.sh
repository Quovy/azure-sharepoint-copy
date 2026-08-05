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
rg_filter=""
while [ $# -gt 0 ]; do
  case "$1" in
    --stop | -s) stop=true ;;
    --park | -p) park=true ;;
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
if { [ "$stop" = true ] || [ "$park" = true ]; } && [ "$deployments" -gt 1 ]; then
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
    sys.exit(0)
common = set.intersection(*(set(o) for o in outside))
missing = sorted(outside[0][k] for k in common - set(mine))
if missing:
    print("  MISSING    resources outside this deployment all carry: " + ", ".join(missing))
    print("             a deny-without-tags policy would block updates to this job")
' || printf '  tags       unreadable\n'

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
    # Restating the existing tags keeps a request that a deny-without-tags
    # policy will accept, when the job already carries what it demands.
    tags="$(az containerapp job show -g "$rg" -n "$resource" \
      --query "tags" -o json --only-show-errors 2>/dev/null |
      python3 -c 'import json,sys; print(" ".join(f"{k}={v}" for k,v in (json.load(sys.stdin) or {}).items()))' 2>/dev/null || true)"
    # shellcheck disable=SC2086
    if az containerapp job update -g "$rg" -n "$resource" \
      --cron-expression "$PARKED_CRON" ${tags:+--tags $tags} \
      --only-show-errors -o none 2>/dev/null; then
      printf '  parked     schedule will not fire until copyctl.py enable\n'
    else
      printf '  FAILED     could not park; if a tag policy denied this, add the\n'
      printf '             missing tags above in the portal Tags blade first\n'
      exit_code=1
    fi
  fi

  printf '\n'
done <<<"$jobs"

if [ "$stop" = false ] && [ "$park" = false ]; then
  printf 'Report only. Re-run with --stop and/or --park to act.\n'
fi

exit "$exit_code"
