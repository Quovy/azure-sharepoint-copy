# Azure to SharePoint Copy

Scheduled, one-way copy jobs from Azure Files or ADLS Gen2 into SharePoint
document libraries. Everything runs inside your own Azure subscription and
Microsoft 365 tenant. Nothing is ever deleted at the destination.

## Install

Two supported paths. Both deploy the same template.

**Azure portal.** Open the Deploy to Azure link for this release and fill in the
form. It creates the resource group, the network, and one row per copy job.

**Azure CLI.** For scripted or repeatable installs:

```bash
az group create --name rg-azure-sharepoint-copy --location eastus

# Describe your jobs in jobs/*.json, then render the parameters file.
./copyctl.py validate
./copyctl.py params --image <published-image@sha256:...> --out my-params.json

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

## Changing a job later

Edit `jobs/<name>.json`, then:

```bash
./copyctl.py validate
./copyctl.py apply <name>
```

SharePoint rewrites some uploaded Office files, so the copy uses rclone's
SharePoint compatibility settings and a successful copy is not a byte-for-byte
guarantee. See [SECURITY.md](SECURITY.md).

`apply` updates the deployed job in place. Paths, filters, copy mode, dry-run,
schedule, and timeout all change without redeploying the template or the image.
Applying a configuration never starts a parked schedule.

Redeploy the template only when adding or removing a job, or when changing a
source storage account.

## What gets created

| Resource | Purpose |
| --- | --- |
| Container Apps environment + VNet | Runs the jobs on a dedicated subnet |
| One Container Apps job per copy job | The schedule, the configuration, the identity |
| One user-assigned managed identity per job | Read-only access to that job's source |
| Key Vault | Holds one Entra client secret per job |
| Log Analytics workspace | 90 days of execution logs |

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
