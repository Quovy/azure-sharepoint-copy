#!/usr/bin/env python3
"""Configuration validation tests. No Azure access required."""

import copy
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COPYCTL = ROOT / "copyctl.py"
JOB = json.loads((ROOT / "jobs" / "default.json").read_text())

failures = []


def read_reference(path):
    uncommented = "\n".join(
        line for line in path.read_text().splitlines() if not line.lstrip().startswith("//")
    )
    return json.loads(uncommented)


def run_validate(jobs):
    with tempfile.TemporaryDirectory() as directory:
        jobs_path = Path(directory) / "jobs"
        jobs_path.mkdir()
        for filename, config in jobs.items():
            (jobs_path / filename).write_text(json.dumps(config))
        return subprocess.run(
            [sys.executable, str(COPYCTL), "validate"],
            env={**os.environ, "COPY_JOBS_DIR": str(jobs_path)},
            text=True,
            capture_output=True,
            check=False,
        )


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")


def expect_success(label, jobs):
    result = run_validate(jobs)
    check(label, result.returncode == 0, result.stderr.strip())


def expect_failure(label, jobs, message):
    result = run_validate(jobs)
    check(label, result.returncode != 0, "expected validation to fail")
    check(label, message in result.stderr, f"expected '{message}' in: {result.stderr.strip()}")


def with_job(**changes):
    job = copy.deepcopy(JOB)
    for dotted, value in changes.items():
        section, _, field = dotted.partition("__")
        if field:
            job[section][field] = value
        else:
            job[section] = value
    return {"default.json": job}


# The commented reference and the deployable sample must stay identical.
check(
    "jsonc-drift",
    read_reference(ROOT / "jobs" / "example.jsonc") == JOB,
    "jobs/example.jsonc and jobs/default.json have drifted apart",
)

expect_success("baseline", with_job())

expect_failure("quoted-boolean", with_job(copy__dryRun="true"), "must be true or false, without quotes")
expect_failure("traversal", with_job(source__path="../outside"), "must not contain '..'")
expect_failure("absolute-path", with_job(source__path="/etc"), "must be relative")
expect_failure("bad-date", with_job(source__modifiedOnOrAfter="2026-13-40"), "must be YYYY-MM-DD")
expect_failure("bad-site-url", with_job(destination__siteUrl="http://contoso.sharepoint.com/sites/x"), "HTTPS site URL")
expect_failure(
    "site-url-with-query",
    with_job(destination__siteUrl="https://contoso.sharepoint.com/sites/x?a=1"),
    "without a query or fragment",
)
expect_failure("unknown-field", with_job(destination__clientSecret="must-never-be-here"), "unknown field")
expect_failure("missing-field", {"default.json": {"name": "default"}}, "missing required field")
expect_failure("empty-jobs-dir", {}, "at least one .json")

renamed = copy.deepcopy(JOB)
renamed["name"] = "not-default"
expect_failure("filename-mismatch", {"default.json": renamed}, "must match its filename")

expect_success("include-paths", with_job(source__includePaths=["Invoices/2026", "Reports/monthly.xlsx"]))
combined = copy.deepcopy(JOB)
combined["source"]["includePaths"] = ["Invoices/2026"]
combined["source"]["modifiedOnOrAfter"] = "2026-07-01"
expect_failure("filters-combined", {"default.json": combined}, "cannot be used together")

duplicate = copy.deepcopy(JOB)
duplicate["source"]["includePaths"] = ["a", "a"]
expect_failure("duplicate-include", {"default.json": duplicate}, "duplicate path")

# --- cron validation --------------------------------------------------------
expect_failure("cron-too-few-fields", with_job(copy__scheduleUtc="0 2 * *"), "exactly five")
expect_failure("cron-minute-range", with_job(copy__scheduleUtc="70 2 * * *"), "minute value out of range")
expect_failure("cron-hour-range", with_job(copy__scheduleUtc="0 25 * * *"), "hour value out of range")
expect_failure("cron-reversed-range", with_job(copy__scheduleUtc="0 5-2 * * *"), "reversed hour range")
expect_failure("cron-bad-step", with_job(copy__scheduleUtc="*/0 2 * * *"), "invalid step")
expect_success("cron-weekday-range", with_job(copy__scheduleUtc="0 2 * * 1-5"))

# --- overlap protection -----------------------------------------------------
overlap = copy.deepcopy(JOB)
overlap["copy"]["scheduleUtc"] = "0 * * * *"
overlap["copy"]["timeoutMinutes"] = 360
expect_failure("overlapping-schedule", {"default.json": overlap}, "executions would overlap")

fits = copy.deepcopy(JOB)
fits["copy"]["scheduleUtc"] = "0 * * * *"
fits["copy"]["timeoutMinutes"] = 30
expect_success("non-overlapping-schedule", {"default.json": fits})

# --- containerOrShare / path must not be shifted by one level ---------------
expect_failure(
    "container-equals-storage-account",
    with_job(source__containerOrShare="examplestorage"),
    "containerOrShare equals storageAccount",
)
expect_failure(
    "path-duplicates-share-name",
    with_job(source__path="source-share/Invoices/2026"),
    "already is containerOrShare",
)
expect_success(
    "path-merely-resembles-share-name",
    with_job(source__path="source-share-archive/Invoices/2026"),
)

# --- one Entra application per site, shared within a site -------------------
same_site = copy.deepcopy(JOB)
same_site["name"] = "second"
same_site["destination"]["path"] = "Archive"
expect_success("one-app-two-jobs-same-site", {"default.json": copy.deepcopy(JOB), "second.json": same_site})

other_site = copy.deepcopy(JOB)
other_site["name"] = "second"
other_site["destination"]["siteUrl"] = "https://contoso.sharepoint.com/sites/Other"
expect_failure(
    "one-app-two-sites",
    {"default.json": copy.deepcopy(JOB), "second.json": other_site},
    "two different SharePoint sites",
)

# --- the environment mapping round-trips ------------------------------------
env_result = subprocess.run(
    [sys.executable, str(COPYCTL), "env", "default"],
    # Pin the jobs directory: an inherited COPY_JOBS_DIR would otherwise make
    # this compare the repo's sample against some other job file entirely.
    env={**os.environ, "COPY_JOBS_DIR": str(ROOT / "jobs")},
    text=True,
    capture_output=True,
    check=False,
    cwd=ROOT,
)
check("env-command", env_result.returncode == 0, env_result.stderr.strip())
env_map = dict(
    line.split("=", 1) for line in env_result.stdout.strip().splitlines() if "=" in line
)
check("env-dry-run-lowercase", env_map.get("COPY_DRY_RUN") == "true", env_map.get("COPY_DRY_RUN"))
check("env-job-name", env_map.get("COPY_JOB_NAME") == "default", env_map.get("COPY_JOB_NAME"))
check("env-include-paths-json", env_map.get("SOURCE_INCLUDE_PATHS") == "[]", env_map.get("SOURCE_INCLUDE_PATHS"))
check(
    "env-carries-source-scope",
    env_map.get("SOURCE_SUBSCRIPTION_ID") == JOB["source"]["subscriptionId"]
    and env_map.get("SOURCE_RESOURCE_GROUP") == JOB["source"]["resourceGroup"],
    "copyctl.py pull needs these to rebuild a job file",
)

# --- apply must not drop variables the deployment template owns -------------
sys.path.insert(0, str(ROOT))
import copyctl  # noqa: E402

TEMPLATE_OWNED = {
    "AZURE_MANAGED_IDENTITY_CLIENT_ID": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    "RCLONE_TRANSFERS": "4",
    "RCLONE_CHECKERS": "8",
    "COPY_SECRET_VERSION": "abc123",
}
deployed = {**TEMPLATE_OWNED, "SOURCE_PATH": "old", "COPY_DRY_RUN": "true"}
from_file = copyctl.config_to_env(copyctl.load_job(ROOT / "jobs" / "default.json"))
merged = copyctl.merged_env(deployed, from_file)

for key, value in TEMPLATE_OWNED.items():
    check(
        f"apply-preserves-{key}",
        merged.get(key) == value,
        f"apply would drop {key}, leaving the job unable to run",
    )
# A job whose identity variable was lost is repaired by apply, not left broken.
repaired = copyctl.merged_env(
    {k: v for k, v in deployed.items() if k != "AZURE_MANAGED_IDENTITY_CLIENT_ID"},
    from_file,
    identity_client_id="11111111-2222-3333-4444-555555555555",
)
check(
    "apply-repairs-missing-identity",
    repaired.get("AZURE_MANAGED_IDENTITY_CLIENT_ID") == "11111111-2222-3333-4444-555555555555",
    "apply should restore the identity variable from the job's own identity",
)
check(
    "apply-overwrites-job-fields",
    merged["SOURCE_PATH"] == from_file["SOURCE_PATH"],
    "the job file must win for fields it owns",
)

if failures:
    print("configuration tests FAILED", file=sys.stderr)
    for failure in failures:
        print(f"  {failure}", file=sys.stderr)
    sys.exit(1)
print("configuration tests passed")
