# SCM Contabo: reviewed image preparation

Status: local preparation; independent review required before pushing or publishing.
The user authorized a PUBLIC GHCR image containing code, dependencies and necessary
runtime templates. No VPS deployment, credentials, Odoo changes or data migration
are part of this package.

## Source and trusted CI

Application source is fixed to da72a177d7d1e2d130819c865348d0d8d360e291.
check_ci.py accepts only GitHub Actions run 38077365117, repository
estebanSulcaInfante/envaperu-workflow, branch codex/warehouse-qr-tv-release-20261010,
push event, .github/workflows/tests.yml, exact source SHA, completed and success.
It does not accept a green run from an arbitrary branch or require main: main is
older than this source. The existing source CI includes fast-suite/postgres-smoke.

The workflow checks out the deployment recipe at a literal commit SHA, separately
from the source. Recipe files are committed first; a following workflow-only commit
pins that reviewed recipe. Both commits require review. Actions are pinned to SHAs.

## Runtime files and templates

runtime-allowlist.json enumerates 256 approved files with SHA-256 hashes.
build_context.py creates a NEW context from these bytes; the source checkout is
never sent to Docker. Unexpected files in the checkout do not enter the context.
Every approved byte must match the fixed source; symlinks/escaping paths fail.

Book2.xlsx is retained byte-for-byte: app/services/excel_service.py:TEMPLATE_PATH
loads it when generating orders, using worksheet IMPRIMIR OP. The owner confirmed
that necessary templates must remain. An offline scan of its XML/relationships
found no private-key, GitHub-token, AWS-access-key or email markers; this bounded
scan is not a general guarantee about all personal data.

Eight other binary/CSV resources remain UNCHANGED in the source repository:

- Book1.xlsx: input of scratch_update_template.py, which generates Book2.
- OP1322-BALDE ROMANO.xlsm: offline analysis/debug scripts.
- SKU PIEZAS 2025.xlsx and SKU PRODUCTOS TERMINADOS 2025.xlsx: import/analysis scripts.
- image.png: OCR test fixture.
- Book3.xlsx, REPORTE NOVIEMBRE25 CERRADO.xlsm and SKU PRODUCTOS PART 1.csv:
  no Python runtime reference found in the reviewed source.

These offline resources are explicitly listed as excluded from the API image,
not deleted or sanitized. If a future runtime path needs one, review its content
and amend the allowlist; do not broadly copy app/templates.

verify_image_layers.py reads EVERY docker-save layer, including bytes hidden by
later layers. All /app files must match the allowlist, and excluded resource
basenames are rejected anywhere. Base OS/Python and installed dependencies outside
/app are permitted by the reviewed Dockerfile and immutable Python base. This is
not a dependency vulnerability scanner. No application secret is supplied to build.

## Build and publication boundary

The manual workflow runs only on codex/scm-contabo-image in the expected repository.
Its build job has contents:read and actions:read, with no packages:write. It validates
CI, builds linux/amd64 outside the VPS, imports dependencies and opens Book2 without
network or credentials, then checks every saved image layer. It uploads the exact
image archive and records its SHA-256 and image ID as job outputs.

Only the separate publish job has packages:write, and only runs when publish=true.
It requires environment scm-pilot-image-public. check_publish_gate.py fails unless
that environment has a required reviewer and exactly one allowed deployment branch,
codex/scm-contabo-image. The build also checks this gate when publication is requested,
before the privileged job. No script creates or weakens environment protections.
The publish job downloads the artifact by its exact ID from this run, verifies the
archive hash and all layers again, loads the tested image and checks its image ID.
It never builds or executes application code. GHCR login uses the ephemeral workflow
token. Tags include source SHA, run ID and attempt; release-image.json records the
registry digest, source SHA, pinned recipe SHA, base and run. There is no deploy step.

Execution prerequisites still needing coordinator resolution:

1. Independent review of the new local recipe and workflow commits before any push.
2. A protected scm-pilot-image-public environment with the reviewer/branch policy
   above. It is not created by this preparation; unavailable API metadata fails closed.
3. workflow_dispatch requires the workflow on the default branch. main is old:
   do not merge the optimized application into main or change default branches just
   to enable dispatch. Coordinate a reviewed workflow-only bootstrap before execution.
4. Supply a reviewed immutable python:3.12-slim@sha256 digest. No fabricated digest.
5. A supported authenticated GitHub execution path. No PAT or credentials are created.
6. GHCR visibility is separate from repository visibility. The new image is authorized
   public, but setting/verifying package visibility and an anonymous digest pull remain
   necessary after publication; this workflow does not silently change visibility.

## Later deployment, outside current publication scope

The existing Dokploy scm-pilot service remains idle. Future generic Git configuration:
https://github.com/estebanSulcaInfante/envaperu-workflow.git, release/scm-pilot,
./deploy/contabo/compose.release.yaml, no SSH key/submodules, AutoDeploy OFF.
That release branch/manifest must be created only after image digest review; no
GitHub App, webhook or new Dokploy API key is required for public HTTPS Git cloning.

render_release.py resolves the API digest plus an approved immutable PostgreSQL
15-17 reference into a new compose.release.yaml; it rejects unpinned references,
PG18's different layout, inconsistent source and overwrites. Secret-file placeholders
remain mandatory; no credentials are included or generated.

Compose budgets: API 1 CPU/1 GiB; DB 1 CPU/1.5 GiB, own volume, internal database
network and no published DB port. Supabase Auth/S3 stay external; dashboard and
physical weighing station stay where they are. Review Dokploy's rendered networks
before a future start so PostgreSQL is not attached to proxy/ingress networks.
DB roles/FORCE RLS, secure credential handoff, external backups and tested restoration
are separate gates before migration or cutover. Odoo is untouched.

## Local validation

Run: python deploy/contabo/test_preparation.py
Tests cover exact CI identity, context hashes, excluded files/hidden layer content,
publication environment policy, runtime template inclusion and digest rendering.
Also validate the real source context, YAML, bash syntax, Compose configuration and
git diff --check. Image build/smoke/layer verification are mandatory CI steps, not
claimed as completed locally. No workflow, image push or VPS deployment is implied
by these static tests.
