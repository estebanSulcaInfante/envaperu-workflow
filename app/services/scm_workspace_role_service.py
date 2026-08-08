from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.models.scm_auditoria import ScmEvento
from app.models.scm_catalogos import ScmCapacidad, scm_rol_capacidad
from app.models.trabajador import (
    RolOperativo,
    ScmRolWorkspacePreferencia,
    Trabajador,
    trabajador_rol,
)
from app.services.scm_service_support import (
    ScmServiceError,
    actor_snapshot,
    load_actor,
    reject_unknown_fields,
    required_text,
    stable_code,
)


ADMIN_CAPABILITY = "AUTORIZACION_SCM_ADMINISTRAR"
ROLE_CREATE_FIELDS = {
    "codigo",
    "nombre",
    "activo",
    "capacidad_codigos",
    "workspace_focus",
    "workspace_start_feature",
    "workspace_preferencias",
}
ROLE_UPDATE_FIELDS = ROLE_CREATE_FIELDS | {"expected_version"}
ROLE_MUTABLE_FIELDS = ROLE_CREATE_FIELDS - {"codigo"}


def _load_admin(session, actor_id):
    actor = load_actor(session, actor_id)
    authorized = session.scalar(
        select(ScmCapacidad.id)
        .join(
            scm_rol_capacidad,
            scm_rol_capacidad.c.capacidad_id == ScmCapacidad.id,
        )
        .join(
            RolOperativo,
            RolOperativo.id == scm_rol_capacidad.c.rol_operativo_id,
        )
        .join(
            trabajador_rol,
            trabajador_rol.c.rol_operativo_id == RolOperativo.id,
        )
        .where(
            trabajador_rol.c.trabajador_id == actor.id,
            RolOperativo.activo.is_(True),
            ScmCapacidad.codigo == ADMIN_CAPABILITY,
            ScmCapacidad.activo.is_(True),
        )
    )
    if authorized is None:
        raise ScmServiceError(
            "CAPABILITY_REQUIRED",
            f"El actor requiere la capacidad {ADMIN_CAPABILITY}.",
            status_code=403,
            details={"capability": ADMIN_CAPABILITY},
        )
    return actor


def load_workspace_admin(session, *, actor_id):
    """Public defense-in-depth entry point for catalog administration."""
    return _load_admin(session, actor_id)


def _optional_text(value, *, field, max_length):
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        raise ScmServiceError(
            "INVALID_TEXT_FIELD",
            f"El campo {field} debe ser texto o null.",
            status_code=400,
            details={"field": field},
        )
    normalized = value.strip()
    if len(normalized) > max_length:
        raise ScmServiceError(
            "FIELD_TOO_LONG",
            f"El campo {field} supera la longitud permitida.",
            status_code=400,
            details={"field": field, "max_length": max_length},
        )
    return normalized


def _active_value(value, *, default):
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ScmServiceError(
            "INVALID_ACTIVE_FLAG",
            "El campo activo debe ser booleano.",
            status_code=400,
        )
    return value


def _capability_codes(value, *, required):
    if value is None and not required:
        return None
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip()
        for item in value
    ):
        raise ScmServiceError(
            "INVALID_CAPABILITY",
            "capacidad_codigos debe ser una lista de codigos.",
            status_code=400,
        )
    return sorted({item.strip().upper() for item in value})


def _resolve_capabilities(session, codes):
    if codes is None:
        return None
    capabilities = session.scalars(
        select(ScmCapacidad).where(ScmCapacidad.codigo.in_(codes))
    ).all() if codes else []
    active_by_code = {
        capability.codigo: capability
        for capability in capabilities
        if capability.activo
    }
    invalid = sorted(set(codes) - set(active_by_code))
    if invalid:
        raise ScmServiceError(
            "INVALID_CAPABILITY",
            "Una o mas capacidades no existen o estan inactivas.",
            status_code=400,
            details={"codes": invalid},
        )
    return [active_by_code[code] for code in codes]


def _workspace_preferences(value, *, required):
    if value is None and not required:
        return None
    if not isinstance(value, list):
        raise ScmServiceError(
            "INVALID_WORKSPACE_PREFERENCE",
            "workspace_preferencias debe ser una lista.",
            status_code=400,
        )

    normalized = []
    seen = set()
    for item in value:
        if not isinstance(item, dict):
            raise ScmServiceError(
                "INVALID_WORKSPACE_PREFERENCE",
                "Cada preferencia debe ser un objeto.",
                status_code=400,
            )
        reject_unknown_fields(
            item,
            allowed={"feature_key", "prioridad", "fijada"},
        )
        feature_key = required_text(
            item.get("feature_key"),
            field="feature_key",
            max_length=80,
        )
        priority = item.get("prioridad")
        if (
            not isinstance(priority, int)
            or isinstance(priority, bool)
            or not 0 <= priority <= 999
        ):
            raise ScmServiceError(
                "INVALID_WORKSPACE_PRIORITY",
                "prioridad debe ser un entero entre 0 y 999.",
                status_code=400,
                details={"feature_key": feature_key},
            )
        pinned = item.get("fijada", False)
        if not isinstance(pinned, bool):
            raise ScmServiceError(
                "INVALID_WORKSPACE_PIN",
                "fijada debe ser booleana.",
                status_code=400,
                details={"feature_key": feature_key},
            )
        if feature_key in seen:
            raise ScmServiceError(
                "DUPLICATE_WORKSPACE_PREFERENCE",
                "Una funcion no puede repetirse en las preferencias.",
                status_code=400,
                details={"feature_key": feature_key},
            )
        seen.add(feature_key)
        normalized.append({
            "feature_key": feature_key,
            "prioridad": priority,
            "fijada": pinned,
        })
    return sorted(normalized, key=lambda item: (
        item["prioridad"], item["feature_key"]
    ))


def _replace_preferences(role, preferences, *, actor_id, session):
    if preferences is None:
        return
    existing = {
        item.feature_key: item
        for item in role.workspace_preferencias
    }
    desired_keys = {item["feature_key"] for item in preferences}
    for feature_key, preference in existing.items():
        if feature_key not in desired_keys:
            session.delete(preference)

    for item in preferences:
        preference = existing.get(item["feature_key"])
        if preference is None:
            preference = ScmRolWorkspacePreferencia(
                feature_key=item["feature_key"],
                created_by_id=actor_id,
            )
            role.workspace_preferencias.append(preference)
        preference.prioridad = item["prioridad"]
        preference.fijada = item["fijada"]
        preference.updated_by_id = actor_id


def _role_event(role, actor, event_type, *, before=None):
    return ScmEvento(
        aggregate_type="ROL_OPERATIVO",
        aggregate_id=str(role.id),
        tipo=event_type,
        actor_id=actor.id,
        actor_snapshot=actor_snapshot(actor),
        before_json=before,
        after_json=role.to_dict(),
    )


def list_capabilities(session, *, actor_id):
    _load_admin(session, actor_id)
    items = session.scalars(
        select(ScmCapacidad).order_by(ScmCapacidad.codigo)
    ).all()
    return [item.to_dict() for item in items]


def list_roles(session, *, actor_id):
    _load_admin(session, actor_id)
    items = session.scalars(
        select(RolOperativo).order_by(RolOperativo.nombre, RolOperativo.id)
    ).unique().all()
    return [item.to_dict() for item in items]


def create_role(session, *, actor_id, data):
    try:
        actor = _load_admin(session, actor_id)
        reject_unknown_fields(data, allowed=ROLE_CREATE_FIELDS)
        code = stable_code(data.get("codigo"), max_length=20)
        name = required_text(
            data.get("nombre"), field="nombre", max_length=100
        )
        capabilities = _resolve_capabilities(
            session,
            _capability_codes(
                data.get("capacidad_codigos", []), required=True
            ),
        )
        preferences = _workspace_preferences(
            data.get("workspace_preferencias", []), required=True
        )
        if session.scalar(
            select(RolOperativo.id).where(RolOperativo.codigo == code)
        ) is not None:
            raise ScmServiceError(
                "ROLE_CODE_CONFLICT",
                "El codigo del rol ya existe.",
                status_code=409,
            )

        role = RolOperativo(
            codigo=code,
            nombre=name,
            activo=_active_value(data.get("activo"), default=True),
            capacidades=capabilities,
            workspace_focus=_optional_text(
                data.get("workspace_focus"),
                field="workspace_focus",
                max_length=1000,
            ),
            workspace_start_feature=_optional_text(
                data.get("workspace_start_feature"),
                field="workspace_start_feature",
                max_length=80,
            ),
        )
        session.add(role)
        session.flush()
        _replace_preferences(
            role, preferences, actor_id=actor.id, session=session
        )
        session.flush()
        session.expire(role, ["workspace_preferencias"])
        session.add(_role_event(role, actor, "ROL_OPERATIVO_CREADO"))
        session.commit()
        return role.to_dict()
    except ScmServiceError:
        session.rollback()
        raise
    except IntegrityError as error:
        session.rollback()
        raise ScmServiceError(
            "ROLE_CONFLICT",
            "El rol entra en conflicto con otro registro.",
            status_code=409,
        ) from error


def update_role(session, *, actor_id, role_id, data):
    try:
        actor = _load_admin(session, actor_id)
        reject_unknown_fields(data, allowed=ROLE_UPDATE_FIELDS)
        received_version = data.get("expected_version")
        if (
            not isinstance(received_version, int)
            or isinstance(received_version, bool)
            or received_version <= 0
        ):
            raise ScmServiceError(
                "VERSION_REQUIRED",
                "expected_version debe ser un entero positivo.",
                status_code=400,
            )
        role = session.scalar(
            select(RolOperativo)
            .where(RolOperativo.id == role_id)
            .with_for_update()
        )
        if role is None:
            raise ScmServiceError(
                "ROLE_NOT_FOUND",
                "El rol operativo no existe.",
                status_code=404,
            )
        if role.version != received_version:
            raise ScmServiceError(
                "VERSION_CONFLICT",
                "La version del rol esta desactualizada.",
                status_code=409,
                details={
                    "expected": role.version,
                    "received": received_version,
                },
            )
        if "codigo" in data and stable_code(
            data["codigo"], max_length=20
        ) != role.codigo:
            raise ScmServiceError(
                "IMMUTABLE_ROLE_CODE",
                "El codigo estable del rol no puede modificarse.",
                status_code=422,
            )
        if not set(data).intersection(ROLE_MUTABLE_FIELDS):
            raise ScmServiceError(
                "PATCH_FIELD_REQUIRED",
                "La solicitud no contiene campos modificables.",
                status_code=400,
            )

        before = role.to_dict()
        capabilities = _resolve_capabilities(
            session,
            _capability_codes(
                data.get("capacidad_codigos"), required=False
            ),
        )
        preferences = _workspace_preferences(
            data.get("workspace_preferencias"), required=False
        )
        if "nombre" in data:
            role.nombre = required_text(
                data["nombre"], field="nombre", max_length=100
            )
        if "activo" in data:
            role.activo = _active_value(data["activo"], default=role.activo)
        if "workspace_focus" in data:
            role.workspace_focus = _optional_text(
                data["workspace_focus"],
                field="workspace_focus",
                max_length=1000,
            )
        if "workspace_start_feature" in data:
            role.workspace_start_feature = _optional_text(
                data["workspace_start_feature"],
                field="workspace_start_feature",
                max_length=80,
            )
        if capabilities is not None:
            role.capacidades = capabilities
        _replace_preferences(
            role, preferences, actor_id=actor.id, session=session
        )
        role.version += 1
        session.flush()
        session.expire(role, ["workspace_preferencias"])
        session.add(_role_event(
            role,
            actor,
            "ROL_OPERATIVO_ACTUALIZADO",
            before=before,
        ))
        session.commit()
        return role.to_dict()
    except ScmServiceError:
        session.rollback()
        raise
    except IntegrityError as error:
        session.rollback()
        raise ScmServiceError(
            "ROLE_CONFLICT",
            "El rol entra en conflicto con otro registro.",
            status_code=409,
        ) from error


def set_primary_role(session, *, actor_id, worker_id, role_id):
    try:
        actor = _load_admin(session, actor_id)
        worker = session.scalar(
            select(Trabajador)
            .where(Trabajador.id == worker_id)
            .with_for_update()
        )
        if worker is None:
            raise ScmServiceError(
                "WORKER_NOT_FOUND",
                "El participante no existe.",
                status_code=404,
            )
        role = session.get(RolOperativo, role_id)
        if role is None or not role.activo:
            raise ScmServiceError(
                "PRIMARY_ROLE_INACTIVE",
                "El rol principal debe existir y estar activo.",
                status_code=422,
            )
        assigned = session.scalar(
            select(trabajador_rol.c.rol_operativo_id).where(
                trabajador_rol.c.trabajador_id == worker.id,
                trabajador_rol.c.rol_operativo_id == role.id,
            )
        )
        if assigned is None:
            raise ScmServiceError(
                "PRIMARY_ROLE_NOT_ASSIGNED",
                "El rol principal debe estar asignado al participante.",
                status_code=422,
            )
        previous_role_id = session.scalar(
            select(trabajador_rol.c.rol_operativo_id).where(
                trabajador_rol.c.trabajador_id == worker.id,
                trabajador_rol.c.es_principal.is_(True),
            )
        )
        if previous_role_id == role.id:
            return worker.to_dict()

        session.execute(
            update(trabajador_rol)
            .where(trabajador_rol.c.trabajador_id == worker.id)
            .values(es_principal=False)
        )
        session.execute(
            update(trabajador_rol)
            .where(
                trabajador_rol.c.trabajador_id == worker.id,
                trabajador_rol.c.rol_operativo_id == role.id,
            )
            .values(es_principal=True)
        )
        session.flush()
        session.expire(worker, ["rol_principal"])
        session.add(ScmEvento(
            aggregate_type="TRABAJADOR_ROL_PRINCIPAL",
            aggregate_id=str(worker.id),
            tipo="ROL_PRINCIPAL_DEFINIDO",
            actor_id=actor.id,
            actor_snapshot=actor_snapshot(actor),
            before_json={"rol_operativo_id": previous_role_id},
            after_json={"rol_operativo_id": role.id},
        ))
        session.commit()
        return worker.to_dict()
    except ScmServiceError:
        session.rollback()
        raise
    except IntegrityError as error:
        session.rollback()
        raise ScmServiceError(
            "PRIMARY_ROLE_CONFLICT",
            "No se pudo definir un unico rol principal.",
            status_code=409,
        ) from error
