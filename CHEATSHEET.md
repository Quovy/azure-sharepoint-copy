# Operator cheat sheet

> **DO NOT COMMIT.** Working notes. Uses `<placeholders>` — substitute your own
> values. Add to `.gitignore` before anyone runs `git add -A`.

Placeholders used below:
`<rg>` deployment resource group · `<job>` logical job name (the `JOB` column of
`copyctl.py list`) · `<res>` Azure resource name (the `AZURE RESOURCE` column,
`<baseName>-<job>`).

---

## 1. What your access lets you do

| Access | You can |
| --- | --- |
| **Contributor on the deployment RG** | `list` `status` `pull` `apply` `start` `set-secret` `enable` `disable` `go-live` `dry-run`, and all env-var tuning below |
| **+ Log Analytics Reader on the workspace** | `preview` (no other command reads the workspace) |
| **+ Owner / User Access Administrator** | `deploy`, template redeploys — both create role assignments |
| **+ write on the *source* storage account** (usually a different RG) | `grant-source` / `revoke-source` |

Contributor alone is enough to iterate on configuration and rclone behaviour.
It is **not** enough to add a job or redeploy.

---

## 2. Everyday sequence

```bash
./copyctl.py list -g <rg>                 # what exists, and in which mode
./copyctl.py pull -g <rg>                 # ALWAYS before editing job files
# edit jobs/<job>.json
./copyctl.py validate
./copyctl.py apply <job> -g <rg>          # pushes config -> job env vars
./copyctl.py preview <job> -g <rg>        # file count + size, uploads nothing
./copyctl.py go-live <job> -g <rg>        # type: LIVE COPY <job>
./copyctl.py start <job> -g <rg>          # one controlled live run
./copyctl.py enable <job> -g <rg>         # activate the schedule
```

`go-live` and `enable` are separate gates. Neither implies the other.

Add a job without disturbing the others (needs Owner/UAA):

```bash
./copyctl.py deploy <newjob> -g <rg>      # type: ADD <newjob>
```

---

## 3. Traps — all verified the hard way

**Pull before you edit.** A fresh clone ships `jobs/default.json` as a
placeholder (`examplestorage`, `contoso.sharepoint.com`). If your deployed job
is also named `default`, `apply` overwrites the real config — with **no
confirmation prompt**, because the placeholder is `dryRun: true`, and `validate`
passes it because placeholders are structurally valid.

**A full redeploy resets every job.** Verified with an identical-parameter
redeploy: the Key Vault secret came back as `secret-not-set` and an active cron
was parked to `0 0 31 2 *`. After *any* template redeploy, for **every** job:

```bash
./copyctl.py set-secret <job> -g <rg>     # have the Entra secrets to hand first
./copyctl.py enable <job> -g <rg>         # only for jobs that were scheduled
```

Record which jobs had active crons **before** redeploying — afterwards
everything reads `parked` and there is no record. `copyctl.py deploy` (PR #6)
avoids all of this; a redeploy does not.

**`params` defaults do not match your deployment.** Defaults are
`baseName=file-copy`, `10.240.0.0/16`, `10.240.0.0/27`. Wrong values silently
build a *second* deployment beside the real one. Recover the originals:

```bash
az deployment group show -g <rg> -n <deployment> --query "properties.parameters"
```

Then always `az deployment group what-if` before `create`. A new VNet or
environment in the what-if output means your parameters are wrong — stop.

**`-g` is needed more often than you'd think.** Discovery fails if *any* job
name is ambiguous across deployments, even when the job you asked for is
unique. One colliding `default` hides every other job in the subscription.

**`start` and `preview` never read local files.** They run whatever is deployed.
Edit → `validate` → `apply` → *then* preview.

**Executions can overlap.** `parallelism: 1` limits replicas *within* an
execution, not concurrent executions. Two starts one second apart both ran.
A long job on a short schedule will pile up on itself.

---

## 4. Runtime tuning with no redeploy ← the big one

rclone reads `RCLONE_*` environment variables for any flag **not already passed
explicitly** on the command line. Container env vars are a plain job write, so
Contributor is enough.

```bash
az containerapp job update -g <rg> -n <res> --set-env-vars RCLONE_MAX_DEPTH=2
./copyctl.py preview <job> -g <rg>                    # measure
az containerapp job update -g <rg> -n <res> --remove-env-vars RCLONE_MAX_DEPTH
```

`--set-env-vars` / `--remove-env-vars` merge safely (22 → 23 → 22 observed,
secretref preserved).

**Several at once** — space-separate the pairs; quote any value containing
spaces or globs. Same for removal (names only):

```bash
az containerapp job update -g <rg> -n <res> \
  --set-env-vars RCLONE_MAX_DEPTH=2 RCLONE_CHECKERS=16 'RCLONE_FILTER=- /big/**'

az containerapp job update -g <rg> -n <res> \
  --remove-env-vars RCLONE_MAX_DEPTH RCLONE_FILTER
```

⚠️ `--set-env-vars` **overwrites** an existing variable rather than erroring, and
`RCLONE_CHECKERS` / `RCLONE_TRANSFERS` are set by the template (8 and 4). Setting
them is fine — just restore the defaults afterwards, since `--remove-env-vars`
would delete them outright rather than reverting them.

Measured against a 735-object share:

| Setting | Objects listed |
| --- | --- |
| baseline | 735 |
| `RCLONE_MAX_DEPTH=1` | 7 |
| `RCLONE_EXCLUDE=/big-subtree/**` | 35 |
| `RCLONE_EXCLUDE=/a/**,/b/**` (comma-separated) | 7 |
| `RCLONE_FILTER=- /a/**,- /b/**` | 7 |

**Composes with `modifiedOnOrAfter`** — both stay active and AND together
(333 files → 8 with a filter → 0 once the date cutoff was tightened).

Worth trying, in order:

```bash
RCLONE_CHECKERS=32                       # walk parallelism; default 8
RCLONE_FILTER='+ /Finance/**,+ /Legal/**,- **'   # include-only; prunes the rest
RCLONE_MAX_DEPTH=<n>                     # cap recursion
RCLONE_NO_TRAVERSE=true                  # skip destination listing — see caveat
```

- Comma-separates for repeatable flags. **A pattern containing a comma breaks it.**
- Do **not** mix `RCLONE_FILTER` with `RCLONE_EXCLUDE`/`RCLONE_INCLUDE`; rclone rejects that.
- Leading `/` anchors to `source:` + `SOURCE_PATH`, not the account root.
- `--no-traverse` swaps one listing per destination directory for **one lookup per
  file**. Only a win when few files qualify. Measure it.

### Cannot be overridden this way

Already passed explicitly, so env vars lose:

```
--create-empty-src-dirs  --checkers  --transfers  --contimeout  --timeout
--retries  --low-level-retries  --onedrive-upload-cutoff --onedrive-chunk-size
--ignore-size  --ignore-checksum  --stats  --use-json-log  --log-level
```

`--create-empty-src-dirs` is the empty-folder problem — it needs PR #5 and a new
image. (`RCLONE_CHECKERS`/`RCLONE_TRANSFERS` *do* work, because `transfer.sh`
interpolates them into the flag values itself.)

### Does not work — don't waste time

- `az containerapp job start --env-vars` — **silently ignored**.
- `az containerapp job start --yaml` — **replaces** the container template
  (22 env vars became 1, dropping the Key Vault secretref).

### These vars are invisible afterwards

`apply` preserves any env var not in the job schema, so a "temporary" tuning
variable becomes permanent and shows up in **neither** `status` nor `pull` nor
the job JSON. Write down anything you set. To see them:

```bash
az containerapp job show -g <rg> -n <res> \
  --query "properties.template.containers[0].env[?starts_with(name,'RCLONE_')]" -o table
```

---

## 5. Diagnosing and stopping

```bash
./copyctl.py preview <job> -g <rg>        # count + size, ~1-2 min, uploads nothing
./copyctl.py status <job>  -g <rg>        # config + last 10 executions
```

Preview refuses on a live job. Switch, preview, switch back:
`dry-run` → `preview` → `go-live`.

Stop a run in progress (no copyctl wrapper; takes the **resource** name):

```bash
./copyctl.py status <job> -g <rg>         # find the execution name
az containerapp job stop -g <rg> -n <res> --job-execution-name <execution>
./copyctl.py disable <job> -g <rg>        # stop future scheduled runs
```

Safe to interrupt — nothing at the destination is ever deleted, and re-running
picks up where it left off.

Runtime error labels in the logs:

| Log line | Means |
| --- | --- |
| `entra_rejected_the_client_id_or_secret` | wrong/expired secret, wrong tenant or client ID |
| `graph_denied_or_failed_the_site_lookup` | no `write` grant on that exact site, or no admin consent |
| `visible_libraries=...` + `document_library_not_found` | library name wrong; the message lists the real ones |
| `transfer_start ...` | all destination checks passed |

Raw stats straight from Log Analytics:

```
ContainerAppConsoleLogs_CL
| where ContainerGroupName_s startswith '<execution>'
| where Log_s contains '"stats"'
| project TimeGenerated, Log_s
```

Log ingestion is sub-second; total preview latency is container cold start.

---

## 6. Needs a new image + redeploy

Anything in `app/transfer.sh` — it is baked into the image (`COPY`, `0555`,
`ENTRYPOINT`) and pinned by digest. No Azure permission edits it.

- **PR #5** — stop creating empty folders under a date cutoff ← the perf fix
- **PR #2** — surface the real Entra/Graph error instead of one generic label

Client-side only, no deployment: **PR #6** (`deploy`), **PR #4** (reject
shifted share names), **PR #3** (gitignore).

---

## 7. Architecture facts worth remembering

- **Azure Files has no change feed and no Event Grid events.** Any date-filtered
  copy must enumerate the whole tree. That is the source's limitation, not
  rclone's — no tool avoids it.
- **Cost scales with directory count, not file count** (one listing per
  directory), until `--no-traverse` flips the destination side to per-file.
- **Graph throttling is per *app* per tenant** — not per site, and not per job.
  Quotas scale with tenant license count, so small tenants throttle sooner.
- `validate` rejects one client ID across two sites, but **allows two client IDs
  on one site** — so giving each job its own Entra app multiplies your
  throttling budget, even within a single tenant and a single site.
- **`includePaths` skips traversal entirely** (`--files-from-raw --no-traverse`).
  If anything upstream can produce a changed-file list — a parallel crawler, or
  an SMB mount plus `find -newermt` — feed it in and the date filter becomes
  unnecessary. This is the real fix if enumeration proves to be the bottleneck.
- Source access is a **per-job managed identity, no credential at all**. The
  SharePoint client secret is the only secret in the system.
- `Storage File Data Privileged Reader` is account-wide and **bypasses NTFS
  ACLs**; ADLS Gen2 instead gets container-scoped `Storage Blob Data Reader`.
- NTFS ACLs are **not** carried into SharePoint — files inherit the destination
  library's permissions.
