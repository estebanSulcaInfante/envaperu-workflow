# SCM Contabo: reviewed API + bridge + native OpenClaw image preparation

Status: local preparation; independent review required before pushing or publishing.
The user authorized PUBLIC GHCR images containing the API, personal-agent bridge
and official OpenClaw Control UI overlay, with code, dependencies and necessary
runtime templates. No VPS deployment,
credentials, Odoo changes or data migration are part of this package.

The exact application source is `1ec2da4cc0e626debf4a1ee94ba6ecabab0adbab` and
the candidate CI gate is run `41682781955` on branch
`codex/personal-agent-simple-20261030`. The API context is independently hashed
by `runtime-allowlist.json`; the bridge context is independently hashed by
`bridge-runtime-allowlist.json` and includes only its pinned Dockerfile,
package manifests and `src/**`. Tests, examples, `.env` files, fixtures and
`node_modules` are never copied into either context.

All three images use the existing public GHCR package
`ghcr.io/estebansulcainfante/envaperu-scm-api`, with distinct immutable tags
`sha-<source>-<run>-<attempt>` and
`bridge-sha-<source>-<run>-<attempt>` and
`native-sha-<source>-<run>-<attempt>`. The manual workflow builds, smoke-checks
and layer-verifies all three images in the unprivileged job, uploads `api.tar`,
`bridge.tar`, `native.tar` and the unpublished `openclaw-base.tar` evidence
together, then has one `scm-pilot-image-public`
protected publish job re-checks all archives, pushes all tags, and pulls all digests
with an empty anonymous Docker config before writing evidence. A private or
unpullable package therefore cannot produce `release-image.json`.

## Source and trusted CI

Application source is fixed to 1ec2da4cc0e626debf4a1ee94ba6ecabab0adbab.
check_ci.py accepts only GitHub Actions run 41682781955, repository
estebanSulcaInfante/envaperu-workflow, branch codex/personal-agent-simple-20261030,
push event, .github/workflows/tests.yml, exact source SHA, completed and success.
The coordinator verified this exact run completed successfully with all four
required jobs. The workflow rechecks the same run before building or publishing.
It does not accept a green run from an arbitrary branch or require main: main is
older than this source. The source CI must include successful
`personal-agent-bridge`, `fast-suite`, `postgres-smoke` and `native-ui-overlay`
jobs; `check_ci.py` fails closed if any required job is absent or unsuccessful.

The native image checks out official OpenClaw revision
`fc23bc864e4553c2d215e479eeec47b67a0bf943` without credentials, applies the exact
overlay patch SHA recorded in `services/openclaw_control_ui/release-metadata.json`,
installs with Node 24 and pnpm 12.5.1 using frozen lockfiles and ignored scripts,
runs the bounded source UI harness, compares the native archive's layer prefix
with the separately saved pinned base archive, and verifies a sealed context containing only
`dist/control-ui/` and `dist/build-info.json` over the pinned official base. No
gateway config, state, environment, credentials or scope is added.

The source preserves the API currently deployed on 27 October (including its five
performance patches and schema head ff60718293a4). This release adds an owner-bound,
read-only agent chat bridge with a server kill switch. It does not expand the
agent's tools or provider ownership and does not migrate data. The release runtime
uses two Gunicorn gthread workers with four threads each and timeout 180 seconds so
provider waits do not occupy all ordinary SCM request capacity. This concurrency
configuration was exercised in an isolated Linux fixture with the restored DB;
it does not substitute for authenticated production acceptance. The bridge budget is 150 seconds, client HTTP/UI 155/160 seconds. Gunicorn timeout is not a per-request deadline; a separately tested transport wall-clock limit must release its cross-worker lease even on a slow-drip response. The spawn transport deadline and lease recovery passed focused code review and HTTP slow-header/body checks on Windows and Linux Gunicorn; CI must pass for the exact pinned source before publication.

The API manifest carries only the reviewed source/runtime changes, including the
personal-agent route/service and their auth/config registration. The bridge is a
separate image and is never added to the API allowlist. No unrelated source tree,
tests, fixtures or secret material is activated by this release.

The workflow checks out the deployment recipe at the immutable `github.sha` of the
workflow commit, separately from the source. The coordinator must record that
workflow/recipe commit in the release receipt before dispatch. Both source and
recipe commits require review. Actions are pinned to SHAs.

## Runtime files and templates

runtime-allowlist.json enumerates 264 approved API files with SHA-256 hashes.
build_context.py creates a NEW context from these bytes; the source checkout is
never sent to Docker. Unexpected files in the checkout do not enter the context.
Every approved byte must match the fixed source; symlinks/escaping paths fail.

The bridge allowlist has 10 approved runtime files plus 2 context-only files.
`verify_bridge_layers.py` reads every
saved layer, rejects links, whiteouts, tests, secrets and unapproved `/app`
files, and checks `package-lock.json` plus the exact `ws@8.22.0` dependency tree.
The bridge base is fixed to
`node:24-bookworm-slim@sha256:c86ecead9e55a5210ac1727de38a4ca840bf03dcaf34dc9749addbf383b1920c`.

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
The publish job downloads the artifact by its exact ID from this run, verifies all
archive hashes and layers again, loads the tested images and checks all image IDs.
It never builds or executes application code. GHCR login uses the ephemeral
workflow token. Tags include source SHA, run ID and attempt; release-image.json
records all registry digests, source SHA, pinned recipe SHA, all bases and run.
There is no deploy step.

Execution prerequisites still needing coordinator resolution:

1. Independent review of the new local recipe and workflow commits before any push.
2. A protected scm-pilot-image-public environment with the reviewer/branch policy
   above. It is not created by this preparation; unavailable API metadata fails closed.
3. workflow_dispatch requires the workflow on the default branch. main is old:
   do not merge the optimized application into main or change default branches just
   to enable dispatch. Coordinate a reviewed workflow-only bootstrap before execution.
4. Supply a reviewed immutable python:3.12-slim@sha256 digest. No fabricated digest.
5. A supported authenticated GitHub execution path. No PAT or credentials are created.
6. All three images use disjoint tags in the existing public GHCR package. The workflow
   does not change visibility: it requires anonymous pulls of all distinct digests
   before emitting release evidence. If any pull fails, the run fails and no
   deployable candidate is emitted. Three pushes are not atomic; a partial publication
   without release evidence must not be deployed and may be retried in a new run.

7. The API and bridge allowlists have been regenerated from the immutable Git
   blobs of source `1ec2da4cc0e626debf4a1ee94ba6ecabab0adbab`, including the approved
   runtime template. Both generated contexts and preparation tests were verified.
   Any later source change requires new reviewed hashes and CI identity. Do not
   claim publication until
   the exact CI run, three archives, image IDs, context manifests, anonymous pulls
   and protected environment approval are recorded.

## Later deployment, outside current publication scope

This publication workflow does not deploy or change the running SCM service.
Deployment uses the existing reviewed production configuration after immutable
digest review and verified backups. Preserve database, resources, secret files,
mounts and AutoDeploy settings; do not replace the production compose with a
generic template or create a new service as part of image publication.

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
