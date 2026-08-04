# Deployment checklist

Work top to bottom. Nothing copies a file until step 5.

## 1. Before you start

- [ ] Choose the Azure subscription and region for the copy service.
- [ ] Record each source: subscription, resource group, storage account, and
      the file share or ADLS Gen2 container.
- [ ] For each SharePoint site you will copy into, create one single-tenant
      Microsoft Entra application.
- [ ] Give each application Microsoft Graph **application** permission
      `Sites.Selected` and grant admin consent.
- [ ] Grant each application `write` access to only its own SharePoint site.
- [ ] Record each application's Client ID and the exact site URL.
- [ ] Create one client secret per application. Keep each **Value** in your
      approved secret manager. Never put a secret in a JSON file or a form.

Two jobs may share one application if they target the same site. Two different
sites always need two applications.

You can verify each application, its site grant, and the library name before
deploying anything: see [docs/CLOUD-SHELL-TEST.md](docs/CLOUD-SHELL-TEST.md).

## 2. Deploy

Pick one path.

### Portal

- [ ] Open the Deploy to Azure link for this release.
- [ ] On **Basics**, choose the subscription, resource group, and region.
- [ ] On **Source**, select the storage account holding the files to copy.
- [ ] On **Copy jobs**, add one row per route. Each row needs a job name, the
      share or container, the SharePoint site URL, the document library, and
      that site's Entra Client ID.
- [ ] On **Copy behavior**, choose what happens when a file already exists.
- [ ] On **Network**, keep the defaults unless 10.240.x.x is already in use.
- [ ] On **Advanced**, keep the published image unless you imported it into
      your own registry. If you did, also select that registry so the jobs are
      authorized to pull from it.
- [ ] Review and create.

### Azure CLI

- [ ] Get the package at a fixed release tag, so what you deploy cannot change
      underneath you:

  ```bash
  git clone --branch v0.2.4 --depth 1 \
    https://github.com/Quovy/azure-sharepoint-copy.git
  cd azure-sharepoint-copy
  ```

- [ ] Edit `jobs/default.json` beside its commented reference. Copy the file to
      add more routes; the file name must match the `name` inside it:

  ```bash
  code jobs/default.json jobs/example.jsonc
  cp jobs/default.json jobs/invoices.json
  code jobs/invoices.json
  ```

- [ ] Keep every `dryRun` set to `true`, then validate and render parameters:

  ```bash
  ./copyctl.py validate
  ./copyctl.py params --out my-params.json
  ```

- [ ] Create the resource group and preview the deployment:

  ```bash
  az group create --name rg-azure-sharepoint-copy --location <region>
  az deployment group what-if \
    --resource-group rg-azure-sharepoint-copy \
    --template-file infra/main.bicep \
    --parameters @my-params.json
  ```

- [ ] Read the `what-if` output. Confirm the only resources marked for change
      outside the new resource group are role assignments on your source.
- [ ] Deploy:

  ```bash
  az deployment group create \
    --resource-group rg-azure-sharepoint-copy \
    --template-file infra/main.bicep \
    --parameters @my-params.json
  ```

## 3. Store credentials

- [ ] Confirm what was deployed:

  ```bash
  ./copyctl.py list
  ```

  Every job should show `dry run` and `parked`.

- [ ] For each job, paste that application's client-secret **Value** at the
      hidden prompt:

  ```bash
  ./copyctl.py set-secret default
  ```

- [ ] If the source storage account has a firewall, give the copy subnet access:

  ```bash
  ./copyctl.py grant-source default
  ```

  Skip this if the account's firewall is set to allow all networks.

## 4. Review a dry run

- [ ] Run one execution and check it:

  ```bash
  ./copyctl.py start default
  ./copyctl.py status default
  ```

- [ ] Open that execution's logs in the Container Apps job in the Azure portal.
      Confirm the file list matches what you expect to copy.
- [ ] Repeat for every job before continuing.

A dry run also proves the credential, the site grant, and the document library
name are all correct. If any is wrong, the execution fails with a specific
reason rather than copying anything.

## 5. Go live

- [ ] Switch one job to live and confirm at the prompt:

  ```bash
  ./copyctl.py go-live default
  ```

- [ ] Run one controlled live execution and verify the destination in
      SharePoint. Open a few representative Office documents rather than
      comparing sizes or hashes: SharePoint rewrites those formats on upload,
      so functional review is the appropriate check.

  ```bash
  ./copyctl.py start default
  ./copyctl.py status default
  ```

- [ ] Activate the schedule only after that execution looks right:

  ```bash
  ./copyctl.py enable default
  ./copyctl.py status default
  ```

## Maintenance

- Change paths, filters, copy mode, schedule, or timeout: edit
  `jobs/<name>.json`, then `./copyctl.py validate` and `./copyctl.py apply <name>`.
- Rotate a credential: `./copyctl.py set-secret <name>`, then remove the old
  credential from the Entra application registration.
- Pause a job: `./copyctl.py disable <name>`. Resume: `./copyctl.py enable <name>`.
- Return a job to dry run: `./copyctl.py dry-run <name>`.
- Rebuild local job files on a new machine: `./copyctl.py pull`.
- Add a job: write `jobs/<name>.json`, then `./copyctl.py validate` and
  `./copyctl.py deploy <name>`. It creates that job alone and leaves every
  deployed job, schedule, and stored credential as it is. The new job arrives in
  dry-run mode with its schedule parked, so repeat steps 3 to 5 for it. Do not
  redeploy the whole template to add a job: that parks every schedule and
  returns every Key Vault secret to its placeholder.
- Remove a job, or change a source storage account: redeploy the template.

## Removal

- [ ] Remove the copy subnet from each source storage account first:

  ```bash
  ./copyctl.py revoke-source default
  ```

- [ ] Delete the resource group:

  ```bash
  az group delete --name rg-azure-sharepoint-copy
  ```

- [ ] Role assignments on your source storage account are deleted automatically
      when the identities they point at are removed. Confirm none remain:

  ```bash
  az role assignment list --scope <source-storage-account-id> --output table
  ```

- [ ] The Key Vault stays recoverable for 7 days. To reuse the same name sooner:

  ```bash
  az keyvault purge --name <vault-name> --location <region>
  ```
