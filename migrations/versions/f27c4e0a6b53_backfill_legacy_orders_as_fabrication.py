"""backfill legacy technical orders as canonical fabrication orders

Revision ID: f27c4e0a6b53
Revises: f16b3d9e5a42
Create Date: 2026-07-29 14:00:00.000000
"""

import uuid

from alembic import op
import sqlalchemy as sa


revision = "f27c4e0a6b53"
down_revision = "f16b3d9e5a42"
branch_labels = None
depends_on = None

LEGACY_NAMESPACE = uuid.UUID("06c96d0c-8f54-5aca-b4b2-c377d631fc91")


def _stable_id(kind, legacy_id):
    return uuid.uuid5(LEGACY_NAMESPACE, f"{kind}:{legacy_id}")


def upgrade():
    connection = op.get_bind()
    orders = connection.execute(sa.text("""
        SELECT numero_op, molde_id, maquina_id, snapshot_tiempo_ciclo,
               snapshot_horas_turno, snapshot_peso_colada_gr
          FROM orden_produccion
         ORDER BY numero_op
    """)).mappings().all()

    for order in orders:
        legacy_code = order["numero_op"]
        operation_id = _stable_id("orden-fabricacion", legacy_code)
        connection.execute(sa.text("""
            INSERT INTO scm_orden_operacion (
                id, codigo, tipo, origen_demanda, motivo, estado, version
            ) VALUES (
                :id, :codigo, 'FABRICACION', 'LEGACY_SIN_OP_DEMANDA',
                'Migrada desde orden_produccion tecnica', 'LIBERADA', 1
            )
        """), {
            "id": operation_id,
            "codigo": f"OF-LEG-{legacy_code}"[:32],
        })
        connection.execute(sa.text("""
            INSERT INTO scm_orden_fabricacion (
                orden_operacion_id, molde_id, maquina_prevista_id,
                snapshot_tiempo_ciclo_seg, snapshot_horas_turno,
                snapshot_peso_colada_gr, codigo_legacy_op
            ) VALUES (
                :id, :molde_id, :maquina_id, :tiempo_ciclo, :horas_turno,
                :peso_colada, :legacy_code
            )
        """), {
            "id": operation_id,
            "molde_id": order["molde_id"],
            "maquina_id": order["maquina_id"],
            "tiempo_ciclo": (
                order["snapshot_tiempo_ciclo"]
                if (order["snapshot_tiempo_ciclo"] or 0) > 0
                else None
            ),
            "horas_turno": (
                order["snapshot_horas_turno"]
                if (order["snapshot_horas_turno"] or 0) > 0
                else None
            ),
            "peso_colada": order["snapshot_peso_colada_gr"],
            "legacy_code": legacy_code,
        })
        connection.execute(sa.text("""
            UPDATE registro_diario_produccion
               SET orden_operacion_id = :of_id
             WHERE orden_id = :legacy_code
        """), {
            "of_id": operation_id,
            "legacy_code": legacy_code,
        })

    runs = connection.execute(sa.text("""
        SELECT id, numero_op, color_produccion_id,
               receta_color_maestra_id, meta_kg
          FROM lote_color
         ORDER BY numero_op, id
    """)).mappings().all()
    sequence_by_order = {}
    run_ids_by_order = {}
    for run in runs:
        legacy_code = run["numero_op"]
        sequence = sequence_by_order.get(legacy_code, 0) + 1
        sequence_by_order[legacy_code] = sequence
        run_id = _stable_id("corrida-fabricacion", run["id"])
        operation_id = _stable_id("orden-fabricacion", legacy_code)
        run_ids_by_order.setdefault(legacy_code, []).append(run_id)
        connection.execute(sa.text("""
            INSERT INTO scm_corrida_fabricacion (
                id, orden_fabricacion_id, codigo, secuencia,
                color_produccion_id, receta_revision_id, ciclos_objetivo,
                estado, lote_color_legacy_id, meta_kg_legacy
            ) VALUES (
                :id, :of_id, :codigo, :secuencia, :color_id, :receta_id,
                NULL, 'LIBERADA', :legacy_id, :meta_kg
            )
        """), {
            "id": run_id,
            "of_id": operation_id,
            "codigo": f"OF-LEG-{legacy_code}-C{sequence:02d}"[:48],
            "secuencia": sequence,
            "color_id": run["color_produccion_id"],
            "receta_id": run["receta_color_maestra_id"],
            "legacy_id": run["id"],
            "meta_kg": run["meta_kg"],
        })

    outputs = connection.execute(sa.text("""
        SELECT salida.id, salida.lote_color_id, lote.numero_op,
               salida.cavidades_snapshot,
               salida.peso_unitario_snapshot_gr,
               salida.cantidad_objetivo, salida.kg_objetivo_neto,
               enlace.articulo_id
          FROM lote_salida_pieza_color AS salida
          JOIN lote_color AS lote ON lote.id = salida.lote_color_id
          JOIN scm_articulo_pieza_color AS enlace
            ON enlace.pieza_color_sku = salida.pieza_color_sku
         WHERE salida.cantidad_objetivo > 0
         ORDER BY salida.id
    """)).mappings().all()
    for output in outputs:
        connection.execute(sa.text("""
            INSERT INTO scm_orden_operacion_salida (
                id, orden_operacion_id, corrida_fabricacion_id,
                articulo_scm_id, cantidad_por_ciclo_snapshot,
                peso_unitario_snapshot_g, cantidad_objetivo,
                kg_estandar_objetivo, excedente_objetivo,
                lote_salida_legacy_id
            ) VALUES (
                :id, :of_id, :run_id, :article_id, :per_cycle,
                :unit_weight, :quantity, :kg, 0, :legacy_id
            )
        """), {
            "id": _stable_id("salida-operacion", output["id"]),
            "of_id": _stable_id(
                "orden-fabricacion", output["numero_op"]
            ),
            "run_id": _stable_id(
                "corrida-fabricacion", output["lote_color_id"]
            ),
            "article_id": output["articulo_id"],
            "per_cycle": output["cavidades_snapshot"],
            "unit_weight": output["peso_unitario_snapshot_gr"],
            "quantity": output["cantidad_objetivo"],
            "kg": output["kg_objetivo_neto"],
            "legacy_id": output["id"],
        })

    # Solo se asigna corrida cuando la orden tenía exactamente un color; en
    # los demás casos queda pendiente de clasificación, sin inventar cuál fue.
    for legacy_code, run_ids in run_ids_by_order.items():
        if len(run_ids) == 1:
            connection.execute(sa.text("""
                UPDATE registro_diario_produccion
                   SET corrida_fabricacion_id = :run_id
                 WHERE orden_id = :legacy_code
            """), {
                "run_id": run_ids[0],
                "legacy_code": legacy_code,
            })

    connection.execute(sa.text("""
        UPDATE scm_plan_manga_op AS plan
           SET orden_operacion_id = fabricacion.orden_operacion_id
          FROM scm_orden_fabricacion AS fabricacion
         WHERE fabricacion.codigo_legacy_op = plan.orden_id
    """))
    connection.execute(sa.text("""
        UPDATE scm_plan_manga_op_linea AS linea
           SET orden_operacion_salida_id = salida.id
          FROM scm_orden_operacion_salida AS salida
         WHERE salida.lote_salida_legacy_id =
               linea.lote_salida_pieza_color_id
    """))


def downgrade():
    connection = op.get_bind()
    demand_count = connection.execute(sa.text(
        "SELECT count(*) FROM scm_orden_produccion"
    )).scalar_one()
    nonlegacy_count = connection.execute(sa.text("""
        SELECT count(*)
          FROM scm_orden_operacion
         WHERE origen_demanda <> 'LEGACY_SIN_OP_DEMANDA'
    """)).scalar_one()
    if demand_count or nonlegacy_count:
        raise RuntimeError(
            "SCM_TS010P_BACKFILL_DOWNGRADE_BLOCKED: existen ordenes v2"
        )

    connection.execute(sa.text("""
        UPDATE scm_plan_manga_op_linea
           SET orden_operacion_salida_id = NULL
    """))
    connection.execute(sa.text("""
        UPDATE scm_plan_manga_op SET orden_operacion_id = NULL
    """))
    connection.execute(sa.text("""
        UPDATE registro_diario_produccion
           SET orden_operacion_id = NULL,
               corrida_fabricacion_id = NULL
    """))
    connection.execute(sa.text(
        "DELETE FROM scm_orden_operacion_salida"
    ))
    connection.execute(sa.text("DELETE FROM scm_corrida_fabricacion"))
    connection.execute(sa.text("DELETE FROM scm_orden_fabricacion"))
    connection.execute(sa.text("""
        DELETE FROM scm_orden_operacion
         WHERE origen_demanda = 'LEGACY_SIN_OP_DEMANDA'
    """))
