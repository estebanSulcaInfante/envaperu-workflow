import hashlib
import secrets
import uuid
from functools import wraps

from flask import g, jsonify, request

from app.models.estacion_pesaje import EstacionPesaje
from app.extensions import db


class StationProvisioningConflict(RuntimeError):
    pass


def hash_station_token(token):
    if not isinstance(token, str) or not token.strip():
        raise ValueError("station token is required")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def provision_station(station_id, code, name, location, token=None):
    station_id = str(uuid.UUID(str(station_id)))
    code = str(code or "").strip()
    name = str(name or "").strip()
    location = str(location or "").strip()
    if not code or not name or not location:
        raise ValueError("code, name y location son requeridos")
    if db.session.get(EstacionPesaje, station_id) is not None:
        raise StationProvisioningConflict("station_id ya registrado")
    if EstacionPesaje.query.filter_by(codigo=code).first() is not None:
        raise StationProvisioningConflict("codigo ya registrado")

    clear_token = token or secrets.token_urlsafe(32)
    station = EstacionPesaje(
        station_id=station_id,
        codigo=code,
        nombre=name,
        ubicacion=location,
        estado_admin="ACTIVA",
        token_hash=hash_station_token(clear_token),
    )
    db.session.add(station)
    db.session.commit()
    return station, clear_token


def _error(status, code, message):
    return jsonify({"code": code, "message": message}), status


def _bearer_token():
    authorization = request.headers.get("Authorization", "")
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def require_station_auth(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        token = _bearer_token()
        if token is None:
            return _error(401, "AUTH_REQUIRED", "Bearer token de estacion requerido")

        station = EstacionPesaje.query.filter_by(
            token_hash=hash_station_token(token)
        ).one_or_none()
        if station is None:
            return _error(401, "INVALID_TOKEN", "Credencial de estacion invalida")
        if station.estado_admin == "RETIRADA":
            return _error(403, "STATION_RETIRED", "La estacion fue retirada")

        version = request.headers.get("X-Station-Version", "").strip()
        if not version:
            return _error(
                400,
                "STATION_VERSION_REQUIRED",
                "X-Station-Version es requerido",
            )
        correlation_id = request.headers.get("X-Correlation-Id", "").strip()
        try:
            uuid.UUID(correlation_id)
        except (ValueError, TypeError, AttributeError):
            return _error(
                400,
                "CORRELATION_ID_INVALID",
                "X-Correlation-Id debe ser UUID",
            )

        g.authenticated_station = station
        g.station_version = version
        g.correlation_id = correlation_id
        return view(*args, **kwargs)

    return wrapped
