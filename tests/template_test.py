#!/usr/bin/env python3
"""Structural checks on the compiled ARM template. No Azure access required.

Bicep compiles some templates that ARM then rejects at submission. The checks
here cover the cases that have actually bitten this package.
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = json.loads((ROOT / "infra" / "main.json").read_text())

failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")


def resource_name_expr(resource):
    return json.dumps(resource.get("name", ""))


# A dependsOn entry naming a resource that may not exist makes ARM reject the
# whole template with InvalidTemplate, on exactly the path where the condition
# is false. Bicep flattens a ternary in dependsOn, so it cannot be expressed
# conditionally: a depended-on resource must always be deployed.
conditional = [r for r in TEMPLATE["resources"] if "condition" in r]
for resource in TEMPLATE["resources"]:
    depends = resource.get("dependsOn") or []
    if not isinstance(depends, list):
        continue
    for entry in depends:
        for target in conditional:
            name = target.get("name", "")
            fragment = re.sub(r"^\[|\]$", "", name) if isinstance(name, str) else ""
            if fragment and fragment in str(entry):
                failures.append(
                    f"conditional-dependson: {resource.get('type')} depends on "
                    f"'{name}', which is conditional and may not exist"
                )

check(
    "no-conditional-top-level-resources",
    not conditional,
    f"{len(conditional)} conditional resource(s); anything depending on them must not",
)

# The job must not be created before its AcrPull grant, or the image pull is
# unauthorized. Container Apps resolves the image at job-creation time.
jobs = [r for r in TEMPLATE["resources"] if r.get("type") == "Microsoft.App/jobs"]
check("job-resource-present", len(jobs) == 1, f"expected one job loop, found {len(jobs)}")
if jobs:
    depends = " ".join(str(d) for d in jobs[0].get("dependsOn", []))
    check(
        "job-depends-on-registry-access",
        "registry-access" in depends,
        "the job must depend on the registry access module",
    )
    check(
        "job-depends-on-source-access",
        "source-access" in depends,
        "the job must depend on the source access module",
    )

# Every parameter the portal form emits must exist here.
ui = json.loads((ROOT / "infra" / "createUiDefinition.json").read_text())
for name in ui["parameters"]["outputs"]:
    if name == "location":
        continue
    check(
        f"param-exists-{name}",
        name in TEMPLATE["parameters"],
        "the portal emits it but the template does not declare it",
    )

# The image must be digest-pinned wherever it is defaulted.
for source, value in (
    ("main.parameters.json", json.loads((ROOT / "infra" / "main.parameters.json").read_text())
     ["parameters"]["containerImage"]["value"]),
):
    check(
        f"image-digest-pinned-{source}",
        "@sha256:" in value,
        f"{value} is not digest-pinned",
    )

if failures:
    print("template tests FAILED", file=sys.stderr)
    for failure in failures:
        print(f"  {failure}", file=sys.stderr)
    sys.exit(1)
print("template tests passed")
