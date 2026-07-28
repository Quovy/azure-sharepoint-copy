# Running the copy script by hand in Cloud Shell

`app/transfer.sh` is an ordinary POSIX shell script. Given the right environment
variables it runs anywhere `rclone`, `jq`, and `curl` exist, including Azure
Cloud Shell. That makes it useful for proving a SharePoint setup before you
deploy anything.

**This is a diagnostic tool, not a deployment method.** Read
[the limits](#what-this-cannot-do) before planning around it.

## What this is good for

Confirming, in about two minutes and without creating a single Azure resource,
that:

- the Microsoft Entra application ID and client secret are valid,
- the application really has `Sites.Selected` **and** a grant on your exact
  site, and
- the document library name matches something that exists.

Those three are the most common reasons a first deployment fails, and all three
are checked before the script touches your source storage at all.

## Test the SharePoint destination

Open Azure Cloud Shell in **Bash** mode.

**1. Install rclone.** Cloud Shell does not ship it. Match the pinned version:

```bash
curl -fsSLO https://downloads.rclone.org/v1.74.4/rclone-v1.74.4-linux-amd64.zip
unzip -q rclone-v1.74.4-linux-amd64.zip
export PATH="$PWD/rclone-v1.74.4-linux-amd64:$PATH"
rclone version | head -1
```

**2. Get the script.**

```bash
git clone --branch v0.2.3 --depth 1 \
  https://github.com/Quovy/azure-sharepoint-copy.git
cd azure-sharepoint-copy
```

**3. Set the destination values.** These are identifiers, not secrets:

```bash
export COPY_JOB_NAME=probe
export DEST_TENANT_ID=<your-tenant-guid>
export DEST_CLIENT_ID=<the-application-client-id>
export DEST_SITE_URL=https://<tenant>.sharepoint.com/sites/<SiteName>
export DEST_LIBRARY=Documents
export DEST_PATH=
```

**4. Enter the client secret without putting it in your shell history:**

```bash
read -rs -p "Client secret: " RCLONE_CONFIG_DESTINATION_CLIENT_SECRET
export RCLONE_CONFIG_DESTINATION_CLIENT_SECRET
echo
```

`read -rs` does not echo, and a leading space before the command keeps it out of
history in most shells. Run `unset RCLONE_CONFIG_DESTINATION_CLIENT_SECRET` when
you are done.

**5. Fill in placeholder source values and run in dry-run mode.** The source is
not reached until after the destination checks pass, so placeholders are fine
for this test:

```bash
export SOURCE_TYPE=azure_files
export SOURCE_STORAGE_ACCOUNT=placeholder
export SOURCE_CONTAINER_OR_SHARE=placeholder
export AZURE_MANAGED_IDENTITY_CLIENT_ID=00000000-0000-0000-0000-000000000000
export COPY_EXISTING_FILES=skip
export COPY_DRY_RUN=true

sh app/transfer.sh
```

## Reading the result

| What you see | What it means |
| --- | --- |
| `runtime_error=entra_rejected_the_client_id_or_secret` | Wrong tenant, client ID, or secret — or the secret has expired |
| `runtime_error=graph_denied_or_failed_the_site_lookup...` | The application has no grant on that exact site, or `Sites.Selected` admin consent was never given |
| `visible_libraries=...` then `runtime_error=document_library_not_found` | Site and grant are correct; the library name is wrong. The message lists the libraries the application can actually see — copy one of those |
| `transfer_start ...` appears | **All three destination checks passed.** Everything after this point is source-side and will fail with placeholder values, which is expected |

Reaching `transfer_start` means the SharePoint half of your configuration is
correct and you can deploy with confidence.

## What this cannot do

**It will not copy files.** Getting past `transfer_start` with real source values
requires two things Cloud Shell does not have:

- **A data-plane role.** Reading the source needs `Storage File Data Privileged
  Reader` (Azure Files) or `Storage Blob Data Reader` (ADLS Gen2) on the
  identity running the script. Subscription **Owner does not grant this** — it is
  a control-plane role. In a deployment these are held by per-job managed
  identities, not by people.
- **Network access.** If the source storage account has a firewall, it allows the
  copy service's subnet, not your Cloud Shell session — whose outbound IP also
  changes between sessions.

**ADLS Gen2 sources cannot work outside Azure at all.** That code path requires
an instance metadata endpoint, which Cloud Shell does not provide for this
purpose.

Making a full copy run by hand would mean granting a person an account-wide,
ACL-bypassing read role and opening a production storage firewall to a rotating
IP. Both are things the deployed design exists to avoid. **Use a dry-run
execution of the deployed job instead** — it produces the same file listing with
none of those concessions:

```bash
./copyctl.py start <job>
./copyctl.py status <job>
```
