"""Bandeja y reglas versionadas de alertas operativas SCM."""

import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from app.models.scm_reproceso import (
    ScmAlertaEvento,
    ScmAlertaOperativa,
    ScmReglaAlerta,
    ScmReglaAlertaRevision,
)
from app.models.scm_ot import ScmManga, ScmPesajeManga
from app.models.scm_warehouse import ScmExistenciaManga
from app.services.scm_service_support import (
    ScmServiceError,
    load_actor,
    positive_kg,
    required_text,
    stable_code,
)


def utc_now():
    return datetime.now(timezone.utc)


def list_alert_rules(session, *, actor_id):
    load_actor(session, actor_id, capability="ALERTA_VER")
    items = session.scalars(
        select(ScmReglaAlerta).order_by(ScmReglaAlerta.codigo)
    ).all()
    return {"items": [item.to_dict() for item in items]}


def create_alert_rule_revision(session, *, actor_id, rule_code, data):
    actor = load_actor(session, actor_id, capability="ALERTA_CONFIGURAR")
    code = stable_code(rule_code)
    rule = session.scalar(select(ScmReglaAlerta).where(ScmReglaAlerta.codigo == code))
    if rule is None:
        rule = ScmReglaAlerta(
            codigo=code,
            nombre=required_text(data.get("nombre"), field="nombre", max_length=160),
            descripcion=(data.get("descripcion") or "").strip() or None,
        )
        session.add(rule)
        session.flush()
    current = max((item.revision for item in rule.revisiones), default=0)
    unit = stable_code(data.get("unidad"), field="unidad", max_length=24)
    severity = stable_code(data.get("severidad"), field="severidad", max_length=20)
    if unit not in {"HORAS", "DIAS_CALENDARIO", "KG", "PORCENTAJE"}:
        raise ScmServiceError("INVALID_ALERT_UNIT", "La unidad de alerta no es valida.", status_code=422)
    if severity not in {"INFO", "ADVERTENCIA", "CRITICA"}:
        raise ScmServiceError("INVALID_ALERT_SEVERITY", "La severidad no es valida.", status_code=422)
    threshold = positive_kg(data.get("umbral"), field="umbral")
    revision = ScmReglaAlertaRevision(
        regla=rule,
        revision=current + 1,
        umbral=threshold,
        unidad=unit,
        severidad=severity,
        alcance=stable_code(data.get("alcance") or "PRODUCCION", field="alcance", max_length=40),
        creado_por_id=actor.id,
    )
    session.add(revision)
    session.commit()
    return rule.to_dict()


def approve_alert_rule_revision(session, *, actor_id, revision_id):
    actor = load_actor(session, actor_id, capability="ALERTA_CONFIGURAR")
    revision = session.get(ScmReglaAlertaRevision, revision_id)
    if revision is None:
        raise ScmServiceError("ALERT_RULE_NOT_FOUND", "La revision de alerta no existe.", status_code=404)
    if revision.estado != "BORRADOR":
        raise ScmServiceError("INVALID_ALERT_RULE_STATE", "Solo una revision borrador puede aprobarse.", status_code=409)
    if revision.creado_por_id == actor.id:
        raise ScmServiceError("SELF_APPROVAL_NOT_ALLOWED", "El creador de la revision no puede aprobarla.", status_code=409)
    approved = session.scalars(select(ScmReglaAlertaRevision).where(
        ScmReglaAlertaRevision.regla_id == revision.regla_id,
        ScmReglaAlertaRevision.estado == "APROBADA",
    )).all()
    for item in approved:
        item.estado = "RETIRADA"
    revision.estado = "APROBADA"
    revision.aprobado_por_id = actor.id
    revision.approved_at = utc_now()
    session.commit()
    return revision.regla.to_dict()


def current_alert_rule(session, code):
    return session.scalar(
        select(ScmReglaAlertaRevision)
        .join(ScmReglaAlerta)
        .where(
            ScmReglaAlerta.codigo == code,
            ScmReglaAlerta.activo.is_(True),
            ScmReglaAlertaRevision.estado == "APROBADA",
        )
        .order_by(ScmReglaAlertaRevision.revision.desc())
    )


def upsert_operational_alert(
    session,
    *,
    rule_code,
    aggregate_type,
    aggregate_id,
    condition_key,
    summary,
    detail,
    actor_id,
):
    revision = current_alert_rule(session, rule_code)
    if revision is None:
        return None
    fingerprint = hashlib.sha256(
        f"{revision.id}|{aggregate_type}|{aggregate_id}|{condition_key}".encode("utf-8")
    ).hexdigest()
    existing = session.scalar(
        select(ScmAlertaOperativa).where(ScmAlertaOperativa.huella == fingerprint)
    )
    if existing is not None:
        return existing
    alert = ScmAlertaOperativa(
        regla_revision_id=revision.id,
        huella=fingerprint,
        tipo=rule_code,
        agregado_tipo=aggregate_type,
        agregado_id=str(aggregate_id),
        severidad=revision.severidad,
        resumen=summary[:240],
        detalle=detail,
    )
    session.add(alert)
    session.flush()
    session.add(ScmAlertaEvento(
        alerta=alert,
        tipo="CREADA",
        actor_id=actor_id,
        detalle={"regla_revision_id": revision.id},
    ))
    return alert


def evaluate_inventory_alerts(session, *, actor_id, now=None):
    """Evalua reglas de inventario; nunca mueve saldos ni custodia."""
    load_actor(session, actor_id, capability="ALERTA_GESTIONAR")
    current = now or utc_now()
    rule = current_alert_rule(session, "MANGA_PESADA_SIN_RECEPCION")
    created = []
    if rule is not None:
        cutoff = current - timedelta(hours=float(rule.umbral))
        rows = session.execute(
            select(ScmManga, ScmPesajeManga)
            .join(ScmPesajeManga, ScmPesajeManga.manga_id == ScmManga.id)
            .outerjoin(ScmExistenciaManga, ScmExistenciaManga.manga_id == ScmManga.id)
            .where(
                ScmPesajeManga.pesada_at < cutoff,
                ScmExistenciaManga.id.is_(None),
                ScmManga.estado == "PENDIENTE_RECEPCION_ALMACEN",
            )
        ).all()
        for manga, weighing in rows:
            alert = upsert_operational_alert(
                session, rule_code="MANGA_PESADA_SIN_RECEPCION",
                aggregate_type="MANGA", aggregate_id=manga.public_id,
                condition_key=f"pesaje:{weighing.public_id}",
                summary=f"{manga.codigo} sigue sin recepcion de almacen",
                detail={
                    "manga_codigo": manga.codigo,
                    "pesada_at": weighing.pesada_at.isoformat(),
                    "umbral_horas": format(rule.umbral, "f"),
                }, actor_id=actor_id,
            )
            if alert is not None:
                created.append(alert)
    session.commit()
    return {"evaluadas_at": current.isoformat(), "alertas": [item.to_dict() for item in created]}


def list_alerts(session, *, actor_id, state=None, severity=None, alert_type=None):
    load_actor(session, actor_id, capability="ALERTA_VER")
    statement = select(ScmAlertaOperativa)
    if state:
        statement = statement.where(ScmAlertaOperativa.estado == state.upper())
    if severity:
        statement = statement.where(ScmAlertaOperativa.severidad == severity.upper())
    if alert_type:
        statement = statement.where(ScmAlertaOperativa.tipo == alert_type.upper())
    items = session.scalars(statement.order_by(ScmAlertaOperativa.detectada_at.desc())).unique().all()
    counters = {"ABIERTA": 0, "RECONOCIDA": 0, "RESUELTA": 0, "DESCARTADA": 0}
    severity_counters = {"INFO": 0, "ADVERTENCIA": 0, "CRITICA": 0}
    for item in items:
        counters[item.estado] = counters.get(item.estado, 0) + 1
        severity_counters[item.severidad] = severity_counters.get(item.severidad, 0) + 1
    return {
        "items": [item.to_dict() for item in items],
        "conteos_estado": counters,
        "conteos_severidad": severity_counters,
    }


def transition_alert(session, *, actor_id, alert_id, action, data):
    actor = load_actor(session, actor_id, capability="ALERTA_GESTIONAR")
    alert = session.get(ScmAlertaOperativa, alert_id)
    if alert is None:
        raise ScmServiceError("ALERT_NOT_FOUND", "La alerta no existe.", status_code=404)
    normalized = action.upper()
    now = utc_now()
    reason = (data.get("motivo") or "").strip()
    if normalized in {"RESOLVER", "DESCARTAR"} and not reason:
        raise ScmServiceError("REASON_REQUIRED", "Debe indicar el motivo de cierre.", status_code=400)
    if normalized == "RECONOCER":
        if alert.estado != "ABIERTA":
            raise ScmServiceError("INVALID_ALERT_STATE", "Solo una alerta abierta puede reconocerse.", status_code=409)
        alert.estado = "RECONOCIDA"
        alert.reconocida_at = now
        event_type = "RECONOCIDA"
    elif normalized == "ASIGNAR":
        assignee = data.get("asignada_a_id")
        load_actor(session, assignee)
        alert.asignada_a_id = assignee
        event_type = "ASIGNADA"
    elif normalized in {"RESOLVER", "DESCARTAR"}:
        if alert.estado in {"RESUELTA", "DESCARTADA"}:
            raise ScmServiceError("INVALID_ALERT_STATE", "La alerta ya esta cerrada.", status_code=409)
        alert.estado = "RESUELTA" if normalized == "RESOLVER" else "DESCARTADA"
        alert.cerrada_at = now
        event_type = "RESUELTA" if normalized == "RESOLVER" else "DESCARTADA"
    else:
        raise ScmServiceError("INVALID_ALERT_ACTION", "La accion de alerta no es valida.", status_code=400)
    session.add(ScmAlertaEvento(
        alerta=alert,
        tipo=event_type,
        actor_id=actor.id,
        motivo=reason or None,
        detalle={"asignada_a_id": alert.asignada_a_id},
    ))
    session.commit()
    return alert.to_dict()


def seed_alert_rule(session, *, code, name, threshold, unit, severity, actor_id):
    """Semilla idempotente usada por la configuración inicial."""
    rule = session.scalar(select(ScmReglaAlerta).where(ScmReglaAlerta.codigo == code))
    if rule is not None:
        return rule
    rule = ScmReglaAlerta(codigo=code, nombre=name, activo=True)
    session.add(rule)
    session.flush()
    rule.revisiones.append(ScmReglaAlertaRevision(
        revision=1,
        estado="APROBADA",
        umbral=Decimal(str(threshold)),
        unidad=unit,
        severidad=severity,
        alcance="PRODUCCION",
        creado_por_id=actor_id,
        aprobado_por_id=actor_id,
        approved_at=utc_now(),
    ))
    return rule
