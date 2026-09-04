"""Apply only the approved M4 capability to the existing local UAT."""
import os
import sys
from pathlib import Path

from sqlalchemy.engine import make_url

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
url = make_url(os.environ["DATABASE_URL"])
if url.host not in {"127.0.0.1", "localhost"} or url.database != "enva_uat_recorrido":
    raise RuntimeError("This command only targets enva_uat_recorrido on loopback")

from app import create_app
from app.extensions import db
from app.models.trabajador import RolOperativo
from app.models.scm_catalogos import ScmCapacidad

with create_app().app_context():
    capability = ScmCapacidad.query.filter_by(codigo="OT_ANULAR").one_or_none()
    if capability is None:
        capability = ScmCapacidad(codigo="OT_ANULAR", nombre="Anular OT de fabricación vacía")
        db.session.add(capability)
    for code in ("SUPERVISOR", "JEFE_PRODUCCION", "GERENTE_GENERAL"):
        role = RolOperativo.query.filter_by(codigo=code).one()
        if not any(item.codigo == "OT_ANULAR" for item in role.capacidades):
            role.capacidades.append(capability)
    db.session.commit()
    print("M4: OT_ANULAR enabled for three approved roles; no business documents modified.")
