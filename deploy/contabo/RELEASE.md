# SCM pilot release preparation - published image, not a deployment

API source: 167154600b58f62e536fc1cf7b0764713ab0860d
Recipe: f9e632e274879256a007c39827a2f34f2b264e2c
Workflow: 64b02eab6c179b4b2ab32acd6e04c69317fc5522
Successful run: https://github.com/estebanSulcaInfante/envaperu-workflow/actions/runs/36914490178

The public image was independently pulled locally with an empty Docker auth config.
All 11 saved layers and all 239 runtime file hashes passed the reviewed allowlist.
A disposable container with network=none, read-only filesystem, no capabilities,
no secrets and UID 10001 loaded the required Book2.xlsx worksheet successfully.
The startup guard rejected absent configuration before application startup.
No application endpoint, database, Supabase Auth or S3 connection was exercised.
This is not a vulnerability scan or migration/restore acceptance test.

release-image.json is NEW local verification evidence, not the original CI artifact.
Its local_archive_sha256 refers to the local docker-save archive, not the CI archive.
Image registry digest, source label, non-root user and platform were verified locally.
The successful public job metadata also confirms the original build/publish gates.

compose.release.yaml pins the published API and official PostgreSQL 16.15 Bookworm
linux/amd64 manifest. PostgreSQL major 16 matches the reviewed postgres-smoke CI.
The source database version, extensions, role/policy inventory and restore compatibility
MUST still be checked before initializing/migrating the target; the pin is preparation,
not a claim that a restore from the current source has been validated.

## Shortest Dokploy step (save configuration only)

Use EXISTING project envaperu-scm-pilot / Compose scm-pilot.
Provider: generic Git, repository https://github.com/estebanSulcaInfante/envaperu-workflow.git
Branch: release/scm-pilot
Compose Path: ./deploy/contabo/compose.release.yaml
SSH key: None. Submodules: off. AutoDeploy: OFF.
Save only; do NOT deploy/start. No new project, GitHub App, token or webhook is needed.
Verify the saved branch/path and that AutoDeploy remains OFF. Before a future start,
review Dokploy's rendered configuration: database only on its internal network,
no DB ports/proxy network, dedicated volume and expected API/DB resource limits.
Odoo, Render's configured branch, dashboard and weighing stations are unchanged.

## One grouped pre-deployment decision/handoff

1. Database roles and source compatibility:
   Inventory source version/extensions, schema/policies and grants first. Proposed
   separate roles: scm_owner (NOLOGIN), scm_migrator for controlled migrations,
   scm_api for runtime and a scoped backup role. Runtime must be NOSUPERUSER,
   NOBYPASSRLS and not own application tables; required FORCE RLS/policies/grants
   must be reviewed and tested before use. Bootstrap postgres is administration only.
   Creating roles/passwords, initialization and restoration are not done by this release.
2. Existing Supabase Auth and S3:
   Keep the same Auth project, issuer/audience and existing object bucket. An administrator
   supplies DATABASE_URL for scm_api and existing Auth/S3 configuration through a secure
   persistent API env file; do not paste secrets into chat or commit them. The separate
   PostgreSQL bootstrap password is provided as a secret file. Required path variables
   SCM_API_ENV_FILE and SCM_PG_PASSWORD_FILE have no defaults and fail closed when absent.
   No new credentials or bucket permissions were created. S3 backup remains deferred
   per the prior decision; existing images are not deleted or moved by this package.
3. Database backup and restore acceptance:
   Identify the existing external backup destination, encryption/key custodian and
   responsible operator. Proposed retention: 7 daily + 4 weekly; proposed RPO 24h /
   RTO 4h require acceptance and a measured isolated restore. No external account,
   purchase, recurring job, dump or restore was created. Contabo snapshots alone do
   not establish this application's restoration guarantees.

After these gates, a separate authorized isolated trial must check authentication,
API authorization/RLS, S3 reads, database integrity, memory/latency and recovery before
any cutover. This branch does not authorize or automate any of those operations.
