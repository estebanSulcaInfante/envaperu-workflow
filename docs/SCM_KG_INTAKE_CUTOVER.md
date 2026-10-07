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
