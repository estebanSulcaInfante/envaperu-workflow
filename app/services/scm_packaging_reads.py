"""Bounded, scalar-only read projection for the packaging assignments table."""
import base64
import binascii
import json

from sqlalchemy import and_, func, or_, select

from app.models.scm_articulos import ScmArticulo
from app.models.scm_empaque import ScmArticuloPerfil, ScmPerfilEmpacable
from app.services.scm_service_support import ScmServiceError, load_actor


def _invalid():
    return ScmServiceError('INVALID_PACKAGING_PAGE', 'Los filtros o el cursor de empaque no son válidos.', status_code=400)


def _page_input(query, limit, cursor, actor_id):
    query = (query or '').strip()
    try:
        limit = int(limit)
    except (ValueError, TypeError):
        raise _invalid() from None
    if not 1 <= limit <= 100 or len(query) > 200:
        raise _invalid()
    key = None
    if cursor:
        try:
            if len(cursor) > 2048:
                raise ValueError()
            raw = base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True)
            values = json.loads(raw)
            if not isinstance(values, list) or len(values) != 5:
                raise ValueError()
            version, scope, search, code, article_id = values
            if (type(version) is not int or version != 1 or type(scope) is not int or scope != actor_id or search != query
                    or not isinstance(code, str) or not 1 <= len(code) <= 64
                    or type(article_id) is not int or article_id <= 0):
                raise ValueError()
            key = (code, article_id)
        except (ValueError, TypeError, UnicodeError, binascii.Error):
            raise _invalid() from None
    return query, limit, key


def _cursor(query, actor_id, row):
    payload = [1, actor_id, query, row['article_code'], row['article_id']]
    return base64.urlsafe_b64encode(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()).decode().rstrip('=')


def list_packaging_assignments(session, *, actor_id, query=None, limit=25, cursor=None):
    # The old table combined /articulos and /articulos/{id}/perfiles-empaque.
    # Preserve both capabilities rather than broadening the article projection.
    load_actor(session, actor_id, capability='EMPAQUE_VER')
    load_actor(session, actor_id, capability='ARTICULO_VER')
    query, limit, key = _page_input(query, limit, cursor, actor_id)
    filters = [ScmArticuloPerfil.activo.is_(True), ScmArticuloPerfil.es_predeterminado.is_(True)]
    if query:
        literal = query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        filters.append(or_(*[
            column.ilike(f'%{literal}%', escape='\\') for column in (
                ScmArticulo.codigo, ScmArticulo.nombre, ScmArticulo.clase,
                ScmPerfilEmpacable.codigo, ScmPerfilEmpacable.nombre,
                ScmPerfilEmpacable.descripcion_fisica,
            )
        ]))
    base = (select(
        ScmArticuloPerfil.id.label('id'),
        ScmArticulo.id.label('article_id'), ScmArticulo.codigo.label('article_code'),
        ScmArticulo.nombre.label('article_name'), ScmArticulo.clase.label('article_class'),
        ScmArticulo.version.label('article_version'), ScmArticulo.activo.label('article_active'),
        ScmPerfilEmpacable.id.label('profile_id'), ScmPerfilEmpacable.codigo.label('profile_code'),
        ScmPerfilEmpacable.nombre.label('profile_name'),
        ScmPerfilEmpacable.descripcion_fisica.label('profile_description'),
        ScmPerfilEmpacable.activo.label('profile_active'),
    ).select_from(ScmArticuloPerfil)
        .join(ScmArticulo, ScmArticulo.id == ScmArticuloPerfil.articulo_id)
        .join(ScmPerfilEmpacable, ScmPerfilEmpacable.id == ScmArticuloPerfil.perfil_empacable_id)
        .where(*filters))
    total = session.scalar(base.with_only_columns(func.count()))
    if key:
        code, article_id = key
        base = base.where(or_(ScmArticulo.codigo > code, and_(ScmArticulo.codigo == code, ScmArticulo.id > article_id)))
    rows = session.execute(base.order_by(ScmArticulo.codigo, ScmArticulo.id).limit(limit + 1)).mappings().all()
    has_more = len(rows) > limit
    page = rows[:limit]
    items = [{
        'id': row['id'],
        'articulo': {'id': row['article_id'], 'codigo': row['article_code'], 'nombre': row['article_name'], 'clase': row['article_class'], 'version': row['article_version'], 'activo': row['article_active']},
        'perfil': {'id': row['profile_id'], 'codigo': row['profile_code'], 'nombre': row['profile_name'], 'descripcion_fisica': row['profile_description'], 'activo': row['profile_active']},
    } for row in page]
    return {'items': items, 'total': total, 'has_more': has_more, 'next_cursor': _cursor(query, actor_id, page[-1]) if has_more else None}