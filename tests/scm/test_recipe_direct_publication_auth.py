from app.extensions import db
from app.models.trabajador import RolOperativo, Trabajador
from app.services.scm_auth import _authorize_request


def test_recipe_approval_requires_distinct_publication_capability(app, scm_config):
    with app.app_context():
        actor = Trabajador.query.filter_by(codigo="TRB-01").one()
        engineering = RolOperativo.query.filter_by(codigo="INGENIERIA_SCM").one()
        actor.roles.append(engineering)
        db.session.commit()

        with app.test_request_context(
            "/api/catalogo/recetas-color", method="POST", json={"estado": "BORRADOR"}
        ):
            assert _authorize_request(actor) is None

        with app.test_request_context(
            "/api/catalogo/recetas-color", method="POST", json={"estado": "APROBADA"}
        ):
            response, status = _authorize_request(actor)
            assert status == 403
            assert response.get_json()["error"]["code"] == "CAPABILITY_REQUIRED"

        general = RolOperativo.query.filter_by(codigo="GERENTE_GENERAL").one()
        actor.roles.append(general)
        db.session.commit()
        with app.test_request_context(
            "/api/catalogo/recetas-color", method="POST", json={"estado": "APROBADA"}
        ):
            assert _authorize_request(actor) is None
