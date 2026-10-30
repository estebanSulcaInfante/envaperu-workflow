# SCM Contabo: reviewed API + bridge image preparation

Status: local preparation; independent review required before pushing or publishing.
The user authorized PUBLIC GHCR images containing the API and the personal-agent
bridge, with code, dependencies and necessary runtime templates. No VPS deployment,
credentials, Odoo changes or data migration are part of this package.

The exact application source is `d9662d5581e780b0b9f64ae40208884f2add6403` and
the candidate CI gate is run `41098144080` on branch
`codex/personal-agent-assets-20261030`. The API context is independently hashed
by `runtime-allowlist.json`; the bridge context is independently hashed by
`bridge-runtime-allowlist.json` and includes only its pinned Dockerfile,
package manifests and `src/**`. Tests, examples, `.env` files, fixtures and
`node_modules` are never copied into either context.

Both images use the existing public GHCR package
`ghcr.io/estebansulcainfante/envaperu-scm-api`, with distinct immutable tags
`sha-<source>-<run>-<attempt>` and
`bridge-sha-<source>-<run>-<attempt>`. The manual workflow builds, smoke-checks
and layer-verifies both images in the unprivileged job, uploads `api.tar` and
`bridge.tar` together, then has one `scm-pilot-image-public` protected publish
job re-check both archives, pushes both tags, and pulls both resulting digests
with an empty anonymous Docker config before writing evidence. A private or
unpullable package therefore cannot produce `release-image.json`.

## Source and trusted CI

Application source is fixed to d9662d5581e780b0b9f64ae40208884f2add6403.
check_ci.py accepts only GitHub Actions run 41098144080, repository
estebanSulcaInfante/envaperu-workflow, branch codex/personal-agent-assets-20261030,
push event, .github/workflows/tests.yml, exact source SHA, completed and success.
At recipe preparation time that run was in progress; publication remains blocked
until this exact run is observed completed with success.
It does not accept a green run from an arbitrary branch or require main: main is
older than this source. The existing source CI includes fast-suite/postgres-smoke.

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
The publish job downloads the artifact by its exact ID from this run, verifies both
archive hashes and all layers again, loads the tested images and checks both image
IDs. It never builds or executes application code. GHCR login uses the ephemeral
workflow token. Tags include source SHA, run ID and attempt; release-image.json
records both registry digests, source SHA, pinned recipe SHA, both bases and run.
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
6. Both images use disjoint tags in the existing public GHCR package. The workflow
   does not change visibility: it requires anonymous pulls of both distinct digests
   before emitting release evidence. If either pull fails, the run fails and no
   deployable candidate is emitted. Two pushes are not atomic; a partial publication
   without release evidence must not be deployed and may be retried in a new run.

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
