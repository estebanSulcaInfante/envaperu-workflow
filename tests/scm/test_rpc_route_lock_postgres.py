"""Real PostgreSQL locking of route selection against the retirement lock.

Uses an isolated ORM schema: this checks transaction/identity-map behaviour,
not migration triggers or permissions (covered by route service tests).
"""
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.extensions import db
from app.models.scm_articulos import ScmArticulo
from app.models.scm_rutas import ScmCentroTrabajo, ScmOperacionRuta, ScmRutaRevision
from app.models.trabajador import Trabajador
from app.services.scm_process_resolution import resolve_route_operation
from app.services.scm_route_service import _content_hash, _locked_route
from app.services.scm_service_support import ScmServiceError
from tests.scm.test_scm_migrations_postgres import _drop_isolated_schema, _isolated_postgres_url

pytestmark = pytest.mark.postgres


@pytest.fixture
def rpc_lock_context():
    admin, schema, url = _isolated_postgres_url()
    engine = create_engine(url)
    try:
        db.metadata.create_all(engine)
        with Session(engine) as session:
            actor = Trabajador(codigo="RPC-LOCK", nombres="QA", apellidos="Local", activo=True)
            article = ScmArticulo(codigo="RPC-LOCK-WIP", nombre="QA", clase="SUBENSAMBLE_WIP", unidad_base="UN", unidad_inventario="UN")
            center = ScmCentroTrabajo(codigo="RPC-LOCK-CT", nombre="Soplado QA", tipo="SOPLADO")
            session.add_all([actor, article, center])
            session.flush()
            route = ScmRutaRevision(articulo_objetivo_id=article.id, numero_revision=1, estado="APROBADA", creada_por_id=actor.id, aprobada_por_id=actor.id)
            operation = ScmOperacionRuta(clave="SOPLAR", nombre="Soplar QA", secuencia_visible=10, tipo="SOPLADO", executor_kind="OP_OT", centro_trabajo_id=center.id, articulo_salida_id=article.id)
            route.operaciones.append(operation)
            session.add(route)
            session.flush()
            route.content_hash = _content_hash(route)
            ids = route.id, operation.id
            session.commit()
        yield engine, ids
    finally:
        engine.dispose()
        _drop_isolated_schema(admin, schema)


def test_selection_holds_the_same_revision_lock_as_retirement(rpc_lock_context):
    engine, (route_id, operation_id) = rpc_lock_context
    with Session(engine) as selecting, Session(engine) as retiring:
        assert resolve_route_operation(selecting, operation_id, lock=True).tipo == "SOPLADO"
        retiring.execute(text("SET LOCAL lock_timeout = '250ms'"))
        with pytest.raises(DBAPIError) as caught:
            _locked_route(retiring, route_id)
        assert caught.value.orig.pgcode == "55P03"
        retiring.rollback()
        selecting.commit()
        # Once selection commits, the retirement path can acquire its lock.
        assert _locked_route(retiring, route_id).estado == "APROBADA"


def test_selection_refreshes_a_cached_route_after_retirement_wins(rpc_lock_context):
    engine, (route_id, operation_id) = rpc_lock_context
    with Session(engine, expire_on_commit=False) as selecting, Session(engine) as retiring:
        cached = resolve_route_operation(selecting, operation_id)
        assert cached.ruta.estado == "APROBADA"
        selecting.commit()
        route = _locked_route(retiring, route_id)
        route.estado = "RETIRADA"
        retiring.commit()
        with pytest.raises(ScmServiceError) as caught:
            resolve_route_operation(selecting, operation_id, lock=True)
        assert caught.value.code == "ROUTE_OPERATION_NOT_APPROVED"
        selecting.rollback()
        # The frozen-reference path can read the same retired revision.
        frozen = resolve_route_operation(selecting, operation_id, lock=True, allow_retired=True)
        assert frozen.ruta.estado == "RETIRADA"
