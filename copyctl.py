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
import tempfile
import time
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


def discover(subscription, resource_group=None):
    """Find every deployed copy job by tag. This is the only state lookup."""
    records = az_json(
        "containerapp",
        "job",
        "list",
        *subscription_args(subscription),
        "--query",
        f"[?tags.workload=='{WORKLOAD_TAG}' && tags.copyJob != null]",
    )
    if resource_group:
        records = [
            r for r in records if r["resourceGroup"].lower() == resource_group.lower()
        ]
    if not records:
        where = f" in resource group {resource_group}" if resource_group else ""
        fail(
            f"No deployed copy jobs were found{where}. "
            "Check that you selected the right subscription with 'az account set'."
        )
    # More than one deployment can share a subscription, and job names are only
    # unique within a deployment. Silently picking one could point a live-mode
    # switch at somebody else's production job, so ambiguity is always fatal.
    by_name = {}
    for record in records:
        by_name.setdefault(record["tags"]["copyJob"], []).append(record["resourceGroup"])
    ambiguous = {n: sorted(set(g)) for n, g in by_name.items() if len(set(g)) > 1}
    if ambiguous:
        detail = "; ".join(f"'{n}' in {', '.join(g)}" for n, g in sorted(ambiguous.items()))
        fail(
            f"Job name(s) exist in more than one deployment: {detail}. "
            "Re-run with --resource-group to say which deployment you mean."
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
        identities = (record.get("identity") or {}).get("userAssignedIdentities") or {}
        identity_client_id = next(
            (v.get("clientId", "") for v in identities.values() if v.get("clientId")), ""
        )
        jobs[record["tags"]["copyJob"]] = {
            "name": record["tags"]["copyJob"],
            "identityClientId": identity_client_id,
            "resourceName": record["name"],
            "resourceGroup": record["resourceGroup"],
            "location": record.get("location", ""),
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


def workspace_id_for(subscription, record):
    """Log Analytics customer ID behind this job's Container Apps environment.

    Read from the environment rather than stored, for the same reason discovery
    is tag-driven: nothing about a deployment is cached on this machine.
    """
    environment = az_json(
        "containerapp",
        "env",
        "show",
        *subscription_args(subscription),
        "--ids",
        record["environmentId"],
        "--query",
        "properties.appLogsConfiguration.logAnalyticsConfiguration.customerId",
    )
    if not environment:
        fail(
            "This job's Container Apps environment has no Log Analytics workspace, "
            "so execution output cannot be read back."
        )
    return environment


# Nothing is readable until the replica has started and rclone has run, so the
# first poll is delayed rather than spent on a query that cannot yet succeed.
PREVIEW_FIRST_POLL_SECONDS = 20
PREVIEW_POLL_SECONDS = 5
# A preview waits a minute or two with nothing to report. Ticking faster than it
# polls keeps the elapsed counter moving, so the wait cannot be read as a hang.
PREVIEW_TICK_SECONDS = 1


def preview_progress(elapsed, note):
    """Overwrite one line with the elapsed time, only when a person is watching.

    Piped or redirected output stays clean for scripting.
    """
    if not sys.stdout.isatty():
        return
    minutes, seconds = divmod(int(elapsed), 60)
    sys.stdout.write(f"\r  {minutes}:{seconds:02d} elapsed - {note}".ljust(72))
    sys.stdout.flush()


def clear_preview_progress():
    if sys.stdout.isatty():
        sys.stdout.write("\r".ljust(72) + "\r")
        sys.stdout.flush()


def preview_preflight(subscription, workspace):
    """Prove the results can be read back before anything is started.

    Starting an execution and only then discovering that Log Analytics is
    unreachable would leave a run nobody can observe, so both the extension and
    read access are checked first. Preview is the only command that reads the
    workspace, so an operator who can run every other command may still lack it.
    """
    _, ok = run(
        "az", "extension", "show", "--name", "log-analytics",
        "--only-show-errors", "--output", "none",
        allow_failure=True,
    )
    if not ok:
        fail(
            "Preview needs the 'log-analytics' Azure CLI extension to read execution "
            "results. Install it with: az extension add --name log-analytics"
        )
    # `print` answers without touching a table, so a workspace that has not yet
    # ingested anything is not mistaken for one that cannot be read.
    _, ok = run(
        "az", "monitor", "log-analytics", "query",
        *subscription_args(subscription),
        "--workspace", workspace,
        "--analytics-query", "print probe=1",
        "--only-show-errors", "--output", "none",
        allow_failure=True,
    )
    if not ok:
        fail(
            f"Could not read Log Analytics workspace {workspace}. Preview needs read "
            "access to it, which no other copyctl.py command requires. Ask for the "
            "'Log Analytics Reader' role on that workspace."
        )


def execution_outcome_rows(subscription, workspace, execution):
    """Every row that could end a preview, fetched in one query.

    Container Apps names each replica after its execution, so the prefix match
    keeps one preview from reading another execution's output. Statistics and
    the wrapper's terminal records are collected together because a round trip
    to Log Analytics costs far more than the extra rows.
    """
    rows = az_json(
        "monitor",
        "log-analytics",
        "query",
        *subscription_args(subscription),
        "--workspace",
        workspace,
        "--analytics-query",
        f"ContainerAppConsoleLogs_CL "
        f"| where ContainerGroupName_s startswith '{execution}' "
        f"| where Log_s contains '\"stats\"' "
        f"or Log_s contains 'runtime_error' "
        f"or Log_s contains 'transfer_complete' "
        f"| project TimeGenerated, Log_s "
        f"| order by TimeGenerated asc",
    )
    return rows or []


def wait_for_execution_stats(subscription, workspace, execution, wait_seconds):
    """Poll until rclone's final stats object for this execution is readable.

    Polling the log rather than the execution status is both faster and more
    precise: the statistics are written when rclone finishes, while the
    execution is not marked complete until its replica is torn down.
    """
    started = time.monotonic()
    deadline = started + wait_seconds
    next_query = started + min(PREVIEW_FIRST_POLL_SECONDS, max(0, wait_seconds))
    note = "starting the execution"
    while True:
        now = time.monotonic()
        if now >= next_query:
            preview_progress(now - started, "reading results")
            terminal = []
            for row in execution_outcome_rows(subscription, workspace, execution):
                line = row["Log_s"]
                if '"stats"' in line:
                    try:
                        stats = json.loads(line).get("stats")
                    except json.JSONDecodeError:
                        stats = None
                    if stats:
                        clear_preview_progress()
                        return stats
                else:
                    terminal.append(line)
            # A run that fails before rclone starts never writes statistics, so
            # the wrapper's terminal records end the wait, not the deadline.
            if terminal:
                clear_preview_progress()
                return {"_failed": terminal}
            next_query = time.monotonic() + PREVIEW_POLL_SECONDS
            note = "waiting for the execution to finish"
        if time.monotonic() >= deadline:
            clear_preview_progress()
            return None
        preview_progress(time.monotonic() - started, note)
        time.sleep(PREVIEW_TICK_SECONDS)


def human_bytes(count):
    value = float(count)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:,.2f} {unit}" if unit != "B" else f"{int(value):,} B"
        value /= 1024


def deployed_job(subscription, name, resource_group=None):
    jobs = discover(subscription, resource_group)
    if name not in jobs:
        available = ", ".join(sorted(jobs)) or "(none)"
        fail(f"Job '{name}' is not deployed. Deployed jobs: {available}")
    return jobs[name]


def deployment_settings(subscription, deployed, registry_id=""):
    """Read one deployment's shared template parameters back out of Azure.

    Adding a job re-declares the network, the environment, and the vault beside
    it. Those declarations are only harmless when every shared value matches
    what is already deployed, so they are read from Azure rather than retyped:
    a mistyped baseName would build a second deployment alongside the first, and
    a mistyped address prefix would try to renumber a network other jobs run on.
    """
    groups = sorted({record["resourceGroup"] for record in deployed.values()})
    if len(groups) > 1:
        fail(
            f"This subscription holds more than one deployment ({', '.join(groups)}). "
            "Re-run with --resource-group to say which one the new job joins."
        )
    base_names = set()
    for name, record in deployed.items():
        suffix = f"-{name}"
        if not record["resourceName"].endswith(suffix):
            fail(
                f"Deployed job '{name}' is named {record['resourceName']}, which does not "
                "follow the '<baseName>-<job>' convention this command relies on."
            )
        base_names.add(record["resourceName"][: -len(suffix)])
    if len(base_names) > 1:
        fail(
            f"Deployed jobs disagree about baseName ({', '.join(sorted(base_names))}). "
            "Re-run with --resource-group to select a single deployment."
        )
    reference = deployed[sorted(deployed)[0]]
    subnet_id = subnet_id_for(reference)
    subnet = az_json(
        "network", "vnet", "subnet", "show",
        *subscription_args(subscription),
        "--ids", subnet_id,
        "--query", "addressPrefix",
    )
    vnet = az_json(
        "network", "vnet", "show",
        *subscription_args(subscription),
        "--ids", subnet_id.split("/subnets/")[0],
        "--query", "addressSpace.addressPrefixes[0]",
    )
    if not subnet or not vnet:
        fail("Could not read the deployed network's address prefixes.")
    # Deploying a job on a different image than the fleet runs would leave one
    # route on a version nobody reviewed, so the running image is the default.
    images = {record["image"] for record in deployed.values()}
    return {
        "resourceGroup": reference["resourceGroup"],
        "location": reference["location"],
        "baseName": base_names.pop(),
        "vnet": vnet,
        "subnet": subnet,
        "image": images.pop() if len(images) == 1 else "",
        # An operator who supplies the registry is not asked to prove it exists
        # here: the lookup only searches this subscription, and a registry in
        # another one is exactly the case --registry-id is for.
        "registryId": registry_id or registry_id_for(subscription, reference),
    }


def registry_id_for(subscription, record):
    """The private registry backing a deployed job, if it uses one.

    Jobs pulling from a private registry need AcrPull before Container Apps can
    resolve the image, and the template grants it from the registry's resource
    ID. Omitting it for a deployment that uses one would fail at creation time.
    """
    server = az_json(
        "containerapp", "job", "show",
        *subscription_args(subscription),
        "--ids", record["resourceId"],
        "--query", "properties.configuration.registries[0].server",
    )
    if not server:
        return ""
    name = server.split(".")[0]
    found, ok = run(
        "az", "acr", "show",
        *subscription_args(subscription),
        "--name", name,
        "--query", "id",
        "--only-show-errors", "--output", "tsv",
        allow_failure=True,
    )
    if not ok or not found:
        fail(
            f"The deployed jobs pull from the private registry {server}, which was not found "
            "in this subscription. Re-run with --registry-id <registry resource ID>."
        )
    return found


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


def merged_env(deployed, from_job_file, identity_client_id=""):
    """Overlay the job file's variables onto what is already deployed.

    The deployment template sets variables no job file knows about, notably
    AZURE_MANAGED_IDENTITY_CLIENT_ID and the rclone tuning values. Replacing the
    environment with only the job file's fields would drop them and leave the
    job unable to authenticate to its source.

    The identity variable is additionally restored from the job's own assigned
    identity when it is missing, so a job left in that state can be repaired by
    running apply rather than redeploying.
    """
    merged = dict(deployed)
    merged.update(from_job_file)
    if not merged.get("AZURE_MANAGED_IDENTITY_CLIENT_ID") and identity_client_id:
        merged["AZURE_MANAGED_IDENTITY_CLIENT_ID"] = identity_client_id
    return merged


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
        pairs = [
            f"{key}={value}"
            for key, value in sorted(
                merged_env(record["env"], env_values, record.get("identityClientId", "")).items()
            )
            if key != SECRET_ENV_NAME
        ]
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


def params_document(jobs, base_name, vnet, subnet, image, registry_id="", location=""):
    document = {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
        "contentVersion": "1.0.0.0",
        "parameters": {
            "baseName": {"value": base_name},
            "vnetAddressPrefix": {"value": vnet},
            "containerAppsSubnetPrefix": {"value": subnet},
            "containerImage": {"value": image},
            "jobs": {"value": jobs},
        },
    }
    if registry_id:
        document["parameters"]["containerRegistryResourceId"] = {"value": registry_id}
    if location:
        document["parameters"]["location"] = {"value": location}
    validate_network(vnet, subnet)
    return document


def cmd_params(args):
    document = params_document(
        load_jobs(),
        args.base_name,
        args.vnet,
        args.subnet,
        args.image or published_image(),
        args.registry_id or "",
    )
    jobs = document["parameters"]["jobs"]["value"]
    rendered = json.dumps(document, indent=2) + "\n"
    if args.out:
        Path(args.out).write_text(rendered, encoding="utf-8")
        print(f"Wrote {args.out} with {len(jobs)} job(s).")
    else:
        sys.stdout.write(rendered)


def cmd_list(args):
    jobs = discover(args.subscription, args.resource_group)
    print(f"{'JOB':<20} {'MODE':<8} {'SCHEDULE':<16} {'AZURE RESOURCE'}")
    for name in sorted(jobs):
        record = jobs[name]
        mode = "LIVE" if record["env"].get("COPY_DRY_RUN") == "false" else "dry run"
        cron = record["cron"]
        schedule = "parked" if cron == PARKED_CRON else cron
        print(f"{name:<20} {mode:<8} {schedule:<16} {record['resourceName']}")


def cmd_status(args):
    record = deployed_job(args.subscription, args.job, args.resource_group)
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
    record = deployed_job(args.subscription, args.job, args.resource_group)
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


def cmd_preview(args):
    """Report what one dry-run execution would copy, without uploading anything.

    This reads rclone's own final statistics rather than recounting anything, so
    the numbers are exactly the ones the real copy would act on.
    """
    record = deployed_job(args.subscription, args.job, args.resource_group)
    env = record["env"]
    # A preview must never upload. Container Apps offers no safe per-execution
    # override to lean on: `job start --env-vars` is ignored outright, and the
    # --yaml form replaces the whole container template, dropping the Key Vault
    # secret reference with it. Rebuilding that template here to force one
    # variable would risk a live upload whenever the rebuild was imperfect, so
    # the mode is left to the operator and stated plainly instead.
    if env.get("COPY_DRY_RUN") == "false":
        fail(
            f"Job '{args.job}' is in live mode, so an execution would upload files.\n"
            f"  Switch it, preview, then switch back:\n"
            f"    copyctl.py dry-run {args.job}\n"
            f"    copyctl.py preview {args.job}\n"
            f"    copyctl.py go-live {args.job}\n"
            f"Switching to dry run never uploads, and leaves the schedule untouched."
        )
    print(f"Job              {record['name']} ({record['resourceName']})")
    print(f"Source           {env.get('SOURCE_TYPE')}: {env.get('SOURCE_STORAGE_ACCOUNT')}/"
          f"{env.get('SOURCE_CONTAINER_OR_SHARE')}/{env.get('SOURCE_PATH', '')}")
    print(f"Destination      {env.get('DEST_SITE_URL')} / {env.get('DEST_LIBRARY')} / {env.get('DEST_PATH', '')}")
    print(f"Existing files   {env.get('COPY_EXISTING_FILES')}")

    workspace = workspace_id_for(args.subscription, record)
    preview_preflight(args.subscription, workspace)
    started = az_json(
        "containerapp",
        "job",
        "start",
        *subscription_args(args.subscription),
        "--resource-group",
        record["resourceGroup"],
        "--name",
        record["resourceName"],
    )
    execution = (started or {}).get("name") or ""
    if not execution:
        fail("Azure did not return an execution name for the preview run.")
    print(f"\nPreview execution {execution} started. Waiting for results (usually 1-2 minutes).")

    stats = wait_for_execution_stats(args.subscription, workspace, execution, args.wait)
    if stats is None:
        fail(
            f"No results within {args.wait}s. The execution may still be running: "
            f"check it with 'copyctl.py status {args.job}'."
        )
    if "_failed" in stats:
        print("\nThe preview execution ended without copying anything:")
        for line in stats["_failed"]:
            print(f"  {line}")
        fail("Preview did not complete. Nothing was uploaded.")

    print(f"\nWould copy       {stats.get('totalTransfers', 0):,} files, "
          f"{human_bytes(stats.get('totalBytes', 0))}")
    print(f"Objects listed   {stats.get('listed', 0):,}")
    print(f"Errors           {stats.get('errors', 0)}")
    print(f"Elapsed          {stats.get('elapsedTime', 0):.1f}s")
    print("\nNothing was uploaded. This job is still in dry-run mode.")


def cmd_deploy(args):
    """Create one new job beside the ones already deployed.

    The template describes a whole deployment, so re-rendering it from every
    jobs/*.json and redeploying would rewrite each existing job: schedules go
    back to parked and every Key Vault secret returns to its placeholder. This
    command deploys a parameter set holding the new job alone. ARM's incremental
    mode leaves out what the template does not mention, so the other jobs, their
    secrets, and their schedules are never touched, while the shared network,
    environment, and vault are re-declared exactly as deployed and change
    nothing.
    """
    job = load_job_named(args.job)
    template = ROOT / "infra" / "main.json"
    if not template.exists():
        fail(f"{template} is missing. Run this command from a checkout of the deployment package.")
    try:
        deployed = discover(args.subscription, args.resource_group)
    except CopyctlError as error:
        fail(
            f"{error}\n"
            "'deploy' adds a job to an existing deployment. For a first install, render every "
            "job with 'copyctl.py params' and deploy it as README.md describes."
        )
    if args.job in deployed:
        fail(
            f"Job '{args.job}' is already deployed. "
            f"Use 'copyctl.py apply {args.job}' to publish configuration changes to it."
        )
    settings = deployment_settings(args.subscription, deployed, args.registry_id or "")
    image = args.image or settings["image"]
    if not image:
        fail(
            "The deployed jobs do not all run the same image, so there is no safe default "
            "for the new one. Re-run with --image <digest-pinned image>."
        )
    document = params_document(
        [job],
        settings["baseName"],
        settings["vnet"],
        settings["subnet"],
        image,
        settings["registryId"],
        settings["location"],
    )
    print(
        f"[{args.job}] Adding one job to the deployment in {settings['resourceGroup']}.\n"
        f"  Existing jobs:  {', '.join(sorted(deployed))} (not included, so not changed)\n"
        f"  Image:          {image}\n"
        f"  Source:         {job['source']['storageAccount']}/{job['source']['containerOrShare']}\n"
        f"  Destination:    {job['destination']['siteUrl']} / {job['destination']['library']}"
    )
    if not job["copy"]["dryRun"]:
        confirm(
            f"LIVE COPY {args.job}",
            f"This job is configured to upload, not to dry run. "
            f"Type 'LIVE COPY {args.job}' to deploy it live: ",
        )
    with tempfile.TemporaryDirectory() as directory:
        parameters = Path(directory) / f"{args.job}-params.json"
        parameters.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        common = (
            "--resource-group", settings["resourceGroup"],
            *subscription_args(args.subscription),
            "--template-file", str(template),
            "--parameters", f"@{parameters}",
            # The default, stated so that a change of defaults could never turn
            # this into a deployment that removes the jobs it does not mention.
            "--mode", "Incremental",
            "--only-show-errors",
        )
        print("\nPreviewing the change (az deployment group what-if). This takes a minute.\n")
        print(run("az", "deployment", "group", "what-if", *common))
        print(
            "\nOnly resources for this job should be marked Create, plus role assignments "
            "on its source. Anything marked Modify or Delete on an existing job is a stop."
        )
        confirm(f"ADD {args.job}", f"\nType 'ADD {args.job}' to deploy: ")
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S")
        print("\nDeploying. Creating the job and its identity takes a few minutes.")
        run(
            "az", "deployment", "group", "create",
            "--name", f"add-{args.job}-{stamp}",
            *common,
            "--output", "none",
        )
    mode = "dry-run mode" if job["copy"]["dryRun"] else "LIVE mode"
    print(f"\n[{args.job}] Deployed in {mode} with its schedule parked.")
    print(f"  ./copyctl.py set-secret {args.job}     # store the Entra client secret")
    print(f"  ./copyctl.py grant-source {args.job}   # only if the source has a storage firewall")
    print(f"  ./copyctl.py start {args.job}          # run one execution now")
    if job["copy"]["dryRun"]:
        print(f"  ./copyctl.py go-live {args.job}        # dry run -> live, after reviewing that run")
    print(f"  ./copyctl.py enable {args.job}         # activate the schedule")


def cmd_apply(args):
    job = load_job_named(args.job)
    record = deployed_job(args.subscription, args.job, args.resource_group)
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
    jobs = discover(args.subscription, args.resource_group)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    for name in sorted(jobs):
        config = env_to_config(jobs[name])
        path = JOBS_DIR / f"{name}.json"
        path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {path}")
    print(f"\nWrote {len(jobs)} job file(s) from what is deployed in Azure.")


def cmd_enable(args):
    record = deployed_job(args.subscription, args.job, args.resource_group)
    schedule = record["env"].get("COPY_SCHEDULE_UTC", "")
    if not schedule:
        fail(f"Job '{args.job}' has no COPY_SCHEDULE_UTC value to activate.")
    validate_cron(schedule, f"{args.job} schedule")
    update_job(args.subscription, record, cron=schedule)
    mode = "LIVE COPY" if record["env"].get("COPY_DRY_RUN") == "false" else "dry-run"
    print(f"[{args.job}] Schedule active: {schedule} UTC ({mode}).")


def cmd_disable(args):
    record = deployed_job(args.subscription, args.job, args.resource_group)
    update_job(args.subscription, record, cron=PARKED_CRON)
    print(f"[{args.job}] Schedule parked. The job will not run until you enable it again.")


def cmd_go_live(args):
    record = deployed_job(args.subscription, args.job, args.resource_group)
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
    record = deployed_job(args.subscription, args.job, args.resource_group)
    env_values = dict(record["env"])
    env_values["COPY_DRY_RUN"] = "true"
    update_job(args.subscription, record, env_values=env_values)
    print(f"[{args.job}] Dry-run mode active. The next execution reports without uploading.")


def cmd_grant_source(args):
    record = deployed_job(args.subscription, args.job, args.resource_group)
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
    record = deployed_job(args.subscription, args.job, args.resource_group)
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


def cmd_check_source(args):
    """Report which access layers between a job and its source account work.

    Azure Storage answers most misconfigurations with the same 403, whether the
    cause is a missing role, a firewall rule, disabled public access, or a
    private endpoint arrangement this deployment does not participate in. These
    read-only control-plane checks pull those layers apart. Checks the operator
    lacks permission to run are reported as unknown, never as passed.
    """
    record = deployed_job(args.subscription, args.job, args.resource_group)
    env = record["env"]
    source_subscription = env.get("SOURCE_SUBSCRIPTION_ID", "")
    source_group = env.get("SOURCE_RESOURCE_GROUP", "")
    account = env.get("SOURCE_STORAGE_ACCOUNT", "")
    service = "file" if env.get("SOURCE_TYPE") == "azure_files" else "blob"
    account_id = (
        f"/subscriptions/{source_subscription}/resourceGroups/{source_group}"
        f"/providers/Microsoft.Storage/storageAccounts/{account}"
    )
    problems = []

    def report(label, state, detail):
        marker = {"ok": "ok      ", "FAIL": "FAIL    ", "unknown": "unknown ", "info": "info    "}[state]
        print(f"  {marker}{label:<22} {detail}")
        if state == "FAIL":
            problems.append(label)

    print(f"[{args.job}] Access layers for {service} account {account}\n")

    expected_role = (
        "Storage File Data Privileged Reader" if service == "file" else "Storage Blob Data Reader"
    )
    principal, ok = run(
        "az", "identity", "show",
        *subscription_args(args.subscription),
        "--resource-group", record["resourceGroup"],
        "--name", f"{record['resourceName']}-identity",
        "--query", "principalId", "--output", "tsv", "--only-show-errors",
        allow_failure=True,
    )
    if not ok or not principal:
        report("job identity", "FAIL", f"managed identity {record['resourceName']}-identity was not found")
    else:
        report("job identity", "ok", principal)
        assignments, ok = run(
            "az", "role", "assignment", "list",
            "--subscription", source_subscription,
            "--assignee", principal,
            "--all",
            "--query", "[].{role: roleDefinitionName, scope: scope}",
            "--output", "json", "--only-show-errors",
            allow_failure=True,
        )
        if not ok:
            report("role assignment", "unknown", "no permission to list role assignments in the source subscription")
        else:
            roles = sorted({
                entry["role"]
                for entry in json.loads(assignments or "[]")
                if entry["scope"].lower().startswith(account_id.lower())
            })
            if expected_role in roles:
                report("role assignment", "ok", f"{expected_role} on the source")
            else:
                report(
                    "role assignment", "FAIL",
                    f"expected '{expected_role}' on the source; found {', '.join(roles) or 'none'} "
                    "- redeploy the template to recreate it",
                )

    subnet_id = subnet_id_for(record)
    settings, ok = run(
        "az", "storage", "account", "show",
        "--subscription", source_subscription,
        "--resource-group", source_group,
        "--name", account,
        "--query", "{public: publicNetworkAccess, action: networkRuleSet.defaultAction,"
        " rules: networkRuleSet.virtualNetworkRules[].{id: virtualNetworkResourceId, state: state}}",
        "--output", "json", "--only-show-errors",
        allow_failure=True,
    )
    public = ""
    if not ok:
        report("network gate", "unknown", "no permission to read the storage account's settings")
    else:
        gate = json.loads(settings or "{}")
        # An account created before the setting existed reports null, which the
        # service treats as enabled.
        public = gate.get("public") or "Enabled"
        action = gate.get("action") or "Allow"
        if public.lower() == "disabled":
            report(
                "network gate", "FAIL",
                "public network access is disabled, and this deployment reaches the account "
                "over its public endpoint - ask the account's owner to re-enable it",
            )
        elif action.lower() == "allow":
            report("network gate", "ok", "the firewall allows all networks")
        else:
            rules = gate.get("rules") or []
            matched = next((rule for rule in rules if rule["id"].lower() == subnet_id.lower()), None)
            if matched is None:
                report(
                    "firewall rule", "FAIL",
                    f"the copy subnet is not in the account's rules - run 'copyctl.py grant-source {args.job}'",
                )
            elif (matched.get("state") or "Succeeded") != "Succeeded":
                report(
                    "firewall rule", "FAIL",
                    f"the copy subnet rule is in state {matched['state']} - "
                    f"re-run 'copyctl.py grant-source {args.job}'",
                )
            else:
                report("firewall rule", "ok", "the copy subnet is in the account's rules")
            endpoints, ok = run(
                "az", "network", "vnet", "subnet", "show",
                "--ids", subnet_id,
                "--query", "serviceEndpoints[].service", "--output", "json", "--only-show-errors",
                allow_failure=True,
            )
            services = json.loads(endpoints or "[]") if ok else []
            if not ok:
                report("service endpoint", "unknown", "could not read the copy subnet")
            elif any(item in ("Microsoft.Storage.Global", "Microsoft.Storage") for item in services):
                report("service endpoint", "ok", "the copy subnet carries a storage service endpoint")
            else:
                report(
                    "service endpoint", "FAIL",
                    "the copy subnet has no storage service endpoint - "
                    "redeploy the template to restore it",
                )

    # Informational: this deployment cannot create or use a private endpoint,
    # but connections other systems requested still shape what can reach the
    # account, and are the only path in once public access is disabled.
    listing, ok = run(
        "az", "network", "private-endpoint-connection", "list",
        "--subscription", source_subscription,
        "--resource-group", source_group,
        "--name", account,
        "--type", "Microsoft.Storage/storageAccounts",
        "--output", "json", "--only-show-errors",
        allow_failure=True,
    )
    if not ok:
        report("private endpoints", "unknown", "no permission to list the account's private endpoint connections")
    else:
        connections = json.loads(listing or "[]")
        states = []
        approved = 0
        for connection in connections:
            properties = connection.get("properties") or connection
            status = ((properties.get("privateLinkServiceConnectionState") or {}).get("status")) or "Unknown"
            if status == "Approved":
                approved += 1
            states.append(f"{connection.get('name', '?')} ({status})")
        if not connections:
            report("private endpoints", "info", "none exist on this account")
        else:
            detail = f"{len(connections)} connection(s): " + ", ".join(states)
            if public.lower() == "disabled" and not approved:
                detail += " - none is approved, so nothing can reach this account until one is"
            elif public.lower() == "disabled":
                detail += (
                    " - an approved endpoint can reach the account, but this deployment "
                    "connects over the public endpoint and cannot use it"
                )
            report("private endpoints", "info", detail)

    print()
    if problems:
        print(f"{len(problems)} layer(s) need attention: {', '.join(problems)}.")
        sys.exit(1)
    print("Every readable layer looks correct. If runs still fail with a 403, re-check any "
          "layer reported as unknown with someone who can read the source subscription.")


def cmd_set_secret(args):
    record = deployed_job(args.subscription, args.job, args.resource_group)
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
    # Shared options live on every subcommand so they can be given where an
    # operator naturally types them: copyctl.py status default -g my-group
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--resource-group",
        "-g",
        help="Resource group holding the copy service. Required when one subscription has more than one deployment.",
    )
    common.add_argument(
        "--subscription",
        default=os.environ.get("AZURE_SUBSCRIPTION_ID"),
        help="Azure subscription holding the copy service. Defaults to your active az subscription.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add(name, handler, help_text, needs_job=False):
        subparser = subparsers.add_parser(name, help=help_text, parents=[common])
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

    deploy = add(
        "deploy",
        cmd_deploy,
        "Create one new job from jobs/JOB.json, leaving deployed jobs untouched.",
        needs_job=True,
    )
    deploy.add_argument(
        "--image",
        help="Digest-pinned container image. Defaults to the image the deployed jobs already run.",
    )
    deploy.add_argument(
        "--registry-id",
        help=(
            "Resource ID of a private Azure Container Registry holding the image. "
            "Defaults to the registry the deployed jobs pull from."
        ),
    )

    add("list", cmd_list, "List deployed copy jobs and their current mode.")
    add("status", cmd_status, "Show one job's configuration and recent executions.", needs_job=True)
    add("start", cmd_start, "Start one execution now.", needs_job=True)
    preview = add(
        "preview",
        cmd_preview,
        "Report what a dry run would copy, without uploading anything.",
        needs_job=True,
    )
    preview.add_argument(
        "--wait",
        type=int,
        default=900,
        help="Seconds to wait for the preview execution's results. Default 900.",
    )
    add("set-secret", cmd_set_secret, "Store or rotate one job's Entra client secret.", needs_job=True)
    add("apply", cmd_apply, "Publish jobs/JOB.json to the deployed job.", needs_job=True)
    add("pull", cmd_pull, "Write jobs/*.json from what is deployed in Azure.")
    add("enable", cmd_enable, "Activate one job's schedule.", needs_job=True)
    add("disable", cmd_disable, "Park one job's schedule so it stops running.", needs_job=True)
    add("go-live", cmd_go_live, "Switch one job from dry run to live upload.", needs_job=True)
    add("dry-run", cmd_dry_run, "Switch one job back to dry run.", needs_job=True)
    add("grant-source", cmd_grant_source, "Add the copy subnet to the source storage firewall.", needs_job=True)
    add("revoke-source", cmd_revoke_source, "Remove the copy subnet from the source storage firewall.", needs_job=True)
    add(
        "check-source",
        cmd_check_source,
        "Explain which access layers between a job and its source account work, and which do not.",
        needs_job=True,
    )
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
