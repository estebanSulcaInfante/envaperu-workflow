#!/bin/sh
# Root-run, reviewed one-shot backup/restore rehearsal. NO production migration.
set -eu
db=envaperuscmpilot-scmpilot-cnxr9n-db-1
test_container=enva-of-execution-restore-20261022
backup_dir=/var/backups/envaperu-scm/releases/of-estados-20261022
dump="$backup_dir/pre-migracion-final-20261022T1950.dump"
test -s "$dump"
test "$(sha256sum "$dump" | cut -d' ' -f1)" = 1d71c09eaa0866bdf50e489743c21cf02a4ab7287d3a9d5ffb3f01b1a0a83f81
upgrade="$backup_dir/of-execution-upgrade.sql"
test -f "$upgrade"
test "$(sha256sum "$upgrade" | cut -d' ' -f1)" = fa67f97c6acc1731b6a1cc0c383d201e06ee6b77e75b889d5b9035991f0c35e9
test -z "$(docker ps -aq --filter name="^/${test_container}$")"
test "$(docker inspect --format '{{.State.Health.Status}}' "$db")" = healthy
identity=$(docker exec "$db" sh -c 'psql -U "$POSTGRES_USER" -d scm_pilot -X -Atc "SELECT current_database(); SELECT version_num FROM alembic_version;"')
test "$identity" = "$(printf 'scm_pilot\nb7f31e9c120a')"
image=$(docker inspect --format '{{.Image}}' "$db")
test "$(docker exec "$db" postgres --version | cut -d' ' -f3 | cut -d. -f1)" = 17
sha256sum "$dump"
docker run -d --rm --name "$test_container" --label enva.task=of-execution-restore-20261022 \
  --network none --memory 768m --cpus 0.5 \
  -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=scm_pilot \
  --mount "type=bind,src=$backup_dir,dst=/backup,readonly" "$image" >/dev/null
cleanup() {
  owned=$(docker inspect --format '{{index .Config.Labels "enva.task"}}' "$test_container" 2>/dev/null || true)
  if test "$owned" = of-execution-restore-20261022; then docker stop -t 10 "$test_container" >/dev/null; fi
}
trap cleanup EXIT HUP INT TERM
ready=no
for i in $(seq 1 30); do
  # The image's temporary init server only listens on its Unix socket. Wait
  # for final TCP readiness so pg_restore cannot race its planned shutdown.
  if docker exec "$test_container" pg_isready -h 127.0.0.1 -U postgres >/dev/null 2>&1; then ready=yes; break; fi
  sleep 1
done
test "$ready" = yes
docker exec "$test_container" pg_restore --list /backup/pre-migracion-final-20261022T1950.dump >/dev/null
# Policy role names are required even with --no-owner --no-acl. Mirror only
# the two observed SCM policy roles, without login or production credentials.
docker exec "$test_container" psql -U postgres -d postgres -X -v ON_ERROR_STOP=1 \
  -c "CREATE ROLE scm_api NOLOGIN; CREATE ROLE scm_reader NOLOGIN;"
docker exec "$test_container" pg_restore -U postgres -d scm_pilot --no-owner --no-acl --exit-on-error /backup/pre-migracion-final-20261022T1950.dump
facts() {
  for table in scm_manga scm_pesaje_manga scm_existencia_manga scm_existencia_manga_kg scm_movimiento_inventario scm_movimiento_inventario_kg scm_movimiento_material_inventario scm_movimiento_material_preparado; do
    docker exec "$test_container" psql -U postgres -d scm_pilot -X -Atc \
      "SELECT '$table',count(*),md5(coalesce(string_agg(h,',' ORDER BY h),'')) FROM (SELECT md5(to_jsonb(t)::text) h FROM $table t) s;"
  done
}
before=$(facts)
docker exec "$test_container" psql -U postgres -d scm_pilot -X -v ON_ERROR_STOP=1 -f /backup/of-execution-upgrade.sql
after=$(facts)
test "$before" = "$after"
docker exec "$test_container" psql -U postgres -d scm_pilot -X -Atc \
  "SELECT current_database(); SELECT version_num FROM alembic_version; SELECT 'mangas',count(*) FROM scm_manga; SELECT 'pesajes',count(*) FROM scm_pesaje_manga; SELECT codigo,count(*) FROM scm_capacidad WHERE codigo IN ('OF_PAUSAR_EJECUCION','OF_CANCELAR_EJECUCION') GROUP BY codigo;"
printf '%s\n' "$after"
printf '%s\n' 'BACKUP_RESTORE_MIGRATION_REHEARSAL_PASS: isolated PG17, facts unchanged, no production changes'
