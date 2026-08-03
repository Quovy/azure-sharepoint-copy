# Security model

## Every write this package makes to your tenant

This is the complete list. Nothing else is created, changed, or deleted.

| When | Operation | Target | Performed by |
| --- | --- | --- | --- |
| Deployment | Create Container Apps environment, VNet + subnet, Log Analytics workspace, Key Vault | The resource group you chose | ARM template |
| Deployment | Create one Container Apps job and one user-assigned managed identity per copy job | The resource group you chose | ARM template |
| Deployment | Create one placeholder Key Vault secret per copy job | The new Key Vault | ARM template |
| Deployment | Assign `Key Vault Secrets User` on one secret | The new Key Vault | ARM template |
| Deployment | Assign a read-only role on your source | **Your existing source storage account** | ARM template |
| Deployment | Assign `AcrPull`, only when `containerRegistryResourceId` is set | **Your existing container registry** | ARM template |
| `set-secret` | `PUT` one secret value | The new Key Vault | copyctl.py |
| `grant-source` | Add the copy subnet to `networkAcls.virtualNetworkRules` | **Your existing source storage account** | copyctl.py |
| `apply`, `go-live`, `dry-run`, `enable`, `disable` | Update job environment variables, cron expression, or replica timeout | The Container Apps job | copyctl.py |
| `start` | Start one job execution | The Container Apps job | copyctl.py |
| `revoke-source` | Remove the copy subnet from `networkAcls.virtualNetworkRules` | **Your existing source storage account** | copyctl.py |

Only four rows touch resources you already own: three on the source storage
account, and one optional `AcrPull` grant if you chose to host the image in your
own registry. The two network-rule changes are separate, explicit commands
rather than a side effect of installing: nothing modifies your existing storage
account's firewall unless you run `grant-source`. Neither command changes the
account's default firewall action, and neither adds an allow-list entry for a
laptop or Cloud Shell IP.

## Data flow and trust boundaries

Each job makes outbound connections to Azure Storage, Microsoft Entra, Microsoft
Graph/SharePoint, the container registry holding the published image, Key Vault,
and Azure Monitor. It has no inbound endpoint and no external control plane.

The source storage account must have public network access enabled so its
firewall and service endpoint can be used. Accounts reachable only through an
existing private endpoint are outside this package's network model.

## Identities and permissions

Each copy job gets its own user-assigned managed identity, holding exactly two
roles:

- `Key Vault Secrets User`, scoped to that job's single secret.
- A read-only role on that job's source.

ADLS Gen2 sources use `Storage Blob Data Reader` scoped to the single container.

Azure Files sources use `Storage File Data Privileged Reader` at storage-account
scope. The role is read-only, but Azure Files OAuth backup semantics bypass
per-file and per-directory ACLs, and Azure offers no narrower scope for this
access path. Use a source-dedicated storage account if account-wide read is too
broad.

No identity in this package has any data-write role in Azure. Writing happens
only into SharePoint.

## SharePoint access

Each SharePoint application must have Microsoft Graph `Sites.Selected` with
admin consent, then a `write` grant on one specific site. `Sites.Selected` grants
nothing on its own, so an application with no site grant can read nothing.

One application may serve several jobs that target the **same** site. Using one
application for two different sites is rejected by `copyctl.py validate`, because
it would widen each job's reach beyond what it needs.

At run time the job resolves the site and document library through Graph and
copies only into the resolved library and folder. Resolution happens on every
execution rather than being pinned at deployment, so a renamed or replaced
library fails loudly instead of copying into the wrong place.

## Credentials

Job configuration files contain identifiers only. They must never contain the
Entra client secret, storage keys, SAS tokens, or passwords. `copyctl.py
validate` rejects any unknown field, which includes anything named like a
secret.

`copyctl.py set-secret` reads the secret without terminal echo and sends it
straight to Azure Resource Manager over TLS. The value is never written to disk,
never placed on a command line where another local process could read it via
`/proc`, and never passed as a deployment parameter, so it does not appear in
deployment history. The job reads it from Key Vault through its managed identity.

The deployment template creates a non-credential placeholder secret so the job's
secret reference resolves before you store the real value. A job that runs
against the placeholder fails authentication; it does not run unauthenticated.

Do not enable shell tracing while running `copyctl.py`. Rotate with
`copyctl.py set-secret` before the Entra credential expires, then remove the old
credential from the application registration.

## Copy behavior

The runtime wrapper contains no destination-delete operation and never calls
`rclone sync`. `scripts/validate.sh` fails the build if one appears.

- `existingFiles: skip` adds `--ignore-existing`.
- `existingFiles: replace_if_changed` lets `rclone copy` replace a changed file.
- `dryRun: true` adds `--dry-run`.
- `includePaths` becomes `--files-from-raw`; unsafe relative paths are rejected.
- `modifiedOnOrAfter` becomes `--max-age`, based on last-modified time.
- `--ignore-size --ignore-checksum` are always applied. These are rclone's
  documented SharePoint compatibility settings: SharePoint rewrites some
  uploaded files, mainly Microsoft Office formats, so destination size and hash
  legitimately differ from the source. Without them every such file would look
  changed and be re-uploaded on every run. Comparison falls back to
  modification time, and **a successful copy is therefore not a byte-for-byte
  or file-size attestation.**

Every job deploys in dry-run mode with its schedule parked. Going live requires
typing `LIVE COPY <job>` at a prompt, and activating a schedule is a separate
command again.

## Supply chain

The application image is built and published once, then deployed by SHA-256
digest. Customers do not build it, so no deployment depends on package
repositories or download endpoints being reachable or unchanged.

- The Dockerfile frontend and Alpine base are pinned by digest.
- rclone 1.74.4 is pinned, and its archive checksum is verified at build time.
- The runtime is non-root and contains only Alpine, CA certificates, rclone,
  jq, curl, and the constrained wrapper.

If your organization does not permit pulling from public registries, import the
same digest into your own registry with `az acr import`, then pass that
reference as `containerImage` **and** the registry's resource ID as
`containerRegistryResourceId`. The second parameter grants each job identity
`AcrPull` on that registry and adds the matching `registries` entry; without it
job creation fails with an unauthorized pull, because Container Apps resolves
the image at create time rather than at first execution. The bytes are
identical; the digest proves it.

## Audit

rclone emits JSON logs including per-file copy decisions and transfer
statistics. Container Apps sends stdout and stderr to a Log Analytics workspace
with 90-day retention.

`copyctl.py preview` starts one dry-run execution and reads that execution's
statistics back out of the workspace, so it needs read access there. No other
command reads it. Preview refuses to run against a job in live mode rather than
switching the mode itself: Container Apps has no per-execution override that
preserves the job's Key Vault secret reference, so forcing dry run for a single
execution cannot be done safely. Switch the job with `dry-run`, preview, then
`go-live`.

Because the entire job configuration lives in the Container Apps job's
environment variables, `az containerapp job show` and the Azure portal are a
complete, current record of what a job will copy and where. Configuration changes
are ordinary ARM operations and appear in the Azure Activity Log alongside
deployments, role assignments, network-rule changes, and manual starts.

Log Analytics alone is not a tamper-evident archive. Apply your own Azure
Monitor/SIEM retention and access controls where logs are compliance evidence.

## Known limitations

- A VNet-integrated workload-profile environment adds Azure-managed networking
  resources and their associated cost.
- Key Vault public network access remains enabled and authenticated.
- `copyctl.py validate` rejects a schedule whose interval is shorter than
  `timeoutMinutes` for the common cron shapes, but unusual expressions are not
  modelled and could still overlap.
- SharePoint path length and character restrictions still apply, and file names
  are normalized by SharePoint.
- SharePoint may rewrite uploaded Office files, so this package cannot
  guarantee that destination bytes or size match the source even when the
  document is functionally equivalent. Verify those formats by opening them,
  not by comparing hashes.
- Azure Files NTFS ACLs are not preserved in SharePoint.
- `modifiedOnOrAfter` uses modification time; portable creation-time filtering
  is not available across both supported source types.
- The portal form supports one source storage account in the deployment
  subscription. Multiple source accounts, or a source in another subscription,
  require the CLI path.
