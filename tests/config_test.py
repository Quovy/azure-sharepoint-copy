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

# --- set: change one field in a job file ------------------------------------
def run_set(field, value, jobs=None, job="default"):
    """Run 'set' against a throwaway jobs directory.

    Returns the completed process and the job file as it was left behind, so a
    rejected edit can be checked for having changed nothing.
    """
    with tempfile.TemporaryDirectory() as directory:
        jobs_path = Path(directory) / "jobs"
        jobs_path.mkdir()
        for filename, config in (jobs or {"default.json": copy.deepcopy(JOB)}).items():
            (jobs_path / filename).write_text(json.dumps(config, indent=2) + "\n")
        result = subprocess.run(
            [sys.executable, str(COPYCTL), "set", job, field, value],
            env={**os.environ, "COPY_JOBS_DIR": str(jobs_path)},
            text=True,
            capture_output=True,
            check=False,
        )
        written = jobs_path / f"{job}.json"
        return result, (json.loads(written.read_text()) if written.exists() else None)


def expect_set(label, field, value, expected, jobs=None):
    result, document = run_set(field, value, jobs)
    check(label, result.returncode == 0, result.stderr.strip())
    if document is None:
        return
    section, _, name = field.partition(".")
    actual = document[section][name]
    check(label, actual == expected and type(actual) is type(expected), f"file holds {actual!r}")


def expect_set_failure(label, field, value, message, jobs=None):
    result, document = run_set(field, value, jobs)
    check(label, result.returncode != 0, "expected the edit to be refused")
    check(label, message in result.stderr, f"expected '{message}' in: {result.stderr.strip()}")
    check(label, document == (jobs or {"default.json": JOB})["default.json"], "a refused edit rewrote the file")


expect_set("set-text", "source.path", "Reports/2026", "Reports/2026")
expect_set("set-schedule", "copy.scheduleUtc", "30 4 * * 1-5", "30 4 * * 1-5")
# A quoted boolean is the mistake validation already rejects in hand-edited
# files, so set must write a real one.
expect_set("set-boolean", "copy.dryRun", "false", False)
expect_set("set-number", "copy.timeoutMinutes", "45", 45)
expect_set("set-list", "source.includePaths", '["Invoices/2026", "Reports"]', ["Invoices/2026", "Reports"])

expect_set_failure("set-boolean-word", "copy.dryRun", "yes", "must be true or false")
expect_set_failure("set-number-text", "copy.timeoutMinutes", "soon", "must be a whole number")
expect_set_failure("set-list-not-json", "source.includePaths", "Invoices,Reports", "must be a JSON array")
expect_set_failure("set-unknown-field", "copy.retries", "3", "is not a field of default.json")
expect_set_failure("set-unknown-section", "cleanup.mode", "purge", "is not a settable field")
# The filename is what ties a job file to its deployed job.
expect_set_failure("set-name-refused", "name", "renamed", "is not a settable field")

# Every rule 'validate' enforces must also gate an edit, including the ones
# that span fields, and a refused edit must leave the file untouched.
expect_set_failure("set-invalid-cron", "copy.scheduleUtc", "0 99 * * *", "hour value out of range")
expect_set_failure("set-traversal", "source.path", "../outside", "must not contain '..'")
expect_set_failure("set-overlap", "copy.scheduleUtc", "0 * * * *", "executions would overlap")
filtered = copy.deepcopy(JOB)
filtered["source"]["modifiedOnOrAfter"] = "2026-07-01"
expect_set_failure(
    "set-conflicting-filters",
    "source.includePaths",
    '["Invoices"]',
    "cannot be used together",
    {"default.json": filtered},
)

missing_result, _ = run_set("source.path", "x", job="ghost")
check("set-missing-job", missing_result.returncode != 0, "expected a missing job file to be refused")
check("set-missing-job-message", "does not exist" in missing_result.stderr, missing_result.stderr.strip())


# --- get: read one field, or list them all ----------------------------------
def run_get(*arguments, jobs=None, job="default"):
    with tempfile.TemporaryDirectory() as directory:
        jobs_path = Path(directory) / "jobs"
        jobs_path.mkdir()
        for filename, config in (jobs or {"default.json": copy.deepcopy(JOB)}).items():
            (jobs_path / filename).write_text(json.dumps(config, indent=2) + "\n")
        return subprocess.run(
            [sys.executable, str(COPYCTL), "get", job, *arguments],
            env={**os.environ, "COPY_JOBS_DIR": str(jobs_path)},
            text=True,
            capture_output=True,
            check=False,
        )


def expect_get(label, field, expected, jobs=None):
    result = run_get(field, jobs=jobs)
    check(label, result.returncode == 0, result.stderr.strip())
    check(label, result.stdout == expected + "\n", f"printed {result.stdout!r}")


# A plain string prints bare: a shell reading one value must not have to strip
# JSON quoting off it.
expect_get("get-text", "destination.library", JOB["destination"]["library"])
expect_get("get-schedule", "copy.scheduleUtc", JOB["copy"]["scheduleUtc"])
expect_get("get-boolean", "copy.dryRun", "true")
expect_get("get-number", "copy.timeoutMinutes", str(JOB["copy"]["timeoutMinutes"]))
expect_get("get-empty-text", "source.path", "")
expect_get("get-list", "source.includePaths", "[]")
paths = copy.deepcopy(JOB)
paths["source"]["includePaths"] = ["Invoices/2026", "Reports"]
expect_get("get-list-values", "source.includePaths", '["Invoices/2026", "Reports"]', {"default.json": paths})

listed = run_get()
check("get-lists-fields", listed.returncode == 0, listed.stderr.strip())
listed_fields = [line.split()[0] for line in listed.stdout.splitlines()]
check(
    "get-lists-every-settable-field",
    listed_fields == [f"{section}.{name}" for section in ("source", "destination", "copy") for name in JOB[section]],
    f"listed {listed_fields}",
)
check("get-listing-omits-name", "name" not in listed_fields, "the job name is fixed by the filename")

# What 'get' prints, 'set' must accept back unchanged. This is the property that
# lets an operator copy a value out, edit it, and put it back.
for round_trip_field in ("destination.library", "copy.scheduleUtc", "copy.dryRun", "copy.timeoutMinutes"):
    section, _, name = round_trip_field.partition(".")
    printed = run_get(round_trip_field).stdout.rstrip("\n")
    _, after = run_set(round_trip_field, printed)
    check(
        f"get-set-round-trip-{round_trip_field}",
        after is not None and after[section][name] == JOB[section][name],
        f"setting the printed value changed {round_trip_field}",
    )

unknown_get = run_get("copy.retries")
check("get-unknown-field", unknown_get.returncode != 0, "expected an unknown field to be refused")
check("get-unknown-field-message", "is not a field of default.json" in unknown_get.stderr, unknown_get.stderr.strip())

missing_get = run_get("source.path", job="ghost")
check("get-missing-job", missing_get.returncode != 0, "expected a missing job file to be refused")
check("get-missing-job-message", "does not exist" in missing_get.stderr, missing_get.stderr.strip())

# Reading must work on a file too broken to validate; that is when it is needed.
broken = copy.deepcopy(JOB)
broken["copy"]["scheduleUtc"] = "0 99 * * *"
broken_get = run_get("copy.scheduleUtc", jobs={"default.json": broken})
check("get-reads-invalid-file", broken_get.returncode == 0, broken_get.stderr.strip())
check("get-reads-invalid-value", broken_get.stdout.strip() == "0 99 * * *", broken_get.stdout.strip())

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
