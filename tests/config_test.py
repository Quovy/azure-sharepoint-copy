#!/usr/bin/env python3
"""Configuration validation tests. No Azure access required."""

import argparse
import contextlib
import copy
import io
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


# --- deploy adds one job without redeploying the ones already there ---------
DEPLOYED = {
    "invoices": {
        "name": "invoices",
        "resourceName": "file-copy-invoices",
        "resourceGroup": "rg-copy",
        "location": "eastus",
        "image": "example.invalid/copy@sha256:" + "a" * 64,
        "environmentId": "/subscriptions/s/resourceGroups/rg-copy/providers/Microsoft.App/managedEnvironments/file-copy-environment",
        "resourceId": "/subscriptions/s/resourceGroups/rg-copy/providers/Microsoft.App/jobs/file-copy-invoices",
    }
}
SUBNET_ID = (
    "/subscriptions/s/resourceGroups/rg-copy/providers/Microsoft.Network"
    "/virtualNetworks/file-copy-vnet/subnets/container-apps"
)


def fake_az_json(*args):
    if args[:3] == ("containerapp", "env", "show"):
        return SUBNET_ID
    if args[:4] == ("network", "vnet", "subnet", "show"):
        return "10.240.0.0/27"
    if args[:3] == ("network", "vnet", "show"):
        return "10.240.0.0/16"
    if args[:3] == ("containerapp", "job", "show"):
        return None  # no private registry
    raise AssertionError(f"unexpected az call: {args}")


copyctl.az_json = fake_az_json
settings = copyctl.deployment_settings(None, DEPLOYED)
check("deploy-infers-base-name", settings["baseName"] == "file-copy", settings["baseName"])
check(
    "deploy-infers-network",
    (settings["vnet"], settings["subnet"]) == ("10.240.0.0/16", "10.240.0.0/27"),
    "a mistyped prefix would try to renumber the deployed network",
)
check(
    "deploy-infers-running-image",
    settings["image"] == DEPLOYED["invoices"]["image"],
    "a new job must not land on a different image than the fleet runs",
)
check("deploy-infers-location", settings["location"] == "eastus", settings["location"])

two_deployments = {
    **DEPLOYED,
    "reports": {**DEPLOYED["invoices"], "name": "reports", "resourceName": "file-copy-reports", "resourceGroup": "rg-other"},
}
try:
    copyctl.deployment_settings(None, two_deployments)
    refused = False
except copyctl.CopyctlError:
    refused = True
check(
    "deploy-refuses-ambiguous-deployment",
    refused,
    "guessing which deployment to extend could add a job to the wrong one",
)

mixed_images = {
    **DEPLOYED,
    "reports": {
        **DEPLOYED["invoices"],
        "name": "reports",
        "resourceName": "file-copy-reports",
        "image": "example.invalid/copy@sha256:" + "b" * 64,
    },
}
check(
    "deploy-refuses-to-guess-image",
    copyctl.deployment_settings(None, mixed_images)["image"] == "",
    "with no agreed image, deploy must ask rather than pick one for the new job",
)


def refuse_az_json(*args):
    if args[:3] == ("containerapp", "job", "show"):
        raise AssertionError("a supplied --registry-id must not be re-looked-up")
    return fake_az_json(*args)


copyctl.az_json = refuse_az_json
supplied = "/subscriptions/other/resourceGroups/rg/providers/Microsoft.ContainerRegistry/registries/acr"
check(
    "deploy-honours-supplied-registry",
    copyctl.deployment_settings(None, DEPLOYED, supplied)["registryId"] == supplied,
    "the lookup only searches one subscription, so --registry-id must bypass it",
)
copyctl.az_json = fake_az_json

document = copyctl.params_document(
    [copy.deepcopy(JOB)], "file-copy", "10.240.0.0/16", "10.240.0.0/27", "example.invalid/copy@sha256:x", "", "eastus"
)
check(
    "deploy-params-hold-one-job",
    [entry["name"] for entry in document["parameters"]["jobs"]["value"]] == [JOB["name"]],
    "an incremental deployment must mention only the job being added",
)
check(
    "deploy-params-carry-location",
    document["parameters"]["location"]["value"] == "eastus",
    "omitting it would fall back to the resource group's location",
)
check(
    "deploy-params-omit-empty-registry",
    "containerRegistryResourceId" not in document["parameters"],
    "an empty registry ID must not be sent as a parameter",
)

# --- check-source separates the layers behind a storage 403 -----------------
CHECK_RECORD = {
    "name": "default",
    "resourceName": "file-copy-default",
    "resourceGroup": "rg-copy",
    "environmentId": "/subscriptions/s/resourceGroups/rg-copy/providers/Microsoft.App/managedEnvironments/file-copy-environment",
    "env": {
        "SOURCE_TYPE": "adls_gen2",
        "SOURCE_SUBSCRIPTION_ID": "s",
        "SOURCE_RESOURCE_GROUP": "rg-source-files",
        "SOURCE_STORAGE_ACCOUNT": "examplestorage",
    },
}
CHECK_ACCOUNT_ID = (
    "/subscriptions/s/resourceGroups/rg-source-files"
    "/providers/Microsoft.Storage/storageAccounts/examplestorage"
)


def run_check_source(fake_run):
    """Run cmd_check_source against faked az calls, returning (output, exit_code)."""
    saved = (copyctl.run, copyctl.az_json, copyctl.deployed_job)
    copyctl.run = fake_run
    copyctl.az_json = fake_az_json  # subnet_id_for reads the environment's subnet
    copyctl.deployed_job = lambda *args, **kwargs: CHECK_RECORD
    output = io.StringIO()
    code = 0
    try:
        with contextlib.redirect_stdout(output):
            copyctl.cmd_check_source(
                argparse.Namespace(subscription=None, job="default", resource_group=None)
            )
    except SystemExit as exit_error:
        code = exit_error.code
    finally:
        copyctl.run, copyctl.az_json, copyctl.deployed_job = saved
    return output.getvalue(), code


def fake_run_firewall_deny(*args, allow_failure=False, stdin_text=None):
    check(
        "check-source-tolerates-az-failures",
        allow_failure,
        f"an az call that can fail on permissions must use allow_failure: {args[:4]}",
    )
    if args[1:3] == ("identity", "show"):
        return "11111111-2222-3333-4444-555555555555", True
    if args[1:4] == ("role", "assignment", "list"):
        return json.dumps([{
            "role": "Storage Blob Data Reader",
            "scope": CHECK_ACCOUNT_ID + "/blobServices/default/containers/data",
        }]), True
    if args[1:4] == ("storage", "account", "show"):
        # The firewall denies by default and holds no rule for the copy subnet.
        return json.dumps({"public": "Enabled", "action": "Deny", "rules": []}), True
    if args[1:5] == ("network", "vnet", "subnet", "show"):
        return json.dumps(["Microsoft.Storage"]), True
    if args[1:4] == ("network", "private-endpoint-connection", "list"):
        return "[]", True
    raise AssertionError(f"unexpected az call: {args}")


deny_output, deny_code = run_check_source(fake_run_firewall_deny)
check("check-source-fails-on-missing-rule", deny_code == 1, f"exit code was {deny_code}")
check(
    "check-source-names-the-failing-layer",
    "FAIL" in deny_output and "firewall rule" in deny_output,
    deny_output,
)
check(
    "check-source-names-the-fix",
    "grant-source" in deny_output,
    "a FAIL line must say which command repairs it",
)
check(
    "check-source-reports-private-endpoints",
    "private endpoints" in deny_output and "none exist" in deny_output,
    "the private endpoint layer must be reported even when this deployment uses none",
)


def fake_run_no_permission(*args, allow_failure=False, stdin_text=None):
    if args[1:3] == ("identity", "show"):
        return "11111111-2222-3333-4444-555555555555", True
    if args[1:4] == ("role", "assignment", "list"):
        return "", False
    if args[1:4] == ("storage", "account", "show"):
        return "", False
    if args[1:4] == ("network", "private-endpoint-connection", "list"):
        return "", False
    raise AssertionError(f"unexpected az call: {args}")


unknown_output, unknown_code = run_check_source(fake_run_no_permission)
check("check-source-unreadable-is-not-failure", unknown_code == 0, f"exit code was {unknown_code}")
check(
    "check-source-unreadable-is-unknown",
    unknown_output.count("unknown ") >= 2,
    "layers the operator cannot read must be reported as unknown, never as ok",
)
check(
    "check-source-unreadable-is-not-ok",
    not any(
        line.strip().startswith("ok") and ("role assignment" in line or "network gate" in line)
        for line in unknown_output.splitlines()
    ),
    "an unreadable layer must not be counted as passed",
)

if failures:
    print("configuration tests FAILED", file=sys.stderr)
    for failure in failures:
        print(f"  {failure}", file=sys.stderr)
    sys.exit(1)
print("configuration tests passed")
