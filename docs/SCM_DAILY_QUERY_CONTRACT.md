# SCM daily query prototype contract

Only `production_daily_summary` is accepted. The local date is resolved in
`America/Lima`, and `ayer` is resolved from the Lima calendar. The read window
is the half open interval `[00:00, next 00:00)` converted to UTC and filtered
by `pesada_at`.

Every group has `of`, `color`, `weighings`, `net_kg`, `cancelled_weighings`,
`cancelled_kg`, `weighing_ids`, and `manga_ids`. Cancelled rows are reported
separately and never contribute to `net_kg`. Non VIGENTE rows that are not
cancelled are excluded and listed under top level `excluded` with a count and
IDs. The daily subtotal is never presented as OF progress; accumulated OF
progress and compatible target comparison remain the responsibility of
`list_production_progress`.

`as_of_utc` is captured from the PostgreSQL transaction snapshot on a cache
miss (with an injected local clock fallback) and remains unchanged on a cache
hit.
The in memory cache key contains actor, permission scope, fixed filters, and
adapter version. `DailyQueryService.invalidate(actor_id=...)` invalidates one
actor; omitting the actor invalidates all entries. The bounded audit log stores
actor, intent, date, latency, trace ID, cache state, and status only.

The actor must be active and have `ASISTENTE_PRODUCCION_USAR`, `OT_VER`, and
`MANGA_PESAJE_VER`. Access is reassigned by changing that persisted
capability; the service does not grant capabilities or use an actor allowlist.
The remote adapter is `AUTH_PENDING` and never handles tokens. The CLI sets
PostgreSQL read-only defaults and the adapter verifies `REPEATABLE READ` and
`READ ONLY` before reading.
