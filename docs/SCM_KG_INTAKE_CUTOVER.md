# Automatic KG intake: cutover 2026-10-07

Authorization: user 2026-10-07 15:05 UTC explicitly authorized inventory/kardex deployment, automatic intake first, scale update window ending 17:00 UTC (12:00 Lima). Infrastructure is the sole API deployer; coordinate through the parent. No scale/API cutover after that deadline without renewed authorization. Human physical UAT remains pending; do not claim it passed.

## Artifact and configuration

Candidate derives from live BE b26b88324d206f2d98642523be053b79aacf2c9b, preserving the current assistant. No schema, role/security, station binary or frontend change. Publish/build this exact candidate using the current infra release process; the legacy checked-in image workflow has older pinned source and must not be dispatched unchanged. Recheck the live revision immediately before replacement; a changed revision requires integration, not overwrite.

Set only:

```
KG_AUTOMATIC_INTAKE_ENABLED=true
KG_PRODUCTION_LOCATION_CODE=PRODUCCION_KG
KG_AUTOMATIC_INTAKE_CUTOFF_AT=<T0, explicit ISO8601 UTC>
KG_CUSTODY_WRITE_ENABLED=false
KG_RECEIPT_WRITE_ENABLED=false
```

Location 9 was revalidated 15:09 UTC: active PUNTO_PRODUCCION, free balance allowed, PIEZA_COLOR/SUBENSAMBLE_WIP. It is a logical production location, not a physical tower assignment.

## Reproducible boundary and station behavior

T0 is a single immutable UTC timestamp recorded from the central server **after** pausing new capture and draining in-flight requests, **before** starting the new API image with the five settings above. Record that timestamp, the new image digest, previous image/config and station queue state in the execution receipt. All API replicas use exactly the same T0. Do not select an earlier T0 merely to accept residuals. Resume only after the configured T0 and clocks agree.

A precreated manga may receive its first KG control/final after T0: its creation date is irrelevant. Acceptance requires capture time and server operation-reservation time on/after T0, every original control/pesaje capture and server-created time on/after T0 (all states, including annulled/reopened), no UN quantities on manga/tramos/pesajes/controls or UN existence, and no pre-cutover/recovered KG existence. Thus an old empty prelabel remains usable without inventing a replacement identity.

An old control blocks another cumulative control, final or close-from-control even if the new reading is recent. A completed historical weighing or one of the 415 recovery existences is never credited by enabling intake. Its corrections require separate reconciliation. New post-cutover controls 5→8→12 credit only +5,+3,+4; correction of a post-cutover weighing adjusts only its delta. Original facts remain authoritative even if a correction proposes a later date.

Rejected commands return 409 KG_INTAKE_CUTOVER_REVIEW_REQUIRED with reason; the operation is rolled back. The operator must preserve the operation/capture UUID and escalate the identified manga; do not recreate it or edit dates. Missing/invalid/naive T0 returns KG_INTAKE_CUTOFF_REQUIRED. This is rejection, not a durable quarantine queue. Existing completed idempotent replays retain their original result, including pre-cutover responses without inventory; do not resend under a new UUID to create stock.

## Deployment sequence for infrastructure

1. Revalidate current image b26..., API health, location9 and all flags. Take fresh consistent pg_dump and retain hash; do not restore it over an active production DB as rollback.
2. Within the authorized window, coordinate station capture pause. Inspect pending/local/in-flight/uncertain operations. Resolve each uncertain outcome against central by its existing UUID. Preserve the queue; do not delete/rewrite captured dates or UUIDs. A post-T0 delivery with pre-T0 capture is blocked and must be reconciled separately. Current edge synchronous transport does not establish durable retention of rejected captures; record evidence rather than assuming no queue.
3. Drain requests; record T0 centrally and snapshot unprojected weighings/active controls/precreated mangas. Keep residual recovery frozen until parent coordinates it. At 15:19 UTC residual = 36 known UN / 182.800 kg + 32 new clean KG / 334.500 kg. Counts can advance before pause.
4. Replace only API with the exact reviewed image plus the five env settings, preserving assistant configuration and all unrelated env values. Use the existing infra rollout mechanism. Do not launch a broad stack replacement or database migration.
5. Verify running revision/digest, all five settings, health and assistant route health without fabricating a weighing. Verify location and cutoff parser in a read-only application context. Ensure all replicas agree before resuming capture.
6. Resume station before 17:00 UTC. Observe the first genuine authorized KG weighing/control: same source UUID, one existence, one initial ingress, correct NET, location9. Retry same command must not duplicate credit. Observe next cumulative control/final as a delta. No fictitious production smoke records. Keep UN behavior separate and custody/receipt off.
7. Record UAT evidence supplied by the operator, or pending if no real capture occurs. Record precise residual at T0 separately from post-cutover monitoring. Do not claim physical stock reconciliation.

## Rollback

Pause capture first. Turning intake off does not remove ledger history; weight-changing commands on an existing KG existence then reject KG_INTAKE_DISABLED_WITH_EXISTENCE. Keep the new guarded image while disabling the intake flag if configuration/operations require a rollback; returning to b26 removes this protection and needs separate assessment with capture paused. Never erase movements or restore a full DB to reverse a release. Re-enabling uses the original T0; changing it can exclude already admitted sources. Existing historical saldo remains pending exit reconciliation with Ximena.

## Validation evidence

Tests cover independent custody/intake flags, invalid config/location atomicity, clean precreated mangas, inclusive timezone boundary, old capture/control/annulled source/server receipt, UN projection, historical recovery exclusion, operation in flight, correction old/new, cumulative controls/final, close rollback, retries and shared-balance races. Test results and actual PostgreSQL version are in the deployment receipt. No automatic test substitutes physical UAT. The 415 historical entries must remain 415 / 2969.100 kg; no residual is applied by this code.

Final technical evidence: 84 SQLite cases and 4 real PostgreSQL 18.3 races; Docker Desktop 17 test runtime was unavailable, so native PostgreSQL was bound exclusively to 127.0.0.1:55510. Production PostgreSQL 17 parity is not claimed. First reconstructed draft ce85cfdd omitted a datetime import; it was rejected by final tests and is superseded, never published or deployed.

## Integrated KG + AZUL release addendum (2026-10-07)

This candidate adds OF74 per-run colada support to KG dedfbe42dcd36a8094dd85b4fd862b3b11310c52 using patch c9fa987772cd3df3542b2eb631740a8a711e8e2d. Unlike KG alone, it REQUIRES Alembic migration fe5f60718293 over fd4e5f607182. The earlier statements "no schema change" and "no database migration" apply only to the KG-only candidate and are superseded for this integrated release.

User authorization at 16:46 UTC covers AZUL deployment; at 16:52 UTC the user authorized cutting over now and explicitly extended the window until completion. Recipe/source publication approvals and automated approval review remain binding; no alternative publication path may bypass a rejection. Preserve the current frontend, station binary, assistant-off settings, custody-off and receipt-off. Reprinting follows KG and AZUL under separate coordination.

Pre-build: publish the exact integrated source, require its own successful Backend tests run (the successful KG-only run 37652933783 is insufficient), pin that SHA/run/branch in the approved image recipe, retain the publication environment and reviewers, and record immutable image digest. Do not pause station capture while building.

Before cutover: revalidate live image and configuration, health, Alembic head, station queue/in-flight operation UUIDs, and OF74 data guards with its owning worker. Take a fresh consistent backup with hash. Abort if the live head is not fd4e5f607182 or a concurrent release changed the assumed baseline. Confirm a tested compensating procedure for OF74, including no-activity guards, before authorizing its data transaction. Do not reuse stale OF74 snapshots as current evidence.

Cutover order: pause capture and relevant writes; drain and reconcile uncertain UUIDs; record immutable server T0. Apply only migration fe5f60718293 via the reviewed image and existing managed database credentials. Bound lock/statement waits and stop on contention; never drop/recreate tables or disable guards to force it. Verify the nullable numeric(12,4) column and check constraint, and that historical work snapshots remain unchanged. Start only the reviewed API image with the five KG settings above and otherwise preserve runtime configuration. Verify digest, migration head, health, assistant-off, custody-off and receipt-off before any AZUL data activation. OF74/C06 0 g and 50 kg activation is a separate guarded transaction using its reviewed procedure; coordinate its exact before/after receipt. Resume capture only after both API and data checks succeed. Observe genuine authorized work; do not manufacture smoke weighings.

Rollback: pause first. Keep the compatible new backend with KG guards; disable automatic intake if required without changing T0 or deleting ledger entries. The additive nullable schema can remain. Once a per-run override or C06 activity exists, returning to b26 or a KG-only backend would ignore the override and can reintroduce the 2 g header value. Do not downgrade the column or roll back only the backend. OF74 compensation is allowed only when its no-activity guards pass; otherwise stop and repair forward under coordination. Never restore the full database over operational writes.

Validation: 150 local SQLite tests passed (121.64 s). Eight integrated PostgreSQL 18.3 tests passed (31.74 s): actual colada migration preserves existing values and permits zero/rejects negative; material, prepared-material and OT consumers run against a schema migrated to head; four KG transaction races preserve cutoff and idempotency. PostgreSQL 17 production parity and physical UAT are not claimed. CI also collects the new PostgreSQL tests; its current service is PostgreSQL 16. Record integrated CI separately before deployment.
