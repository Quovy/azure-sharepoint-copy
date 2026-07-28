#!/usr/bin/env python3
"""Checks infra/createUiDefinition.json against the template it feeds.

The portal builds each job object by concatenating string literals around form
values, which makes an unbalanced brace or a missing comma easy to introduce and
impossible to notice until a customer is halfway through a deployment. This test
reconstructs the concatenation with sample values and validates the result the
same way copyctl.py validates a job file.
"""

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI_PATH = ROOT / "infra" / "createUiDefinition.json"
COPYCTL = ROOT / "copyctl.py"

failures = []

# One sample value per expression the jobs output interpolates. Keys are the
# expression text with all whitespace removed.
SAMPLE_VALUES = {
    "coalesce(job.name,'')": "invoices",
    "coalesce(job.sourceType,'azure_files')": "azure_files",
    "subscription().subscriptionId": "00000000-0000-4000-8000-000000000000",
    "first(skip(split(steps('source').storageAccount.id,'/'),4))": "rg-source-files",
    "steps('source').storageAccount.name": "examplestorage",
    "coalesce(job.containerOrShare,'')": "source-share",
    "coalesce(job.sourcePath,'')": "Invoices/2026",
    "subscription().tenantId": "00000000-0000-4000-8000-000000000001",
    "coalesce(job.clientId,'')": "00000000-0000-4000-8000-000000000002",
    "coalesce(job.siteUrl,'')": "https://contoso.sharepoint.com/sites/Records",
    "coalesce(job.library,'Documents')": "Documents",
    "coalesce(job.destPath,'')": "Imported",
    "steps('behavior').existingFiles": "skip",
    "coalesce(job.scheduleUtc,'0 2 * * *')": "0 2 * * *",
    "string(steps('behavior').timeoutMinutes)": "360",
}


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")


def normalize_expression(text):
    """Drop whitespace between tokens but keep it inside quoted literals.

    A cron fallback like '0 2 * * *' must survive normalization intact.
    """
    result = []
    in_quotes = False
    for character in text:
        if character == "'":
            in_quotes = not in_quotes
            result.append(character)
        elif in_quotes or not character.isspace():
            result.append(character)
    return "".join(result)


def split_arguments(text):
    """Split a function argument list on commas that sit at nesting depth zero."""
    arguments = []
    current = ""
    depth = 0
    in_quotes = False
    index = 0
    while index < len(text):
        character = text[index]
        if in_quotes:
            # '' is an escaped single quote inside a createUiDefinition literal.
            if character == "'" and text[index : index + 2] == "''":
                current += "''"
                index += 2
                continue
            if character == "'":
                in_quotes = False
            current += character
        elif character == "'":
            in_quotes = True
            current += character
        elif character in "([":
            depth += 1
            current += character
        elif character in ")]":
            depth -= 1
            current += character
        elif character == "," and depth == 0:
            arguments.append(current.strip())
            current = ""
        else:
            current += character
        index += 1
    if current.strip():
        arguments.append(current.strip())
    return arguments


definition = json.loads(UI_PATH.read_text())
outputs = definition["parameters"]["outputs"]

for required_output in ("location", "baseName", "vnetAddressPrefix", "containerAppsSubnetPrefix", "containerImage", "jobs"):
    check("outputs", required_output in outputs, f"missing output '{required_output}'")

# Every output must line up with a parameter the template actually declares.
template_parameters = set(
    re.findall(r"^param\s+([A-Za-z0-9_]+)\s", (ROOT / "infra" / "main.bicep").read_text(), re.M)
)
for output_name in outputs:
    if output_name == "location":
        continue
    check(
        "output-matches-template",
        output_name in template_parameters,
        f"createUiDefinition emits '{output_name}' but main.bicep has no such parameter",
    )

jobs_expression = outputs["jobs"]
match = re.search(r"parse\(concat\((.*)\)\)\)\]$", jobs_expression)
check("jobs-expression-shape", match is not None, "could not find parse(concat(...)) in the jobs output")

if match:
    pieces = split_arguments(match.group(1))
    rebuilt = ""
    for piece in pieces:
        if piece.startswith("'") and piece.endswith("'"):
            rebuilt += piece[1:-1].replace("''", "'")
            continue
        key = normalize_expression(piece)
        if key not in SAMPLE_VALUES:
            failures.append(f"jobs-expression: no sample value for expression '{piece}'")
            rebuilt += "UNKNOWN"
            continue
        rebuilt += SAMPLE_VALUES[key]

    try:
        job = json.loads(rebuilt)
    except json.JSONDecodeError as error:
        job = None
        failures.append(f"jobs-expression: portal would emit invalid JSON ({error}): {rebuilt}")

    if job is not None:
        check("dry-run-forced", job["copy"]["dryRun"] is True, "the portal must always deploy in dry-run mode")
        check(
            "timeout-is-number",
            isinstance(job["copy"]["timeoutMinutes"], int),
            f"timeoutMinutes came through as {type(job['copy']['timeoutMinutes']).__name__}",
        )

        # The decisive check: run the reconstructed object through the same
        # validation a hand-written job file gets.
        job["name"] = "invoices"
        with tempfile.TemporaryDirectory() as directory:
            jobs_dir = Path(directory) / "jobs"
            jobs_dir.mkdir()
            (jobs_dir / "invoices.json").write_text(json.dumps(job))
            result = subprocess.run(
                [sys.executable, str(COPYCTL), "validate"],
                env={"COPY_JOBS_DIR": str(jobs_dir), "PATH": "/usr/bin:/bin"},
                text=True,
                capture_output=True,
                check=False,
            )
        check("portal-output-validates", result.returncode == 0, result.stderr.strip() or result.stdout.strip())

# The grid's column ids must match the job.<field> references in the output.
grid = None
for step in definition["parameters"]["steps"]:
    for element in step["elements"]:
        if element.get("type") == "Microsoft.Common.EditableGrid":
            grid = element
check("grid-present", grid is not None, "no EditableGrid found")
if grid:
    column_ids = {column["id"] for column in grid["constraints"]["columns"]}
    referenced = set(re.findall(r"job\.([A-Za-z0-9_]+)", jobs_expression))
    missing = sorted(referenced - column_ids)
    check("grid-columns", not missing, f"the jobs output references columns that do not exist: {missing}")
    unused = sorted(column_ids - referenced)
    check("grid-columns-unused", not unused, f"grid columns collected but never used: {unused}")

    # The portal drops empty cells from a row object entirely, and a DropDown's
    # defaultValue is only displayed, never committed. An untouched row would
    # therefore emit missing keys, so every reference needs its own fallback.
    bare = sorted(
        column
        for column in referenced
        if not re.search(r"coalesce\(\s*job\." + column + r"\s*,", jobs_expression)
    )
    check(
        "grid-refs-have-fallbacks",
        not bare,
        f"these grid references are not wrapped in coalesce(): {bare}",
    )

    # A DropDown column's fallback must be one of its own allowed values.
    for column in grid["constraints"]["columns"]:
        element = column["element"]
        if element.get("type") != "Microsoft.Common.DropDown":
            continue
        allowed = {item["value"] for item in element["constraints"]["allowedValues"]}
        match = re.search(r"coalesce\(\s*job\." + column["id"] + r"\s*,\s*'([^']*)'\)", jobs_expression)
        check(
            f"dropdown-fallback-{column['id']}",
            match is not None and match.group(1) in allowed,
            f"fallback {match.group(1) if match else '(none)'} is not one of {sorted(allowed)}",
        )

if failures:
    print("createUiDefinition tests FAILED", file=sys.stderr)
    for failure in failures:
        print(f"  {failure}", file=sys.stderr)
    sys.exit(1)
print("createUiDefinition tests passed")
