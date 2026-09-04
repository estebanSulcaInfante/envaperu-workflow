"""Semilla UAT local, aislada e idempotente para Alcancia Pablo.

Este modulo no es un cargador de datos productivos. La guardia explicita limita
su uso a bases PostgreSQL locales cuyos nombres estan reservados para desarrollo.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone, timedelta
from decimal import Decimal
from hashlib import sha256
import uuid

from sqlalchemy import select, update
from sqlalchemy.engine import make_url

from app.models.estacion_pesaje import EstacionPesaje
from app.models.maquina import Maquina, TipoMaquina
from app.models.molde import Molde, MoldePieza, Pieza
from app.models.producto import (
    ColorBase,
    ColorProduccion,
    Familia,
    FamiliaColor,
    Linea,
    LineaFamilia,
    PiezaColor,
    ProductoPieza,
    ProductoTerminado,
)
from app.models.registro import RegistroDiarioProduccion
from app.models.scm_articulos import ScmArticuloPiezaColor, ScmArticuloProducto
from app.models.scm_empaque import (
    ScmArticuloPerfil,
    ScmPerfilEmpacable,
    ScmReglaEmpaque,
    ScmReglaEmpaqueRevision,
    ScmTipoContenedor,
)
from app.models.scm_ot import ScmEtiquetaManga, ScmManga, ScmTrabajoOt
from app.models.scm_production_orders import (
    ScmAsignacionDemandaSuministro,
    ScmCorridaFabricacion,
    ScmOrdenFabricacion,
    ScmOrdenOperacion,
    ScmOrdenOperacionSalida,
    ScmOrdenProduccion,
    ScmOrdenProduccionLinea,
    ScmPlanProduccion,
)
from app.models.trabajador import RolOperativo, Trabajador, trabajador_rol
from app.services.scm_configuration import ensure_initial_scm_configuration
from app.services.scm_ot_service import (
    acknowledge_station_print_job,
    add_color_work,
    create_fabrication_ot_header,
    generate_prelabels,
    recalculate_fabrication_manga_plan,
    transition_color_work,
)
from app.services.scm_weighing_service import resolve_manga_label
from app.services.station_auth import hash_station_token


ALEMBIC_HEAD = "f93d4e6a8c02"
DEMO_MARKER = "PORTFOLIO_ALCANCIA_PABLO_V2"
DEMO_OF_IDLE_CODE = "OF-DEMO-AP-SIN-OT"
DEMO_OF_ACTIVE_CODE = "OF-DEMO-AP-PRE"
DEMO_STATION_TOKEN = "local-demo-alcancia-pablo-token-v1"

_ALLOWED_DATABASES = frozenset({"enva_test", "enva_uat_alcancia"})
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class LocalDemoSeedError(RuntimeError):
    """La semilla no puede ejecutarse de manera segura o coherente."""


def _stable_uuid(suffix: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"{DEMO_MARKER}:{suffix}")


def _hash(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _utc_at(day: date, hour: int = 8) -> datetime:
    return datetime.combine(day, time(hour=hour), tzinfo=timezone.utc)


def assert_local_demo_database(
    database_url: str,
    *,
    connection_database: str,
    migration_revision: str,
) -> None:
    """Falla cerrado antes de escribir fuera de una base UAT local conocida."""

    try:
        parsed = make_url(database_url)
    except (TypeError, ValueError) as error:
        raise LocalDemoSeedError("La URL de base no es valida.") from error
    if parsed.get_backend_name() != "postgresql":
        raise LocalDemoSeedError("La semilla exige PostgreSQL local.")
    if (parsed.host or "").lower() not in _LOOPBACK_HOSTS:
        raise LocalDemoSeedError("La base debe residir en loopback.")
    configured_database = parsed.database or ""
    if configured_database not in _ALLOWED_DATABASES:
        raise LocalDemoSeedError(
            f"Base local no autorizada: {configured_database or '(vacia)'}"
        )
    if connection_database != configured_database:
        raise LocalDemoSeedError(
            "La base conectada no coincide con la URL configurada."
        )
    if migration_revision != ALEMBIC_HEAD:
        raise LocalDemoSeedError(
            "La base no tiene las migraciones requeridas en head."
        )


def _ensure_catalogs(session):
    linea = session.scalar(select(Linea).where(Linea.codigo == 9901))
    if linea is None:
        linea = Linea(codigo=9901, nombre="HOGAR - ALCANCIAS")
        session.add(linea)
    familia = session.scalar(select(Familia).where(Familia.codigo == 9901))
    if familia is None:
        familia = Familia(codigo=9901, nombre="ALCANCIAS")
        session.add(familia)
    session.flush()
    relation = session.scalar(select(LineaFamilia).where(
        LineaFamilia.linea_id == linea.id,
        LineaFamilia.familia_id == familia.id,
    ))
    if relation is None:
        session.add(LineaFamilia(linea_id=linea.id, familia_id=familia.id))

    color_family = session.scalar(
        select(FamiliaColor).where(FamiliaColor.codigo == 9901)
    )
    if color_family is None:
        color_family = FamiliaColor(codigo=9901, nombre="SOLIDO")
        session.add(color_family)
    color_base = session.scalar(
        select(ColorBase).where(ColorBase.nombre == "CARNE")
    )
    if color_base is None:
        color_base = ColorBase(nombre="CARNE")
        session.add(color_base)
    session.flush()
    color = session.scalar(select(ColorProduccion).where(
        ColorProduccion.color_base_id == color_base.id,
        ColorProduccion.familia_color_id == color_family.id,
    ))
    if color is None:
        color = ColorProduccion(
            color_base_rel=color_base,
            familia_color_rel=color_family,
            hex_referencia="#D8A48F",
        )
        session.add(color)

    piece = session.scalar(select(Pieza).where(Pieza.codigo == "PZ-DEMO-AP"))
    if piece is None:
        piece = Pieza(
            codigo="PZ-DEMO-AP",
            nombre="Cuerpo Alcancia Pablo Grande",
            linea_id=linea.id,
            familia_id=familia.id,
            peso_nominal_gr=125,
            activo=True,
        )
        session.add(piece)
    mold = session.get(Molde, "ML-DEMO-AP")
    if mold is None:
        mold = Molde(
            codigo="ML-DEMO-AP",
            nombre="Molde soplado Alcancia Pablo Grande",
            peso_tiro_gr=130,
            tiempo_ciclo_std=30,
            activo=True,
            notas=DEMO_MARKER,
        )
        session.add(mold)
    session.flush()
    mold_piece = session.scalar(select(MoldePieza).where(
        MoldePieza.molde_id == mold.codigo,
        MoldePieza.pieza_id == piece.id,
    ))
    if mold_piece is None:
        session.add(MoldePieza(
            molde=mold,
            pieza=piece,
            cavidades=1,
            peso_unitario_gr=125,
            activo=True,
        ))
    piece_color = session.get(PiezaColor, "PC-DEMO-AP-CARNE")
    if piece_color is None:
        piece_color = PiezaColor(
            sku="PC-DEMO-AP-CARNE",
            linea_id=linea.id,
            familia_id=familia.id,
            pieza_rel=piece,
            piezas="Alcancia Pablo Grande CARNE SOLIDO",
            color_produccion_rel=color,
            peso=125,
            tipo_extruccion="SOPLADO",
            estado_revision="VERIFICADO",
            notas_revision=DEMO_MARKER,
        )
        session.add(piece_color)
    product = session.get(ProductoTerminado, "PT-DEMO-AP")
    if product is None:
        product = ProductoTerminado(
            cod_sku_pt="PT-DEMO-AP",
            producto="Alcancia Pablo Grande",
            linea_id=linea.id,
            familia_id=familia.id,
            um="UN",
            peso_g=125,
            status="ACTIVO",
            estado_revision="VERIFICADO",
            obs=DEMO_MARKER,
        )
        session.add(product)
    session.flush()
    composition = session.scalar(select(ProductoPieza).where(
        ProductoPieza.producto_terminado_id == product.cod_sku_pt,
        ProductoPieza.pieza_sku == piece_color.sku,
    ))
    if composition is None:
        session.add(ProductoPieza(
            producto_terminado_id=product.cod_sku_pt,
            pieza_sku=piece_color.sku,
            cantidad=1,
        ))
    session.flush()
    article_link = session.scalar(select(ScmArticuloPiezaColor).where(
        ScmArticuloPiezaColor.pieza_color_sku == piece_color.sku
    ))
    product_article_link = session.scalar(select(ScmArticuloProducto).where(
        ScmArticuloProducto.producto_terminado_id == product.cod_sku_pt
    ))
    if article_link is None or product_article_link is None:
        raise LocalDemoSeedError("No se crearon los articulos SCM del demo.")
    return {
        "linea": linea,
        "familia": familia,
        "color": color,
        "piece": piece,
        "piece_color": piece_color,
        "product": product,
        "mold": mold,
        "piece_article": article_link.articulo,
        # En este harness la Alcancia de una sola pieza sale terminada de la
        # OF (soplado + acabado en el mismo proceso). Asi la asignacion de
        # demanda OP -> salida OF es terminal y no simula una PiezaColor como
        # si ya fuese PT. El maestro PC permanece disponible para otras UAT.
        "article": product_article_link.articulo,
    }


def _ensure_actor_machine_station(session):
    ensure_initial_scm_configuration()
    actor = session.scalar(select(Trabajador).where(
        Trabajador.codigo == "TRB-DEMO-AP"
    ))
    if actor is None:
        actor = Trabajador(
            codigo="TRB-DEMO-AP",
            nombres="Diego",
            apellidos="Ramos",
            nombre_corto="Maquinista",
            activo=True,
            observaciones=DEMO_MARKER,
        )
        session.add(actor)
    roles = session.scalars(select(RolOperativo).where(
        RolOperativo.codigo.in_(("GERENTE_GENERAL", "MAQUINISTA"))
    )).all()
    for role in roles:
        if role not in actor.roles:
            actor.roles.append(role)
    if not {role.codigo for role in actor.roles}.issuperset(
        {"GERENTE_GENERAL", "MAQUINISTA"}
    ):
        raise LocalDemoSeedError("Faltan roles SCM para el operador del recorrido.")
    session.flush()
    manager_role = next(
        role for role in roles if role.codigo == "GERENTE_GENERAL"
    )
    # La relacion ORM conserva ambos roles funcionales. Esta marca explicita
    # evita que /auth/me trate al actor del recorrido como configuracion pendiente.
    session.execute(
        update(trabajador_rol)
        .where(trabajador_rol.c.trabajador_id == actor.id)
        .values(es_principal=False)
    )
    session.execute(
        update(trabajador_rol)
        .where(
            trabajador_rol.c.trabajador_id == actor.id,
            trabajador_rol.c.rol_operativo_id == manager_role.id,
        )
        .values(es_principal=True)
    )
    session.expire(actor, ["rol_principal"])

    machine_type = session.scalar(select(TipoMaquina).where(
        TipoMaquina.codigo == "SOPLADO-DEMO"
    ))
    if machine_type is None:
        machine_type = TipoMaquina(
            codigo="SOPLADO-DEMO",
            nombre="Sopladora",
            proceso="SOPLADO",
            activo=True,
        )
        session.add(machine_type)
    session.flush()
    machine = session.scalar(select(Maquina).where(
        Maquina.codigo == "MAQ-DEMO-AP"
    ))
    if machine is None:
        machine = Maquina(
            codigo="MAQ-DEMO-AP",
            nombre="Sopladora Alcancia Pablo",
            tipo_maquina_id=machine_type.id,
            estado="OPERATIVA",
            activo=True,
            observaciones=DEMO_MARKER,
        )
        session.add(machine)
    station_id = str(_stable_uuid("station"))
    station = session.get(EstacionPesaje, station_id)
    if station is None:
        station = EstacionPesaje(
            station_id=station_id,
            codigo="BAL-DEMO-AP",
            nombre="Balanza Alcancia Pablo",
            ubicacion="Area de pesaje",
            estado_admin="ACTIVA",
            token_hash=hash_station_token(DEMO_STATION_TOKEN),
        )
        session.add(station)
    session.flush()
    if not actor.tiene_capacidad("MANGA_PESAR"):
        raise LocalDemoSeedError("El operador del recorrido no posee MANGA_PESAR.")
    return actor, machine, station


def _ensure_packaging(session, *, actor, article):
    container = session.scalar(select(ScmTipoContenedor).where(
        ScmTipoContenedor.codigo == "MANGA-DEMO-AP"
    ))
    if container is None:
        container = ScmTipoContenedor(
            codigo="MANGA-DEMO-AP",
            clase="MANGA",
            nombre="Manga Alcancia Pablo",
            material="PE",
            tara_nominal_g=Decimal("10"),
            tolerancia_tara_g=Decimal("5"),
            peso_bruto_max_kg=Decimal("20"),
            activo=True,
        )
        session.add(container)
    profile = session.scalar(select(ScmPerfilEmpacable).where(
        ScmPerfilEmpacable.codigo == "PERFIL-DEMO-AP"
    ))
    if profile is None:
        profile = ScmPerfilEmpacable(
            codigo="PERFIL-DEMO-AP",
            nombre="Alcancia Pablo 67 unidades",
            descripcion_fisica=DEMO_MARKER,
            activo=True,
        )
        session.add(profile)
    session.flush()
    link = session.scalar(select(ScmArticuloPerfil).where(
        ScmArticuloPerfil.articulo_id == article.id,
        ScmArticuloPerfil.perfil_empacable_id == profile.id,
    ))
    if link is None:
        link = ScmArticuloPerfil(
            articulo_id=article.id,
            perfil_empacable_id=profile.id,
            es_predeterminado=True,
            activo=True,
        )
        session.add(link)
    rule = session.scalar(select(ScmReglaEmpaque).where(
        ScmReglaEmpaque.perfil_empacable_id == profile.id,
        ScmReglaEmpaque.tipo_contenedor_id == container.id,
    ))
    if rule is None:
        rule = ScmReglaEmpaque(
            perfil_empacable_id=profile.id,
            tipo_contenedor_id=container.id,
        )
        session.add(rule)
    session.flush()
    revision = session.scalar(select(ScmReglaEmpaqueRevision).where(
        ScmReglaEmpaqueRevision.regla_id == rule.id,
        ScmReglaEmpaqueRevision.estado == "APROBADA",
    ))
    if revision is None:
        revision = ScmReglaEmpaqueRevision(
            regla_id=rule.id,
            numero_revision=1,
            estado="APROBADA",
            medicion_fisica_probada=True,
            cantidad_objetivo_un=67,
            cantidad_maxima_probada_un=67,
            peso_neto_operativo_max_kg=Decimal("8.375"),
            margen_seguridad_kg=Decimal("0"),
            tolerancia_peso_abs_g=Decimal("25"),
            tolerancia_peso_pct=Decimal("1"),
            tara_nominal_g_snapshot=container.tara_nominal_g,
            tolerancia_tara_g_snapshot=container.tolerancia_tara_g,
            peso_bruto_max_kg_snapshot=container.peso_bruto_max_kg,
            notas=DEMO_MARKER,
            content_hash=_hash(f"{DEMO_MARKER}:packaging:67"),
            creada_por_id=actor.id,
            aprobada_por_id=actor.id,
            aprobada_at=datetime.now(timezone.utc),
        )
        session.add(revision)
    session.flush()


def _ensure_demand_and_orders(session, *, actor, machine, catalogs, operational_date):
    demand = session.scalar(select(ScmOrdenProduccion).where(
        ScmOrdenProduccion.referencia_origen == DEMO_MARKER
    ))
    created = demand is None
    if demand is None:
        demand = ScmOrdenProduccion(
            codigo="OP-DEMO-AP-2400",
            origen="DEMO_LOCAL",
            referencia_origen=DEMO_MARKER,
            fecha_necesidad=operational_date + timedelta(days=4),
            prioridad="NORMAL",
            estado="PLANIFICADA",
            created_by_id=actor.id,
            approved_by_id=actor.id,
            approved_at=_utc_at(operational_date, 7),
        )
        demand.lineas.append(ScmOrdenProduccionLinea(
            producto_terminado_id=catalogs["product"].cod_sku_pt,
            cantidad_solicitada=Decimal("2400"),
            fecha_necesidad=operational_date + timedelta(days=4),
            estado="ACTIVA",
        ))
        session.add(demand)
        session.flush()
    line = demand.lineas[0]
    plan = session.scalar(select(ScmPlanProduccion).where(
        ScmPlanProduccion.orden_produccion_id == demand.id,
        ScmPlanProduccion.revision == 1,
    ))
    if plan is None:
        proposal = {
            "marker": DEMO_MARKER,
            "documentos": [
                {"clave": "DEMO-OF-IDLE", "cantidad": 2266},
                {"clave": "DEMO-OF-ACTIVE", "cantidad": 134},
            ],
        }
        plan = ScmPlanProduccion(
            orden_produccion_id=demand.id,
            revision=1,
            estado="CONFIRMADO",
            input_hash=_hash(f"{DEMO_MARKER}:input"),
            content_hash=_hash(f"{DEMO_MARKER}:content"),
            propuesta_json=proposal,
            calculado_por_id=actor.id,
            confirmado_por_id=actor.id,
            operation_id=_stable_uuid("plan"),
            confirmado_at=_utc_at(operational_date, 7),
        )
        session.add(plan)
        session.flush()

    def ensure_of(code, proposal_key, quantity):
        order = session.scalar(select(ScmOrdenOperacion).where(
            ScmOrdenOperacion.codigo == code
        ))
        if order is None:
            order = ScmOrdenOperacion(
                codigo=code,
                tipo="FABRICACION",
                origen_demanda="PLANIFICADA",
                motivo=f"{DEMO_MARKER}: escenario local",
                plan_produccion_id=plan.id,
                propuesta_clave=proposal_key,
                estado="LIBERADA",
                created_by_id=actor.id,
                released_by_id=actor.id,
                released_at=_utc_at(operational_date, 7),
            )
            fabrication = ScmOrdenFabricacion(
                orden_operacion=order,
                molde_id=catalogs["mold"].codigo,
                maquina_prevista_id=machine.id,
                snapshot_tiempo_ciclo_seg=Decimal("30"),
                snapshot_horas_turno=Decimal("8"),
                snapshot_peso_colada_gr=Decimal("5"),
            )
            run = ScmCorridaFabricacion(
                orden_fabricacion=fabrication,
                codigo=f"{code}-C01",
                secuencia=1,
                color_produccion_id=catalogs["color"].id,
                ciclos_objetivo=quantity,
                estado="LIBERADA",
            )
            output = ScmOrdenOperacionSalida(
                orden_operacion=order,
                corrida_fabricacion=run,
                articulo_scm_id=catalogs["article"].id,
                cantidad_por_ciclo_snapshot=Decimal("1"),
                peso_unitario_snapshot_g=Decimal("125"),
                cantidad_objetivo=Decimal(quantity),
                kg_estandar_objetivo=Decimal(quantity) * Decimal("0.125"),
            )
            session.add_all([order, fabrication, run, output])
            session.flush()
        output = order.salidas[0]
        allocation = session.scalar(select(ScmAsignacionDemandaSuministro).where(
            ScmAsignacionDemandaSuministro.orden_produccion_linea_id == line.id,
            ScmAsignacionDemandaSuministro.orden_operacion_salida_id == output.id,
        ))
        if allocation is None:
            session.add(ScmAsignacionDemandaSuministro(
                orden_produccion_linea_id=line.id,
                fuente_tipo="SALIDA_ORDEN",
                orden_operacion_salida_id=output.id,
                cantidad_planificada=Decimal(quantity),
                cantidad_comprometida=Decimal("0"),
                cantidad_satisfecha=Decimal("0"),
                estado="PLANIFICADA",
                operation_id=_stable_uuid(f"allocation:{code}"),
            ))
        return order, order.fabricacion.corridas[0]

    idle, _ = ensure_of(DEMO_OF_IDLE_CODE, "DEMO-OF-IDLE", 2266)
    active, active_run = ensure_of(
        DEMO_OF_ACTIVE_CODE, "DEMO-OF-ACTIVE", 134
    )
    session.commit()
    return created, demand, idle, active, active_run


def _result(session, *, created, actor, station, active_order):
    ot = session.scalar(select(RegistroDiarioProduccion).where(
        RegistroDiarioProduccion.tipo_ot == "FABRICACION",
        RegistroDiarioProduccion.codigo_ot_sintetico.is_(False),
        RegistroDiarioProduccion.trabajos_ot.any(
            ScmTrabajoOt.orden_operacion_id == active_order.id
        ),
    ))
    if ot is None:
        return None
    work = session.scalar(select(ScmTrabajoOt).where(
        ScmTrabajoOt.orden_trabajo_id == ot.id,
        ScmTrabajoOt.orden_operacion_id == active_order.id,
    ))
    labels = session.scalars(
        select(ScmEtiquetaManga)
        .join(ScmManga)
        .where(ScmManga.trabajo_ot_id == work.id)
        .order_by(ScmEtiquetaManga.id)
    ).all() if work is not None else []
    if (
        work is None
        or work.estado != "EN_EJECUCION"
        or len(labels) != 2
        or any(label.estado != "IMPRESA" for label in labels)
    ):
        return None
    resolutions = [
        resolve_manga_label(session, label_id=label.public_id)
        for label in labels
    ]
    if not all(item["can_weigh"] for item in resolutions):
        raise LocalDemoSeedError("Los QR del recorrido no quedaron habilitados para pesaje.")
    return {
        "marker": DEMO_MARKER,
        "created": created,
        "op_id": str(active_order.plan_produccion.orden_produccion_id),
        "of_idle_id": str(session.scalar(select(ScmOrdenOperacion.id).where(
            ScmOrdenOperacion.codigo == DEMO_OF_IDLE_CODE
        ))),
        "of_active_id": str(active_order.id),
        "ot_id": str(ot.public_id),
        "trabajo_color_id": str(work.id),
        "operator_id": actor.id,
        "station_id": station.station_id,
        "station_token": DEMO_STATION_TOKEN,
        "label_ids": [str(label.public_id) for label in labels],
        "qr_json": [label.payload_json["qr"] for label in labels],
    }


def seed_alcancia_pablo_demo(
    session,
    *,
    database_url: str,
    connection_database: str,
    migration_revision: str,
    operational_date: date,
    validate_environment: bool = True,
):
    """Crea dos pistas UAT enlazadas a una OP, sin registrar pesajes."""

    if validate_environment:
        assert_local_demo_database(
            database_url,
            connection_database=connection_database,
            migration_revision=migration_revision,
        )
    catalogs = _ensure_catalogs(session)
    actor, machine, station = _ensure_actor_machine_station(session)
    _ensure_packaging(session, actor=actor, article=catalogs["article"])
    session.commit()
    created, _demand, _idle, active, run = _ensure_demand_and_orders(
        session,
        actor=actor,
        machine=machine,
        catalogs=catalogs,
        operational_date=operational_date,
    )
    complete = _result(
        session,
        created=created,
        actor=actor,
        station=station,
        active_order=active,
    )
    if complete is not None:
        return complete

    plan = recalculate_fabrication_manga_plan(
        session,
        actor_id=actor.id,
        order_id=active.id,
        operation_id=_stable_uuid("manga-plan"),
        data={},
    )["plan"]
    header = create_fabrication_ot_header(
        session,
        actor_id=actor.id,
        operation_id=_stable_uuid("ot-header"),
        data={
            "maquina_id": machine.id,
            "fecha_operativa": operational_date.isoformat(),
            "turno": "DIA",
            "maquinista_predeterminado_id": actor.id,
        },
    )["ot"]
    work_payload = add_color_work(
        session,
        actor_id=actor.id,
        ot_id=uuid.UUID(header["public_id"]),
        operation_id=_stable_uuid("color-work"),
        data={
            "corrida_fabricacion_id": str(run.id),
            "maquinista_id": actor.id,
            "asignaciones": [{
                "plan_linea_id": plan["lineas"][0]["id"],
                "cantidad_un": 134,
            }],
        },
    )
    work = work_payload["trabajo_color"]
    mangas = work_payload["mangas"]
    generated = generate_prelabels(
        session,
        actor_id=actor.id,
        manga_id=uuid.UUID(mangas[0]["public_id"]),
        operation_id=_stable_uuid("prelabels"),
        data={"manga_ids": [item["public_id"] for item in mangas]},
    )
    acknowledge_station_print_job(
        session,
        station_id=station.station_id,
        print_job_id=uuid.UUID(generated["print_job_id"]),
        data={"results": [{
            "label_id": label["public_id"],
            "estado": "IMPRESA",
            "printer_name": "TSC_SIMULADA",
        } for label in generated["labels"]]},
    )
    transition_color_work(
        session,
        actor_id=actor.id,
        work_id=uuid.UUID(work["id"]),
        operation_id=_stable_uuid("start-work"),
        data={"version": work["version"]},
        action="iniciar",
    )
    result = _result(
        session,
        created=created,
        actor=actor,
        station=station,
        active_order=active,
    )
    if result is None:
        raise LocalDemoSeedError("El escenario de demostracion quedo incompleto.")
    return result
