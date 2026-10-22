-- Generated from the reviewed Alembic ff60718293a4 upgrade(), PostgreSQL dialect.
-- Requires a verified pre-release backup and the approved maintenance window.
-- No state transitions of existing OFs, no inventory/production data changes.
\set ON_ERROR_STOP on
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';
SET LOCAL search_path TO public, pg_catalog;
SELECT pg_advisory_xact_lock(20261022001);
DO $gate$
BEGIN
  IF current_database() <> 'scm_pilot' THEN RAISE EXCEPTION 'WRONG_DATABASE'; END IF;
  IF (SELECT count(*) FROM alembic_version) <> 1 OR
     NOT EXISTS (SELECT 1 FROM alembic_version WHERE version_num='b7f31e9c120a')
  THEN RAISE EXCEPTION 'UNEXPECTED_ALEMBIC_PARENT'; END IF;
  IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public'
    AND table_name='scm_trabajo_ot' AND column_name='estado_previo_pausa')
  THEN RAISE EXCEPTION 'MIGRATION_ALREADY_OR_PARTIALLY_APPLIED'; END IF;
  IF (SELECT count(*) FROM rol_operativo WHERE codigo IN
    ('GERENTE_GENERAL','JEFE_PRODUCCION','SUPERVISOR')) <> 3
  THEN RAISE EXCEPTION 'EXPECTED_ROLES_MISSING'; END IF;
  IF EXISTS (SELECT 1 FROM scm_capacidad WHERE codigo IN
    ('OF_PAUSAR_EJECUCION','OF_CANCELAR_EJECUCION'))
  THEN RAISE EXCEPTION 'LIFECYCLE_CAPABILITIES_PREEXIST_REVIEW_REQUIRED'; END IF;
END
$gate$;
INSERT INTO scm_capacidad (codigo, nombre, activo)
        VALUES
            ('OF_PAUSAR_EJECUCION', 'Pausar y reanudar la ejecucion de OF', TRUE),
            ('OF_CANCELAR_EJECUCION', 'Cancelar definitivamente la ejecucion de OF', TRUE)
        ON CONFLICT (codigo) DO UPDATE SET activo = TRUE;

INSERT INTO scm_rol_capacidad (rol_operativo_id, capacidad_id)
        SELECT rol.id, cap.id
          FROM rol_operativo rol
          CROSS JOIN scm_capacidad cap
         WHERE rol.codigo IN ('GERENTE_GENERAL', 'JEFE_PRODUCCION')
           AND cap.codigo IN ('OF_PAUSAR_EJECUCION', 'OF_CANCELAR_EJECUCION')
        ON CONFLICT DO NOTHING;

INSERT INTO scm_rol_capacidad (rol_operativo_id, capacidad_id)
        SELECT rol.id, cap.id
          FROM rol_operativo rol
          CROSS JOIN scm_capacidad cap
         WHERE rol.codigo = 'SUPERVISOR'
           AND cap.codigo = 'OF_PAUSAR_EJECUCION'
        ON CONFLICT DO NOTHING;

ALTER TABLE scm_orden_operacion DROP CONSTRAINT ck_scm_orden_operacion_estado;

ALTER TABLE scm_orden_operacion ADD CONSTRAINT ck_scm_orden_operacion_estado CHECK (estado IN ('BORRADOR', 'LIBERADA', 'PROGRAMADA', 'EN_EJECUCION', 'PAUSADA', 'CERRADA', 'CANCELADA', 'ANULADA'));

ALTER TABLE scm_corrida_fabricacion DROP CONSTRAINT ck_scm_corrida_estado;

ALTER TABLE scm_corrida_fabricacion ADD CONSTRAINT ck_scm_corrida_estado CHECK (estado IN ('BORRADOR', 'LIBERADA', 'EN_EJECUCION', 'PAUSADA', 'COMPLETADA', 'CANCELADA', 'ANULADA'));

ALTER TABLE scm_trabajo_ot DROP CONSTRAINT ck_scm_trabajo_ot_estado;

ALTER TABLE scm_trabajo_ot ADD CONSTRAINT ck_scm_trabajo_ot_estado CHECK (estado IN ('PLANIFICADO', 'EN_EJECUCION', 'PAUSADO', 'COMPLETADO', 'CANCELADO', 'ANULADO'));

ALTER TABLE scm_trabajo_ot ADD COLUMN estado_previo_pausa VARCHAR(24);


DO $permissions$
BEGIN
  IF (SELECT count(*) FROM scm_rol_capacidad rc JOIN scm_capacidad c
      ON c.id=rc.capacidad_id WHERE c.codigo IN
      ('OF_PAUSAR_EJECUCION','OF_CANCELAR_EJECUCION')) <> 5
  OR EXISTS (SELECT 1 FROM scm_rol_capacidad rc JOIN scm_capacidad c
      ON c.id=rc.capacidad_id JOIN rol_operativo r ON r.id=rc.rol_operativo_id
      WHERE c.codigo IN ('OF_PAUSAR_EJECUCION','OF_CANCELAR_EJECUCION')
      AND NOT (r.codigo IN ('GERENTE_GENERAL','JEFE_PRODUCCION')
        OR (r.codigo='SUPERVISOR' AND c.codigo='OF_PAUSAR_EJECUCION')))
  THEN RAISE EXCEPTION 'LIFECYCLE_GRANTS_MISMATCH'; END IF;
END
$permissions$;
UPDATE alembic_version SET version_num='ff60718293a4' WHERE version_num='b7f31e9c120a';
COMMIT;
SELECT version_num FROM alembic_version;
