"""Aprovisiona el equipo de Mesa con una credencial recibida por stdin."""
import hashlib
import secrets
import sys
import uuid

from sqlalchemy import select

from app import create_app
from app.extensions import db
from app.models.estacion_pesaje import EstacionPesaje
from app.models.scm_catalogos import ScmCapacidad
from app.models.scm_inventory import ScmUbicacionInventario
from app.models.scm_inventory_operations import ScmAlmacenTrabajador
from app.models.trabajador import RolOperativo, Trabajador


def provision(session, token):
    if len(token) < 40 or not token.isascii():
        raise ValueError("Credencial de dispositivo inválida.")
    digest = hashlib.sha256(token.encode()).hexdigest()
    actor = session.scalar(select(Trabajador).where(Trabajador.codigo == "ESTACION-2"))
    station = session.scalar(select(EstacionPesaje).where(EstacionPesaje.codigo == "MESA-ESTACION-2"))
    role = session.scalar(select(RolOperativo).where(RolOperativo.codigo == "MESA_PIEZAS"))
    if any((actor, station, role)):
        if (actor and station and role and actor.activo and station.estado_admin == "ACTIVA"
                and secrets.compare_digest(station.token_hash, digest) and role in actor.roles):
            return
        raise RuntimeError("Hay una asignación anterior. Revísela antes de cambiar la cuenta.")
    location = session.scalar(select(ScmUbicacionInventario).where(
        ScmUbicacionInventario.codigo == "PRODUCCION_KG", ScmUbicacionInventario.activo.is_(True)))
    if not location or not location.almacen_id:
        raise RuntimeError("El saldo de piezas carece de almacén responsable.")
    names = {"INVENTARIO_VER", "INVENTARIO_AJUSTAR", "ABASTECIMIENTO_VER", "RETORNO_RECIBIR"}
    caps = session.scalars(select(ScmCapacidad).where(ScmCapacidad.activo.is_(True),
                                                       ScmCapacidad.codigo.in_(names))).all()
    if {x.codigo for x in caps} != names:
        raise RuntimeError("Faltan permisos de inventario o retorno.")
    role = RolOperativo(codigo="MESA_PIEZAS", nombre="Estación de inventario y retornos", capacidades=caps)
    actor = Trabajador(codigo="ESTACION-2", nombres="Estación", apellidos="2", nombre_corto="Estación 2", roles=[role])
    station = EstacionPesaje(station_id=str(uuid.uuid4()), codigo="MESA-ESTACION-2", nombre="Estación 2",
                             ubicacion="Mesa de piezas", estado_admin="ACTIVA", token_hash=digest)
    session.add_all([actor, station])
    session.flush()
    session.add(ScmAlmacenTrabajador(almacen_id=location.almacen_id, trabajador_id=actor.id,
                                     clases_articulo_json=["PIEZA_COLOR"], asignado_por_id=actor.id))
    session.commit()


if __name__ == "__main__":
    with create_app().app_context():
        provision(db.session, sys.stdin.readline().strip())
