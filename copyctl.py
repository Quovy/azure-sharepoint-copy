#!/usr/bin/env python3
"""Operate the Azure to SharePoint copy service. Standard library only.

Every command discovers what is deployed by querying Azure for resources tagged
workload=azure-sharepoint-copy. Nothing is cached on this machine, so any
administrator with the right Azure role can run these commands from any Cloud
Shell or workstation.
"""

import argparse
import datetime
import getpass
import ipaddress
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
JOBS_DIR = Path(os.environ.get("COPY_JOBS_DIR", ROOT / "jobs"))

WORKLOAD_TAG = "azure-sharepoint-copy"
SECRET_ENV_NAME = "RCLONE_CONFIG_DESTINATION_CLIENT_SECRET"
SECRET_REF_NAME = "sharepoint-client-secret"
PARKED_CRON = "0 0 31 2 *"

UUID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
NAME_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
STORAGE_ACCOUNT_PATTERN = re.compile(r"^[a-z0-9]{3,24}$")

# Fields the deployment template turns into container environment variables.
# This mapping is the single definition of the runtime contract; app/transfer.sh
# consumes exactly these names.
ENV_FIELDS = (
    ("SOURCE_TYPE", ("source", "type")),
    ("SOURCE_SUBSCRIPTION_ID", ("source", "subscriptionId")),
    ("SOURCE_RESOURCE_GROUP", ("source", "resourceGroup")),
    ("SOURCE_STORAGE_ACCOUNT", ("source", "storageAccount")),
    ("SOURCE_CONTAINER_OR_SHARE", ("source", "containerOrShare")),
    ("SOURCE_PATH", ("source", "path")),
    ("SOURCE_MODIFIED_ON_OR_AFTER", ("source", "modifiedOnOrAfter")),
    ("DEST_TENANT_ID", ("destination", "tenantId")),
    ("DEST_CLIENT_ID", ("destination", "clientId")),
    ("DEST_SITE_URL", ("destination", "siteUrl")),
    ("DEST_LIBRARY", ("destination", "library")),
    ("DEST_PATH", ("destination", "path")),
    ("COPY_EXISTING_FILES", ("copy", "existingFiles")),
    ("COPY_SCHEDULE_UTC", ("copy", "scheduleUtc")),
)


class CopyctlError(Exception):
    pass


def fail(message):
    raise CopyctlError(message)


# --------------------------------------------------------------------------
# Configuration validation
# --------------------------------------------------------------------------


def exact_keys(value, allowed, label):
    if not isinstance(value, dict):
        fail(f"{label} must be a JSON object.")
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        fail(f"{label} contains unknown field(s): {', '.join(unknown)}")
    missing = sorted(set(allowed) - set(value))
    if missing:
        fail(f"{label} is missing required field(s): {', '.join(missing)}")


def text(value, label, allow_empty=False):
    if not isinstance(value, str):
        fail(f"{label} must be text.")
    if not allow_empty and not value:
        fail(f"{label} cannot be empty.")
    if any(character in value for character in ("\n", "\r", "\t", "\0")):
        fail(f"{label} cannot contain control characters.")
    return value


def relative_path(value, label):
    value = text(value, label, allow_empty=True)
    if value.startswith("/"):
        fail(f"{label} must be relative and must not begin with '/'.")
    if ":" in value:
        fail(f"{label} must not contain ':'.")
    if ".." in value.split("/"):
        fail(f"{label} must not contain '..'.")
    return value


def uuid_text(value, label):
    value = text(value, label)
    if not UUID_PATTERN.fullmatch(value):
        fail(f"{label} must be a UUID.")
    return value


def whole_number(value, label, minimum, maximum):
    if type(value) is not int:
        fail(f"{label} must be a whole number.")
    if not minimum <= value <= maximum:
        fail(f"{label} must be between {minimum} and {maximum}.")
    return value


def boolean(value, label):
    if type(value) is not bool:
        fail(f"{label} must be true or false, without quotes.")
    return value


def validate_date(value, label):
    if not value:
        return
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            datetime.date.fromisoformat(value)
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
            datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            raise ValueError
    except ValueError:
        fail(f"{label} must be YYYY-MM-DD or a UTC timestamp ending in Z.")


CRON_FIELD_RANGES = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day of month", 1, 31),
    ("month", 1, 12),
    ("day of week", 0, 7),
)


def validate_cron(value, label):
    """Check a five-field cron expression field by field.

    Container Apps rejects a malformed expression only at deployment time, which
    is far too late in a guided install.
    """
    fields = value.split()
    if len(fields) != 5:
        fail(f"{label} must contain exactly five space-separated cron fields.")
    for field, (field_name, low, high) in zip(fields, CRON_FIELD_RANGES):
        for part in field.split(","):
            if "/" in part:
                part, _, step_text = part.partition("/")
                if not step_text.isdigit() or int(step_text) < 1:
                    fail(f"{label} has an invalid step in the {field_name} field: '{step_text}'.")
            if part == "*":
                continue
            bounds = part.split("-")
            if len(bounds) > 2:
                fail(f"{label} has an invalid range in the {field_name} field: '{part}'.")
            for bound in bounds:
                if not bound.isdigit():
                    fail(f"{label} has a non-numeric {field_name} value: '{bound}'.")
                if not low <= int(bound) <= high:
                    fail(f"{label} has a {field_name} value out of range ({low}-{high}): '{bound}'.")
            if len(bounds) == 2 and int(bounds[0]) > int(bounds[1]):
                fail(f"{label} has a reversed {field_name} range: '{part}'.")
    return value


def cron_interval_minutes(value):
    """Smallest gap between runs, or None when it cannot be determined cheaply.

    Only the common shapes are modelled. Anything else returns None and skips
    the overlap warning rather than guessing.
    """
    minute, hour, day_of_month, month, day_of_week = value.split()
    if any(field != "*" for field in (day_of_month, month, day_of_week)):
        return None
    if minute == "*":
        return 1
    if minute.startswith("*/") and minute[2:].isdigit():
        return int(minute[2:])
    if not minute.isdigit():
        return None
    if hour == "*":
        return 60
    if hour.startswith("*/") and hour[2:].isdigit():
        return int(hour[2:]) * 60
    if hour.isdigit():
        return 24 * 60
    return None


def validate_network(vnet_text, subnet_text):
    try:
        vnet = ipaddress.ip_network(vnet_text, strict=True)
        subnet = ipaddress.ip_network(subnet_text, strict=True)
    except ValueError:
        fail("Network ranges must use CIDR notation, for example 10.240.0.0/16 and 10.240.0.0/27.")
    private_ranges = [
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
    ]
    if vnet.version != 4 or subnet.version != 4:
        fail("Network ranges must use IPv4.")
    if not any(vnet.subnet_of(item) for item in private_ranges):
        fail("vnetAddressPrefix must be an RFC 1918 private range.")
    if not any(subnet.subnet_of(item) for item in private_ranges):
        fail("containerAppsSubnetPrefix must be an RFC 1918 private range.")
    if subnet.prefixlen > 27:
        fail("containerAppsSubnetPrefix must be /27 or larger.")
    if not subnet.subnet_of(vnet):
        fail("The Container Apps subnet must fit inside the virtual network.")


def read_json(path, label):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        fail(f"{label} was not found.")
    except json.JSONDecodeError as error:
        fail(f"{label} is not valid JSON: line {error.lineno}, column {error.colno}: {error.msg}")


def load_job(path):
    raw = read_json(path, str(path))
    label = f"jobs/{path.name}"
    exact_keys(raw, ["name", "source", "destination", "copy"], label)

    name = text(raw["name"], f"{label}.name")
    if not 2 <= len(name) <= 20 or not NAME_PATTERN.fullmatch(name):
        fail(f"{label}.name must use 2-20 lowercase letters, numbers, or internal hyphens.")
    if path.stem != name:
        fail(f"{label}.name must match its filename: expected '{path.stem}'.")

    source, destination, copy = raw["source"], raw["destination"], raw["copy"]
    exact_keys(
        source,
        [
            "type",
            "subscriptionId",
            "resourceGroup",
            "storageAccount",
            "containerOrShare",
            "path",
            "includePaths",
            "modifiedOnOrAfter",
        ],
        f"{label}.source",
    )
    exact_keys(destination, ["tenantId", "clientId", "siteUrl", "library", "path"], f"{label}.destination")
    exact_keys(copy, ["existingFiles", "dryRun", "scheduleUtc", "timeoutMinutes"], f"{label}.copy")

    source_type = text(source["type"], f"{label}.source.type")
    if source_type not in ("azure_files", "adls_gen2"):
        fail(f"{label}.source.type must be azure_files or adls_gen2.")

    storage_account = text(source["storageAccount"], f"{label}.source.storageAccount")
    if not STORAGE_ACCOUNT_PATTERN.fullmatch(storage_account):
        fail(f"{label}.source.storageAccount must use 3-24 lowercase letters and numbers.")

    container = text(source["containerOrShare"], f"{label}.source.containerOrShare")
    if not 3 <= len(container) <= 63 or not NAME_PATTERN.fullmatch(container):
        fail(f"{label}.source.containerOrShare must use 3-63 lowercase letters, numbers, or internal hyphens.")

    include_paths = source["includePaths"]
    if not isinstance(include_paths, list):
        fail(f"{label}.source.includePaths must be a JSON array.")
    include_paths = [
        relative_path(item, f"{label}.source.includePaths[{index}]")
        for index, item in enumerate(include_paths)
    ]
    if any(not item for item in include_paths):
        fail(f"{label}.source.includePaths cannot contain an empty path.")
    if len(include_paths) != len(set(include_paths)):
        fail(f"{label}.source.includePaths contains a duplicate path.")

    modified = text(source["modifiedOnOrAfter"], f"{label}.source.modifiedOnOrAfter", allow_empty=True)
    validate_date(modified, f"{label}.source.modifiedOnOrAfter")
    if include_paths and modified:
        # --files-from-raw overrides every other rclone filter.
        fail(f"{label}.source.includePaths and modifiedOnOrAfter cannot be used together.")

    site_url = text(destination["siteUrl"], f"{label}.destination.siteUrl").rstrip("/")
    parsed_site = urllib.parse.urlsplit(site_url)
    if (
        parsed_site.scheme != "https"
        or not parsed_site.hostname
        or parsed_site.path in ("", "/")
        or parsed_site.query
        or parsed_site.fragment
    ):
        fail(f"{label}.destination.siteUrl must be an exact HTTPS site URL without a query or fragment.")

    existing_files = text(copy["existingFiles"], f"{label}.copy.existingFiles")
    if existing_files not in ("skip", "replace_if_changed"):
        fail(f"{label}.copy.existingFiles must be skip or replace_if_changed.")

    schedule = validate_cron(text(copy["scheduleUtc"], f"{label}.copy.scheduleUtc"), f"{label}.copy.scheduleUtc")
    timeout_minutes = whole_number(copy["timeoutMinutes"], f"{label}.copy.timeoutMinutes", 5, 1440)

    interval = cron_interval_minutes(schedule)
    if interval is not None and interval < timeout_minutes:
        fail(
            f"{label} runs every {interval} minute(s) but allows {timeout_minutes} minutes per execution, "
            "so executions would overlap. Lengthen the schedule or shorten timeoutMinutes."
        )

    return {
        "name": name,
        "source": {
            "type": source_type,
            "subscriptionId": uuid_text(source["subscriptionId"], f"{label}.source.subscriptionId"),
            "resourceGroup": text(source["resourceGroup"], f"{label}.source.resourceGroup"),
            "storageAccount": storage_account,
            "containerOrShare": container,
            "path": relative_path(source["path"], f"{label}.source.path"),
            "includePaths": include_paths,
            "modifiedOnOrAfter": modified,
        },
        "destination": {
            "tenantId": uuid_text(destination["tenantId"], f"{label}.destination.tenantId"),
            "clientId": uuid_text(destination["clientId"], f"{label}.destination.clientId"),
            "siteUrl": site_url,
            "library": text(destination["library"], f"{label}.destination.library"),
            "path": relative_path(destination["path"], f"{label}.destination.path"),
        },
        "copy": {
            "existingFiles": existing_files,
            "dryRun": boolean(copy["dryRun"], f"{label}.copy.dryRun"),
            "scheduleUtc": schedule,
            "timeoutMinutes": timeout_minutes,
        },
    }


def load_jobs():
    paths = sorted(JOBS_DIR.glob("*.json"))
    if not paths:
        fail(f"{JOBS_DIR} must contain at least one .json job configuration.")
    jobs = [load_job(path) for path in paths]
    names = [job["name"] for job in jobs]
    if len(names) != len(set(names)):
        fail("Job names must be unique.")
    seen = {}
    for job in jobs:
        client_id = job["destination"]["clientId"].lower()
        site = job["destination"]["siteUrl"].lower()
        # A Sites.Selected grant is per site, so one application may serve
        # several jobs on the same site but must never span two sites.
        if client_id in seen and seen[client_id] != site:
            fail(
                f"Entra application {client_id} is used for two different SharePoint sites. "
                "Register one application per site so each grant stays scoped."
            )
        seen[client_id] = site
    return jobs


def load_job_named(name):
    path = JOBS_DIR / f"{name}.json"
    if not path.exists():
        fail(f"{path} does not exist. Run 'copyctl.py pull' to write local files from Azure.")
    for job in load_jobs():
        if job["name"] == name:
            return job
    fail(f"No job named '{name}' was found in {JOBS_DIR}.")


# --------------------------------------------------------------------------
# Azure discovery
# --------------------------------------------------------------------------


def run(*args, allow_failure=False, stdin_text=None):
    result = subprocess.run(args, text=True, capture_output=True, check=False, input=stdin_text)
    if allow_failure:
        return result.stdout.strip(), result.returncode == 0
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        fail(f"Command failed: {' '.join(args[:3])}\n{detail}")
    return result.stdout.strip()


def az_json(*args):
    output = run("az", *args, "--only-show-errors", "--output", "json")
    return json.loads(output) if output else None


def subscription_args(subscription):
    return ("--subscription", subscription) if subscription else ()


def discover(subscription):
    """Find every deployed copy job by tag. This is the only state lookup."""
    records = az_json(
        "containerapp",
        "job",
        "list",
        *subscription_args(subscription),
        "--query",
        f"[?tags.workload=='{WORKLOAD_TAG}' && tags.copyJob != null]",
    )
    if not records:
        fail(
            "No deployed copy jobs were found in this subscription. "
            "Check that you selected the right subscription with 'az account set'."
        )
    jobs = {}
    for record in records:
        env = {
            item["name"]: item.get("value", "")
            for item in record["properties"]["template"]["containers"][0].get("env", [])
            if "value" in item
        }
        secrets = record["properties"]["configuration"].get("secrets") or []
        vault_name = ""
        secret_name = ""
        for secret in secrets:
            url = secret.get("keyVaultUrl") or ""
            if "/secrets/" in url:
                vault_name = urllib.parse.urlsplit(url).hostname.split(".")[0]
                secret_name = url.rsplit("/secrets/", 1)[1].split("/")[0]
        jobs[record["tags"]["copyJob"]] = {
            "name": record["tags"]["copyJob"],
            "resourceName": record["name"],
            "resourceGroup": record["resourceGroup"],
            "resourceId": record["id"],
            "environmentId": record["properties"]["environmentId"],
            "image": record["properties"]["template"]["containers"][0]["image"],
            # A manually triggered job has no scheduleTriggerConfig at all, and
            # ARM may return the key with a null value rather than omitting it.
            "cron": (record["properties"]["configuration"].get("scheduleTriggerConfig") or {}).get(
                "cronExpression", ""
            ),
            "replicaTimeout": record["properties"]["configuration"].get("replicaTimeout", 0),
            "vaultName": vault_name,
            "secretName": secret_name,
            "env": env,
        }
    return jobs


def deployed_job(subscription, name):
    jobs = discover(subscription)
    if name not in jobs:
        available = ", ".join(sorted(jobs)) or "(none)"
        fail(f"Job '{name}' is not deployed. Deployed jobs: {available}")
    return jobs[name]


def subnet_id_for(record):
    # --ids already carries the subscription, and az rejects it alongside the
    # other resource-selecting arguments.
    environment = az_json(
        "containerapp",
        "env",
        "show",
        "--ids",
        record["environmentId"],
        "--query",
        "properties.vnetConfiguration.infrastructureSubnetId",
    )
    if not environment:
        fail("The Container Apps environment has no infrastructure subnet.")
    return environment


# --------------------------------------------------------------------------
# Configuration <-> environment mapping
# --------------------------------------------------------------------------


def config_to_env(job):
    values = {"COPY_JOB_NAME": job["name"]}
    for env_name, (section, field) in ENV_FIELDS:
        values[env_name] = str(job[section][field])
    values["SOURCE_INCLUDE_PATHS"] = json.dumps(job["source"]["includePaths"], separators=(",", ":"))
    values["COPY_DRY_RUN"] = "true" if job["copy"]["dryRun"] else "false"
    return values


def env_to_config(record):
    env = record["env"]
    missing = [name for name, _ in ENV_FIELDS if name not in env]
    if missing:
        fail(
            f"Deployed job '{record['name']}' is missing environment variable(s): {', '.join(missing)}. "
            "It may predate this version of the template."
        )
    try:
        include_paths = json.loads(env.get("SOURCE_INCLUDE_PATHS") or "[]")
    except json.JSONDecodeError:
        fail(f"Deployed job '{record['name']}' has an invalid SOURCE_INCLUDE_PATHS value.")
    return {
        "name": record["name"],
        "source": {
            "type": env["SOURCE_TYPE"],
            "subscriptionId": env["SOURCE_SUBSCRIPTION_ID"],
            "resourceGroup": env["SOURCE_RESOURCE_GROUP"],
            "storageAccount": env["SOURCE_STORAGE_ACCOUNT"],
            "containerOrShare": env["SOURCE_CONTAINER_OR_SHARE"],
            "path": env.get("SOURCE_PATH", ""),
            "includePaths": include_paths,
            "modifiedOnOrAfter": env.get("SOURCE_MODIFIED_ON_OR_AFTER", ""),
        },
        "destination": {
            "tenantId": env["DEST_TENANT_ID"],
            "clientId": env["DEST_CLIENT_ID"],
            "siteUrl": env["DEST_SITE_URL"],
            "library": env["DEST_LIBRARY"],
            "path": env.get("DEST_PATH", ""),
        },
        "copy": {
            "existingFiles": env["COPY_EXISTING_FILES"],
            "dryRun": env.get("COPY_DRY_RUN", "true") == "true",
            "scheduleUtc": env["COPY_SCHEDULE_UTC"],
            "timeoutMinutes": max(5, int(record["replicaTimeout"] or 300) // 60),
        },
    }


def update_job(subscription, record, env_values=None, cron=None, timeout_minutes=None):
    args = [
        "containerapp",
        "job",
        "update",
        *subscription_args(subscription),
        "--resource-group",
        record["resourceGroup"],
        "--name",
        record["resourceName"],
    ]
    if env_values is not None:
        env_values = dict(env_values)
        # Carry forward the recorded credential version; a full replacement
        # would otherwise drop it and lose the audit trail.
        existing_version = record["env"].get("COPY_SECRET_VERSION")
        if existing_version and "COPY_SECRET_VERSION" not in env_values:
            env_values["COPY_SECRET_VERSION"] = existing_version
        pairs = [f"{key}={value}" for key, value in sorted(env_values.items()) if key != SECRET_ENV_NAME]
        # The credential is a secret reference rather than a literal value, so a
        # full replacement has to restate it or the job would lose it.
        pairs.append(f"{SECRET_ENV_NAME}=secretref:{SECRET_REF_NAME}")
        args.extend(["--replace-env-vars", *pairs])
    if cron is not None:
        args.extend(["--cron-expression", cron])
    if timeout_minutes is not None:
        args.extend(["--replica-timeout", str(timeout_minutes * 60)])
    run("az", *args, "--only-show-errors", "--output", "none")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_validate(args):
    jobs = load_jobs()
    validate_network(args.vnet, args.subnet)
    for job in jobs:
        mode = "dry run" if job["copy"]["dryRun"] else "LIVE"
        print(f"  {job['name']:<20} {mode:<8} {job['copy']['scheduleUtc']} UTC")
    print(f"{len(jobs)} job configuration(s) are valid.")


def cmd_env(args):
    """Print the container environment a local job file produces.

    Useful for comparing a local file against a deployed job, and used by the
    runtime tests so app/transfer.sh is always exercised against the exact
    variables this tool generates.
    """
    for key, value in sorted(config_to_env(load_job_named(args.job)).items()):
        print(f"{key}={value}")


def published_image():
    """The image this release was built against.

    Defaulting to it means an operator never has to hunt for the digest, and
    cannot accidentally deploy a different one than the release was tested with.
    """
    path = ROOT / "infra" / "main.parameters.json"
    document = read_json(path, str(path))
    value = document.get("parameters", {}).get("containerImage", {}).get("value", "")
    if "@sha256:" not in value:
        fail(
            f"{path} does not contain a digest-pinned image. "
            "Pass --image explicitly, or use a released version of this package."
        )
    return value


def cmd_params(args):
    jobs = load_jobs()
    image = args.image or published_image()
    document = {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
        "contentVersion": "1.0.0.0",
        "parameters": {
            "baseName": {"value": args.base_name},
            "vnetAddressPrefix": {"value": args.vnet},
            "containerAppsSubnetPrefix": {"value": args.subnet},
            "containerImage": {"value": image},
            "jobs": {"value": jobs},
        },
    }
    if args.registry_id:
        document["parameters"]["containerRegistryResourceId"] = {"value": args.registry_id}
    validate_network(args.vnet, args.subnet)
    rendered = json.dumps(document, indent=2) + "\n"
    if args.out:
        Path(args.out).write_text(rendered, encoding="utf-8")
        print(f"Wrote {args.out} with {len(jobs)} job(s).")
    else:
        sys.stdout.write(rendered)


def cmd_list(args):
    jobs = discover(args.subscription)
    print(f"{'JOB':<20} {'MODE':<8} {'SCHEDULE':<16} {'AZURE RESOURCE'}")
    for name in sorted(jobs):
        record = jobs[name]
        mode = "LIVE" if record["env"].get("COPY_DRY_RUN") == "false" else "dry run"
        cron = record["cron"]
        schedule = "parked" if cron == PARKED_CRON else cron
        print(f"{name:<20} {mode:<8} {schedule:<16} {record['resourceName']}")


def cmd_status(args):
    record = deployed_job(args.subscription, args.job)
    env = record["env"]
    mode = "LIVE COPY" if env.get("COPY_DRY_RUN") == "false" else "dry run"
    parked = record["cron"] == PARKED_CRON
    print(f"Job              {record['name']} ({record['resourceName']})")
    print(f"Mode             {mode}")
    print(
        "Schedule         "
        + (
            f"parked, will not run (intended {env.get('COPY_SCHEDULE_UTC', '?')} UTC) "
            "- run 'copyctl.py enable' to activate"
            if parked
            else f"{record['cron']} UTC"
        )
    )
    print(f"Source           {env.get('SOURCE_TYPE')}: {env.get('SOURCE_STORAGE_ACCOUNT')}/"
          f"{env.get('SOURCE_CONTAINER_OR_SHARE')}/{env.get('SOURCE_PATH', '')}")
    print(f"Destination      {env.get('DEST_SITE_URL')} / {env.get('DEST_LIBRARY')} / {env.get('DEST_PATH', '')}")
    print(f"Existing files   {env.get('COPY_EXISTING_FILES')}")
    print(f"Timeout          {int(record['replicaTimeout'] or 0) // 60} minutes")
    print(f"Image            {record['image']}")

    executions = az_json(
        "containerapp",
        "job",
        "execution",
        "list",
        *subscription_args(args.subscription),
        "--resource-group",
        record["resourceGroup"],
        "--name",
        record["resourceName"],
        "--query",
        "sort_by([].{name:name,status:properties.status,started:properties.startTime,"
        "finished:properties.endTime},&started)[-10:] | reverse(@)",
    )
    print("\nRecent executions")
    if not executions:
        print("  (none yet)")
        return
    for execution in executions:
        print(f"  {execution.get('started', '?'):<26} {execution.get('status', '?'):<12} {execution.get('name', '')}")


def cmd_start(args):
    record = deployed_job(args.subscription, args.job)
    mode = "LIVE COPY" if record["env"].get("COPY_DRY_RUN") == "false" else "dry run"
    run(
        "az",
        "containerapp",
        "job",
        "start",
        *subscription_args(args.subscription),
        "--resource-group",
        record["resourceGroup"],
        "--name",
        record["resourceName"],
        "--only-show-errors",
        "--output",
        "none",
    )
    print(f"[{args.job}] Started one {mode} execution. Check it with 'copyctl.py status {args.job}'.")


def cmd_apply(args):
    job = load_job_named(args.job)
    record = deployed_job(args.subscription, args.job)
    if not job["copy"]["dryRun"]:
        confirm(f"LIVE COPY {args.job}", f"Type 'LIVE COPY {args.job}' to publish this live configuration: ")
    # An already-active schedule follows the file. A parked one stays parked so
    # applying a configuration change can never start a job the operator has
    # not explicitly enabled.
    update_job(
        args.subscription,
        record,
        env_values=config_to_env(job),
        timeout_minutes=job["copy"]["timeoutMinutes"],
        cron=None if record["cron"] == PARKED_CRON else job["copy"]["scheduleUtc"],
    )
    mode = "dry-run" if job["copy"]["dryRun"] else "LIVE"
    print(f"[{args.job}] Published {mode} configuration. No image was deployed.")
    if record["cron"] == PARKED_CRON:
        print(f"[{args.job}] The schedule is still parked. Run 'copyctl.py enable {args.job}' to activate it.")


def cmd_pull(args):
    jobs = discover(args.subscription)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    for name in sorted(jobs):
        config = env_to_config(jobs[name])
        path = JOBS_DIR / f"{name}.json"
        path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {path}")
    print(f"\nWrote {len(jobs)} job file(s) from what is deployed in Azure.")


def cmd_enable(args):
    record = deployed_job(args.subscription, args.job)
    schedule = record["env"].get("COPY_SCHEDULE_UTC", "")
    if not schedule:
        fail(f"Job '{args.job}' has no COPY_SCHEDULE_UTC value to activate.")
    validate_cron(schedule, f"{args.job} schedule")
    update_job(args.subscription, record, cron=schedule)
    mode = "LIVE COPY" if record["env"].get("COPY_DRY_RUN") == "false" else "dry-run"
    print(f"[{args.job}] Schedule active: {schedule} UTC ({mode}).")


def cmd_disable(args):
    record = deployed_job(args.subscription, args.job)
    update_job(args.subscription, record, cron=PARKED_CRON)
    print(f"[{args.job}] Schedule parked. The job will not run until you enable it again.")


def cmd_go_live(args):
    record = deployed_job(args.subscription, args.job)
    if record["env"].get("COPY_DRY_RUN") == "false":
        print(f"[{args.job}] Already in live mode.")
        return
    print(
        f"[{args.job}] Switching from dry run to live upload.\n"
        f"  Source:      {record['env'].get('SOURCE_STORAGE_ACCOUNT')}/"
        f"{record['env'].get('SOURCE_CONTAINER_OR_SHARE')}/{record['env'].get('SOURCE_PATH', '')}\n"
        f"  Destination: {record['env'].get('DEST_SITE_URL')} / {record['env'].get('DEST_LIBRARY')} / "
        f"{record['env'].get('DEST_PATH', '')}\n"
        "  Files are uploaded. Nothing at the destination is ever deleted."
    )
    confirm(f"LIVE COPY {args.job}", f"Type 'LIVE COPY {args.job}' to continue: ")
    env_values = dict(record["env"])
    env_values["COPY_DRY_RUN"] = "false"
    update_job(args.subscription, record, env_values=env_values)
    print(f"[{args.job}] Live mode active. The next execution uploads files.")


def cmd_dry_run(args):
    record = deployed_job(args.subscription, args.job)
    env_values = dict(record["env"])
    env_values["COPY_DRY_RUN"] = "true"
    update_job(args.subscription, record, env_values=env_values)
    print(f"[{args.job}] Dry-run mode active. The next execution reports without uploading.")


def cmd_grant_source(args):
    record = deployed_job(args.subscription, args.job)
    job = load_job_named(args.job)
    subnet = subnet_id_for(record)
    run(
        "az",
        "storage",
        "account",
        "network-rule",
        "add",
        "--subscription",
        job["source"]["subscriptionId"],
        "--resource-group",
        job["source"]["resourceGroup"],
        "--account-name",
        job["source"]["storageAccount"],
        "--subnet",
        subnet,
        "--only-show-errors",
        "--output",
        "none",
    )
    print(
        f"[{args.job}] Added the copy subnet to {job['source']['storageAccount']}'s network rules. "
        "The account's default firewall action was not changed."
    )


def cmd_revoke_source(args):
    record = deployed_job(args.subscription, args.job)
    job = load_job_named(args.job)
    subnet = subnet_id_for(record)
    _, ok = run(
        "az",
        "storage",
        "account",
        "network-rule",
        "remove",
        "--subscription",
        job["source"]["subscriptionId"],
        "--resource-group",
        job["source"]["resourceGroup"],
        "--account-name",
        job["source"]["storageAccount"],
        "--subnet",
        subnet,
        "--only-show-errors",
        "--output",
        "none",
        allow_failure=True,
    )
    print(f"[{args.job}] " + ("Removed the copy subnet rule." if ok else "The subnet rule was already absent."))


def cmd_set_secret(args):
    record = deployed_job(args.subscription, args.job)
    if not record["vaultName"] or not record["secretName"]:
        fail(f"Job '{args.job}' has no Key Vault secret reference.")
    secret = getpass.getpass(
        f"[{args.job}] Paste the Microsoft Entra application client secret Value (input is hidden): "
    )
    if not secret:
        fail("No client secret was entered.")
    version = store_secret(args.subscription, record, secret)
    del secret
    # Container Apps resolves a Key Vault secret reference when the job is
    # created or updated and then caches it. Without this the job keeps using
    # the deployment placeholder and every execution fails against Entra.
    run(
        "az",
        "containerapp",
        "job",
        "update",
        *subscription_args(args.subscription),
        "--resource-group",
        record["resourceGroup"],
        "--name",
        record["resourceName"],
        "--set-env-vars",
        f"COPY_SECRET_VERSION={version}",
        "--only-show-errors",
        "--output",
        "none",
    )
    print(
        f"[{args.job}] Stored in Key Vault {record['vaultName']} and reloaded by the job. "
        f"Verify it with 'copyctl.py start {args.job}' while the job is still in dry-run mode."
    )


def store_secret(subscription, record, secret):
    """Send the secret straight to Azure Resource Manager over TLS.

    It is never written to disk, never placed in a command line where another
    process could read it, and never stored in deployment history.
    """
    subscription_id = record["resourceId"].split("/")[2]
    token = run(
        "az",
        "account",
        "get-access-token",
        *subscription_args(subscription),
        "--resource",
        "https://management.azure.com/",
        "--query",
        "accessToken",
        "--output",
        "tsv",
        "--only-show-errors",
    )
    url = (
        f"https://management.azure.com/subscriptions/{subscription_id}"
        f"/resourceGroups/{record['resourceGroup']}/providers/Microsoft.KeyVault"
        f"/vaults/{record['vaultName']}/secrets/{record['secretName']}?api-version=2023-07-01"
    )
    body = {
        "properties": {
            "value": secret,
            "contentType": f"Microsoft Entra application client secret for {record['name']}",
        },
        "tags": {"workload": WORKLOAD_TAG, "copyJob": record["name"]},
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(body, separators=(",", ":")).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="PUT",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        raw = error.read().decode(errors="replace")
        fail(f"Azure Key Vault rejected the secret update ({error.code}): {raw}")
    except urllib.error.URLError as error:
        fail(f"HTTPS request to Azure Key Vault failed: {error.reason}")
    # The trailing segment of the secret id is its version. Recording it on the
    # job both forces the cache refresh and documents which credential is live.
    return (body.get("properties", {}).get("secretUriWithVersion") or body.get("id") or "").rsplit("/", 1)[-1]


def confirm(expected, prompt):
    if input(prompt).strip() != expected:
        fail("Cancelled. Nothing was changed.")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        prog="copyctl.py",
        description="Operate the Azure to SharePoint copy service.",
    )
    parser.add_argument(
        "--subscription",
        default=os.environ.get("AZURE_SUBSCRIPTION_ID"),
        help="Azure subscription holding the copy service. Defaults to your active az subscription.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add(name, handler, help_text, needs_job=False):
        subparser = subparsers.add_parser(name, help=help_text)
        if needs_job:
            subparser.add_argument("job", help="Job name, as shown by 'copyctl.py list'.")
        subparser.set_defaults(handler=handler)
        return subparser

    validate = add("validate", cmd_validate, "Check every jobs/*.json file without contacting Azure.")
    validate.add_argument("--vnet", default="10.240.0.0/16")
    validate.add_argument("--subnet", default="10.240.0.0/27")

    add("env", cmd_env, "Print the container environment one job file produces.", needs_job=True)

    params = add("params", cmd_params, "Render an ARM parameters file from jobs/*.json.")
    params.add_argument(
        "--image",
        help="Digest-pinned container image. Defaults to the one this release was built against.",
    )
    params.add_argument(
        "--registry-id",
        help=(
            "Resource ID of a private Azure Container Registry holding the image. "
            "Required when the image is not publicly pullable, because Container Apps "
            "resolves the image while creating the job."
        ),
    )
    params.add_argument("--base-name", default="file-copy")
    params.add_argument("--vnet", default="10.240.0.0/16")
    params.add_argument("--subnet", default="10.240.0.0/27")
    params.add_argument("--out", help="Write to this path instead of standard output.")

    add("list", cmd_list, "List deployed copy jobs and their current mode.")
    add("status", cmd_status, "Show one job's configuration and recent executions.", needs_job=True)
    add("start", cmd_start, "Start one execution now.", needs_job=True)
    add("set-secret", cmd_set_secret, "Store or rotate one job's Entra client secret.", needs_job=True)
    add("apply", cmd_apply, "Publish jobs/JOB.json to the deployed job.", needs_job=True)
    add("pull", cmd_pull, "Write jobs/*.json from what is deployed in Azure.")
    add("enable", cmd_enable, "Activate one job's schedule.", needs_job=True)
    add("disable", cmd_disable, "Park one job's schedule so it stops running.", needs_job=True)
    add("go-live", cmd_go_live, "Switch one job from dry run to live upload.", needs_job=True)
    add("dry-run", cmd_dry_run, "Switch one job back to dry run.", needs_job=True)
    add("grant-source", cmd_grant_source, "Add the copy subnet to the source storage firewall.", needs_job=True)
    add("revoke-source", cmd_revoke_source, "Remove the copy subnet from the source storage firewall.", needs_job=True)
    return parser


def main():
    args = build_parser().parse_args()
    args.handler(args)


if __name__ == "__main__":
    try:
        main()
    except CopyctlError as error:
        print(f"\nStopped: {error}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        sys.exit(130)
