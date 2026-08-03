# Azure to SharePoint Copy

Scheduled, one-way copy jobs from Azure Files or ADLS Gen2 into SharePoint
document libraries. Everything runs inside your own Azure subscription and
Microsoft 365 tenant. Nothing is ever deleted at the destination.

[![Deploy to Azure](https://aka.ms/deploytoazurebutton)](https://portal.azure.com/#create/Microsoft.Template/uri/https%3A%2F%2Fraw.githubusercontent.com%2FQuovy%2Fazure-sharepoint-copy%2Fv0.2.4%2Finfra%2Fmain.json/createUIDefinitionUri/https%3A%2F%2Fraw.githubusercontent.com%2FQuovy%2Fazure-sharepoint-copy%2Fv0.2.4%2Finfra%2FcreateUiDefinition.json)

## Install

Two supported paths. Both deploy the same template.

**Azure portal.** Click the button above and fill in the form. It creates the
network, the jobs, and one row per copy route. The button is pinned to a
release tag, so what you deploy does not change when this repository does.

**Azure CLI.** For scripted or repeatable installs:

```bash
az group create --name rg-azure-sharepoint-copy --location eastus

# Describe your jobs in jobs/*.json, then render the parameters file.
./copyctl.py validate
./copyctl.py params --out my-params.json      # uses this release's pinned image

az deployment group what-if \
  --resource-group rg-azure-sharepoint-copy \
  --template-file infra/main.bicep \
  --parameters @my-params.json

az deployment group create \
  --resource-group rg-azure-sharepoint-copy \
  --template-file infra/main.bicep \
  --parameters @my-params.json
```

`what-if` prints exactly what will change before anything is created. Run it.

Deployment creates every job **in dry-run mode with its schedule parked**, so no
file moves until you explicitly activate it.

## After deploying

```bash
./copyctl.py list                     # what is deployed, and in which mode
./copyctl.py set-secret default       # hidden prompt for the Entra client secret
./copyctl.py grant-source default     # only if the source has a storage firewall
./copyctl.py start default            # run one dry-run execution now
./copyctl.py preview default          # how many files a dry run would copy
./copyctl.py status default           # mode, schedule, and recent executions
./copyctl.py go-live default          # dry run -> live, requires typed confirmation
./copyctl.py enable default           # activate the schedule
```

`copyctl.py` keeps no local state. Every command finds what is deployed by
querying Azure for resources tagged `workload=azure-sharepoint-copy`, so any
administrator with the right role can run it from any Cloud Shell or
workstation. `./copyctl.py pull` writes `jobs/*.json` back out from what is
actually deployed.

Follow [CHECKLIST.md](CHECKLIST.md) for the full first-run sequence, and read
[SECURITY.md](SECURITY.md) before deploying.

To check an Entra application, its site grant, and a library name before
deploying anything, see
[docs/CLOUD-SHELL-TEST.md](docs/CLOUD-SHELL-TEST.md).

## Changing a job later

Edit `jobs/<name>.json`, then:

```bash
./copyctl.py validate
./copyctl.py apply <name>
```

Or read and change single values from the command line, without opening an
editor:

```bash
./copyctl.py get-config <name>                    # every field and its value
./copyctl.py get-config <name> copy.scheduleUtc   # just one

./copyctl.py set-config <name> copy.scheduleUtc "30 4 * * 1-5"
./copyctl.py set-config <name> source.path Reports/2026
./copyctl.py set-config <name> copy.timeoutMinutes 45
./copyctl.py set-config <name> source.includePaths '["Invoices/2026", "Reports"]'
./copyctl.py apply <name>
```

Both take the field as `section.field`. `get-config` prints a value as the text
`set-config` accepts back, so a value can be read, edited, and put back without
quoting games. `set-config` reads the value as the type that field already holds
— `dryRun false` writes a boolean, not the string `"false"` — and validates the
whole file before writing it, so a rejected value leaves the job file exactly as
it was.

Both work only on the local file; `apply` is still what publishes it to Azure.

SharePoint rewrites some uploaded Office files, so the copy uses rclone's
SharePoint compatibility settings and a successful copy is not a byte-for-byte
guarantee. See [SECURITY.md](SECURITY.md).

`apply` updates the deployed job in place. Paths, filters, copy mode, dry-run,
schedule, and timeout all change without redeploying the template or the image.
Applying a configuration never starts a parked schedule.

Redeploy the template only when adding or removing a job, or when changing a
source storage account.

## What gets created

Everything below is created in the resource group you deploy into, tagged
`workload=azure-sharepoint-copy`. Names derive from the `baseName` parameter,
which defaults to `file-copy`.

| Resource | Azure type | Name | Details |
| --- | --- | --- | --- |
| Virtual network | `Microsoft.Network/virtualNetworks` | `<baseName>-vnet` | `10.240.0.0/16` by default (`vnetAddressPrefix`) |
| Subnet | (inside that VNet) | `container-apps` | `10.240.0.0/27` by default (`containerAppsSubnetPrefix`), delegated to `Microsoft.App/environments`, with a `Microsoft.Storage.Global` service endpoint |
| Container Apps environment | `Microsoft.App/managedEnvironments` | `<baseName>-environment` | Consumption workload profile, not zone redundant, injected into the subnet above, logs to the workspace below |
| Log Analytics workspace | `Microsoft.OperationalInsights/workspaces` | `<baseName>-logs` | `PerGB2018`, 90 day retention |
| Key Vault | `Microsoft.KeyVault/vaults` | `<baseName>-<hash>` (24 chars) | Standard, RBAC authorization, soft delete with 7 day retention, no purge protection, public network access enabled |
| Copy job — one per job | `Microsoft.App/jobs` | `<baseName>-<job>` | Schedule trigger, parallelism 1, retry limit 1, replica timeout from `timeoutMinutes` |
| Managed identity — one per job | `Microsoft.ManagedIdentity/userAssignedIdentities` | `<baseName>-<job>-identity` | Assigned to that job only |
| Key Vault secret — one per job | `Microsoft.KeyVault/vaults/secrets` | `sharepoint-<job>` | Created holding the placeholder `secret-not-set`; `copyctl.py set-secret` writes the real Entra client secret, which never passes through the template or its deployment history |

Every job is created with the cron expression `0 0 31 2 *` — a date that never
occurs — so the schedule is deployed but parked until `copyctl.py enable`
installs the real one.

Role assignments, some of which land outside the deployment resource group:

| Role | Scope | Assigned to |
| --- | --- | --- |
| Key Vault Secrets User | That job's secret, not the whole vault | Each job's identity |
| Storage File Data Privileged Reader | The whole source storage account | Each `azure_files` job's identity |
| Storage Blob Data Reader | The single source container | Each `adls_gen2` job's identity |
| AcrPull | The registry named by `containerRegistryResourceId` | Every job identity, only when a private registry is configured |

Azure Files OAuth-over-REST uses backup semantics, so its role is read-only but
account-wide and bypasses per-file NTFS ACLs. Use a source-dedicated storage
account if that is too broad. See [SECURITY.md](SECURITY.md).

There is no container registry and no build step. The image is published ahead
of time and pinned by digest.

If your organization blocks public registries, import that exact digest into
your own registry and pass its resource ID as `containerRegistryResourceId`
(the portal form has a picker for it). Container Apps resolves the image while
creating each job, so a private registry will not work without it.

## Requirements

- An Azure subscription, and permission to create the resources above.
- One single-tenant Microsoft Entra application per SharePoint site, with
  Microsoft Graph `Sites.Selected` granted admin consent, plus a `write` grant on
  that specific site.
- The source storage account must allow public network access. Accounts
  reachable only through an existing private endpoint are not supported.
- `copyctl.py preview` additionally needs the `log-analytics` Azure CLI
  extension (`az extension add --name log-analytics`) and read access to the
  deployment's Log Analytics workspace. No other command reads that workspace,
  so an operator who can run everything else may still need this granted.
