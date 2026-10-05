"""Focused regressions for direct KG batch withdrawal."""
import json
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app import db
from app.models.scm_inventory_kg import (
    ScmExistenciaMangaKg,
    ScmReservaUnidadKg,
    ScmRetiroArmadoKg,
    ScmSaldoInventarioKg,
    ScmUnidadFisicaKg,
)
from app.models.scm_auditoria import ScmOperacion
from app.models.scm_inventory import ScmUbicacionInventario
from app.models.scm_inventory_operations import ScmAlmacen, ScmAlmacenTrabajador
from app.models.scm_ot import ScmEtiquetaManga
from app.models.trabajador import Trabajador
from app.services.scm_kg_custody_service import (
    get_kg_withdrawal_batch,
    resolve_kg_outgoing,
    resolve_kg_return,
    withdraw_kg_batch,
)
from app.services.scm_service_support import ScmServiceError
from app.services.scm_warehouse_service import decide_manga_quality
from test_scm_kg_custody import _grant_capabilities, _received


def _batch_ready(app):
    first = _received(app)
    actor = first["actor"]
    _grant_capabilities(actor, ("PICKING_PREPARAR", "PICKING_DESPACHAR", "ABASTECIMIENTO_VER"))
    existence = first["existence"]
    decide_manga_quality(
        db.session,
        actor_id=actor.id,
        existence_id=existence.id,
        operation_id=uuid4(),
        data={"decision": "LIBERADA", "motivo": "Conforme", "version": existence.version},
    )
    db.session.commit()
    app.config["KG_CUSTODY_WRITE_ENABLED"] = True
    first_unit = db.session.get(ScmUnidadFisicaKg, first["existence"].unidad_fisica_kg_id)
    first_unit.saldo.cantidad_fisica_kg = Decimal("24.000")
    warehouse = ScmAlmacen(codigo="KG-BATCH", nombre="KG Batch", tipo="PIEZAS_WIP")
    db.session.add(warehouse)
    db.session.flush()
    first_unit.almacen_responsable_id = warehouse.id
    first_unit.ubicacion.almacen_id = warehouse.id
    db.session.add(ScmAlmacenTrabajador(
        almacen_id=warehouse.id, trabajador_id=actor.id,
        clases_articulo_json=["PIEZA_COLOR"], asignado_por_id=actor.id,
    ))
    second_unit = ScmUnidadFisicaKg(
        public_id=uuid4(), codigo=f"{first_unit.codigo}-BATCH",
        articulo_scm_id=first_unit.articulo_scm_id,
        saldo_id=first_unit.saldo_id, ubicacion_id=first_unit.ubicacion_id,
        almacen_responsable_id=first_unit.almacen_responsable_id,
        estado="ACTIVA", estado_logistico="RECIBIDA_ALMACEN",
        estado_calidad="LIBERADA", kg_entregado=Decimal("12.000"),
        atributo_proceso=first_unit.atributo_proceso,
    )
    db.session.add(second_unit)
    db.session.commit()
    units = [first_unit, second_unit]
    return actor, units


def _request(units, *, holder_id, versions=None):
    versions = versions or [unit.version for unit in units]
    return {
        "items": [
            {"unit_id": str(unit.id), "version": version}
            for unit, version in zip(units, versions)
        ],
        "motivo_operativo": "Armado lote de prueba",
        "tenedor_fisico_id": holder_id,
        "documento_destino_tipo": "OA",
        "documento_destino_id": "OA-BATCH-1",
    }


def test_direct_batch_is_atomic_and_deduplicates_unit_aliases(app):
    with app.app_context():
        actor, units = _batch_ready(app)
        key = uuid4()
        data = _request(units, holder_id=actor.id)
        data["items"].append(dict(data["items"][0]))
        original_data = {**data, "items": [dict(item) for item in data["items"]]}
        response = withdraw_kg_batch(db.session, actor_id=actor.id, operation_id=key, data=data)

        assert response["operation_id"] == str(key)
        assert response["cantidad_unidades"] == 2
        assert response["total_kg"] == "24.000"
        assert len(response["retiros"]) == 2
        assert all(len(retiro["items"]) == 1 for retiro in response["retiros"])
        assert ScmRetiroArmadoKg.query.count() == 2
        assert ScmReservaUnidadKg.query.filter_by(estado="ACTIVA").count() == 0
        assert ScmReservaUnidadKg.query.filter_by(estado="RETIRADA").count() == 2
        assert all(db.session.get(ScmUnidadFisicaKg, unit.id).estado_logistico == "RETIRADA_ARMADO" for unit in units)

        replay = withdraw_kg_batch(db.session, actor_id=actor.id, operation_id=key, data=original_data)
        assert replay == response
        with pytest.raises(ScmServiceError) as conflict:
            withdraw_kg_batch(
                db.session,
                actor_id=actor.id,
                operation_id=key,
                data={**original_data, "motivo_operativo": "otro"},
            )
        assert conflict.value.code == "IDEMPOTENCY_CONFLICT"
        recovered = get_kg_withdrawal_batch(db.session, actor_id=actor.id, operation_id=key)
        assert recovered == response


def test_direct_batch_validates_every_unit_before_mutating_any(app):
    with app.app_context():
        actor, units = _batch_ready(app)
        before = [(unit.id, unit.version, unit.estado_logistico, Decimal(unit.saldo.cantidad_fisica_kg)) for unit in units]
        key = uuid4()
        versions = [units[0].version, units[1].version + 1]
        with pytest.raises(ScmServiceError) as error:
            withdraw_kg_batch(
                db.session,
                actor_id=actor.id,
                operation_id=key,
                data=_request(units, holder_id=actor.id, versions=versions),
            )
        assert error.value.code == "VERSION_CONFLICT"
        assert ScmRetiroArmadoKg.query.count() == 0
        assert ScmReservaUnidadKg.query.count() == 0
        assert db.session.get(ScmOperacion, key) is None
        after = [
            (db.session.get(ScmUnidadFisicaKg, unit_id).id,
             db.session.get(ScmUnidadFisicaKg, unit_id).version,
             db.session.get(ScmUnidadFisicaKg, unit_id).estado_logistico,
             Decimal(db.session.get(ScmUnidadFisicaKg, unit_id).saldo.cantidad_fisica_kg))
            for unit_id, _, _, _ in before
        ]
        assert after == before


def test_direct_batch_aggregates_requirements_per_shared_balance(app):
    with app.app_context():
        actor, units = _batch_ready(app)
        units[0].saldo.cantidad_fisica_kg = Decimal("12.000")
        db.session.commit()
        with pytest.raises(ScmServiceError) as error:
            withdraw_kg_batch(
                db.session,
                actor_id=actor.id,
                operation_id=uuid4(),
                data=_request(units, holder_id=actor.id),
            )
        assert error.value.code == "INVENTORY_CONFLICT"
        assert ScmRetiroArmadoKg.query.count() == 0
        assert ScmReservaUnidadKg.query.count() == 0


def test_batch_get_is_actor_scoped_and_missing_is_404(app):
    with app.app_context():
        actor, units = _batch_ready(app)
        key = uuid4()
        response = withdraw_kg_batch(db.session, actor_id=actor.id, operation_id=key, data=_request(units, holder_id=actor.id))
        assert response["cantidad_unidades"] == 2
        with pytest.raises(ScmServiceError) as missing:
            get_kg_withdrawal_batch(db.session, actor_id=actor.id, operation_id=uuid4())
        assert missing.value.status_code == 404


def test_outgoing_resolver_accepts_joint_picking_but_legacy_return_does_not(app):
    with app.app_context():
        actor, units = _batch_ready(app)
        from app.models.scm_catalogos import ScmCapacidad
        read_capability = ScmCapacidad.query.filter_by(codigo="ABASTECIMIENTO_VER").one()
        for role in actor.roles:
            if read_capability in role.capacidades:
                role.capacidades.remove(read_capability)
        db.session.commit()
        resolved = resolve_kg_outgoing(db.session, actor_id=actor.id, code=units[0].codigo)
        assert resolved["unit"]["id"] == str(units[0].id)
        with pytest.raises(ScmServiceError) as error:
            resolve_kg_return(db.session, actor_id=actor.id, code=units[0].codigo)
        assert error.value.code == "CAPABILITY_REQUIRED"
        assert error.value.details["capability"] == "ABASTECIMIENTO_VER"


def _prepesaje_qr(label):
    return json.dumps({"v": 1, "label_id": str(label.public_id)})


def test_resolver_accepts_current_prepesaje_and_rejects_stale_label(app):
    with app.app_context():
        actor, units = _batch_ready(app)
        label = db.session.query(ScmEtiquetaManga).filter_by(
            manga_id=db.session.get(ScmExistenciaMangaKg, units[0].recepcion_vigente_id).manga_id,
            tipo="PREPESAJE",
        ).order_by(ScmEtiquetaManga.version.desc()).first()
        assert label is not None
        resolved = resolve_kg_return(db.session, actor_id=actor.id, code=_prepesaje_qr(label))
        assert resolved["unit"]["id"] == str(units[0].id)

        replacement = ScmEtiquetaManga(
            public_id=uuid4(), manga_id=label.manga_id,
            trabajo_impresion_id=label.trabajo_impresion_id,
            tipo="PREPESAJE", version=label.version + 1,
            estado="IMPRESA", plantilla_version=label.plantilla_version,
            payload_hash="b" * 64, payload_json=dict(label.payload_json or {}),
        )
        db.session.add(replacement)
        db.session.commit()
        with pytest.raises(ScmServiceError) as error:
            resolve_kg_return(db.session, actor_id=actor.id, code=_prepesaje_qr(label))
        assert error.value.code == "ETIQUETA_VERSION_CONFLICT"


def test_resolver_rejects_prepesaje_without_existing_kg_identity(app):
    with app.app_context():
        actor, units = _batch_ready(app)
        label = db.session.query(ScmEtiquetaManga).filter_by(
            manga_id=db.session.get(ScmExistenciaMangaKg, units[0].recepcion_vigente_id).manga_id,
            tipo="PREPESAJE",
        ).order_by(ScmEtiquetaManga.version.desc()).first()
        existence = db.session.scalar(select(ScmExistenciaMangaKg).where(
            ScmExistenciaMangaKg.manga_id == label.manga_id,
            ScmExistenciaMangaKg.estado_logistico != "REVERSADA",
        ))
        existence.unidad_fisica_kg_id = None
        db.session.commit()
        with pytest.raises(ScmServiceError) as error:
            resolve_kg_return(db.session, actor_id=actor.id, code=_prepesaje_qr(label))
        assert error.value.code == "KG_IDENTITY_MISSING"


def test_resolver_hides_prepesaje_state_outside_actor_scope(app):
    with app.app_context():
        actor, units = _batch_ready(app)
        label = db.session.query(ScmEtiquetaManga).filter_by(
            manga_id=db.session.get(ScmExistenciaMangaKg, units[0].recepcion_vigente_id).manga_id,
            tipo="PREPESAJE",
        ).order_by(ScmEtiquetaManga.version.desc()).first()
        label.estado = "INVALIDADA"
        outsider = Trabajador(
            codigo=f"KG-OUTSIDER-{uuid4().hex[:8]}",
            nombres="Sin", apellidos="Alcance", activo=True,
        )
        outsider.roles.append(actor.roles[0])
        db.session.add(outsider)
        db.session.commit()
        with pytest.raises(ScmServiceError) as error:
            resolve_kg_return(db.session, actor_id=outsider.id, code=_prepesaje_qr(label))
        assert error.value.code == "KG_UNIT_NOT_FOUND"
        assert error.value.status_code == 404


def test_resolver_rejects_historical_identity_from_current_prepesaje(app):
    with app.app_context():
        actor, units = _batch_ready(app)
        label = db.session.query(ScmEtiquetaManga).filter_by(
            manga_id=db.session.get(ScmExistenciaMangaKg, units[0].recepcion_vigente_id).manga_id,
            tipo="PREPESAJE",
        ).order_by(ScmEtiquetaManga.version.desc()).first()
        units[0].estado = "HISTORICA"
        db.session.commit()
        with pytest.raises(ScmServiceError) as error:
            resolve_kg_return(db.session, actor_id=actor.id, code=_prepesaje_qr(label))
        assert error.value.code == "KG_UNIT_HISTORICAL"


def test_batch_rejects_balance_from_other_warehouse(app):
    with app.app_context():
        actor, units = _batch_ready(app)
        other = ScmAlmacen(codigo="KG-OTHER", nombre="KG Other", tipo="PIEZAS_WIP")
        db.session.add(other)
        db.session.flush()
        other_location = ScmUbicacionInventario(
            codigo="KG-OTHER-POS", nombre="KG Other", tipo="POSICION",
            almacen_id=other.id, activo=True, permite_saldo_libre=True,
        )
        db.session.add(other_location)
        db.session.flush()
        balance = ScmSaldoInventarioKg(
            articulo_scm_id=units[1].articulo_scm_id,
            ubicacion_id=other_location.id,
            cantidad_fisica_kg=Decimal("24.000"),
        )
        db.session.add(balance)
        db.session.flush()
        units[1].saldo_id = balance.id
        db.session.commit()
        with pytest.raises(ScmServiceError) as error:
            withdraw_kg_batch(
                db.session, actor_id=actor.id, operation_id=uuid4(),
                data=_request(units, holder_id=actor.id),
            )
        assert error.value.code == "INVENTORY_CONFLICT"
        assert ScmRetiroArmadoKg.query.count() == 0
        assert ScmReservaUnidadKg.query.count() == 0
