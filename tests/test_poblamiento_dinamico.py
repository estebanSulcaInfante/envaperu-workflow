"""
Tests para el poblamiento dinámico de la BD al crear OPs:
  1. RecetaColorNormalizada: upsert y promedio ponderado acumulado
  2. Molde on-the-fly: crea Molde+MoldePieza si no existen, no sobreescribe
  3. Endpoint GET /api/catalogo/receta-color: prefill de pigmentos
"""
import pytest
from app.models.receta_color import RecetaColorNormalizada
from app.models.molde import Molde, MoldePieza
from app.models.producto import ColorProducto, FamiliaColor, Pieza, Linea, Familia
from app.models.materiales import Colorante
from app.extensions import db


# ============================================================
#  FIXTURES HELPERS
# ============================================================

def _make_color(app, nombre="AZUL", codigo=99):
    with app.app_context():
        fam = FamiliaColor.query.first() or FamiliaColor(nombre="STD")
        if not FamiliaColor.query.first():
            db.session.add(fam)
            db.session.flush()
        fam = FamiliaColor.query.first()
        c = ColorProducto.query.filter_by(nombre=nombre).first()
        if not c:
            c = ColorProducto(nombre=nombre, codigo=codigo, familia_id=fam.id)
            db.session.add(c)
            db.session.commit()
        return c.id


def _make_colorante(app, nombre="AZUL PHTALO"):
    with app.app_context():
        col = Colorante.query.filter_by(nombre=nombre).first()
        if not col:
            col = Colorante(nombre=nombre)
            db.session.add(col)
            db.session.commit()
        return col.id


# ============================================================
#  1. RecetaColorNormalizada: upsert y promedio ponderado
# ============================================================

class TestRecetaColorNormalizada:

    def test_primer_upsert_crea_receta(self, app):
        """La primera vez que se hace upsert, crea la receta con n_muestras=1."""
        color_id = _make_color(app, "VERDE TEST", 88)
        colo_id  = _make_colorante(app, "VERDE BRILLANTE")

        with app.app_context():
            RecetaColorNormalizada.upsert(
                session=db.session,
                color_id=color_id,
                colorante_id=colo_id,
                producto_sku=None,
                gr_por_kg_nuevo=0.30,
            )
            db.session.commit()

            receta = RecetaColorNormalizada.query.filter_by(
                color_id=color_id, colorante_id=colo_id, producto_sku=None
            ).first()

            assert receta is not None
            assert receta.n_muestras == 1
            assert abs(receta.gr_por_kg - 0.30) < 1e-6

    def test_segundo_upsert_hace_promedio_ponderado(self, app):
        """Dos OPs con el mismo color y distintos gr/kg → promedio (0.30 + 0.50) / 2 = 0.40."""
        color_id = _make_color(app, "NARANJA TEST", 87)
        colo_id  = _make_colorante(app, "AMARILLO NARANJA")

        with app.app_context():
            # OP 1: 0.30 gr/kg
            RecetaColorNormalizada.upsert(db.session, color_id, colo_id, None, 0.30)
            db.session.commit()

            # OP 2: 0.50 gr/kg
            RecetaColorNormalizada.upsert(db.session, color_id, colo_id, None, 0.50)
            db.session.commit()

            receta = RecetaColorNormalizada.query.filter_by(
                color_id=color_id, colorante_id=colo_id, producto_sku=None
            ).first()

            assert receta.n_muestras == 2
            assert abs(receta.gr_por_kg - 0.40) < 1e-6  # (0.30 + 0.50) / 2

    def test_receta_especifica_es_independiente_de_generica(self, app):
        """La receta con producto_sku no mezcla su promedio con la genérica (sku=None)."""
        color_id = _make_color(app, "MORADO TEST", 86)
        colo_id  = _make_colorante(app, "VIOLETA DIOXAZINA")

        with app.app_context():
            RecetaColorNormalizada.upsert(db.session, color_id, colo_id, None,       0.10)
            RecetaColorNormalizada.upsert(db.session, color_id, colo_id, "SKU-PROVA", 0.40)
            db.session.commit()

            generica   = RecetaColorNormalizada.query.filter_by(
                color_id=color_id, colorante_id=colo_id, producto_sku=None).first()
            especifica = RecetaColorNormalizada.query.filter_by(
                color_id=color_id, colorante_id=colo_id, producto_sku="SKU-PROVA").first()

            assert generica.n_muestras == 1
            assert abs(generica.gr_por_kg - 0.10) < 1e-6
            assert especifica.n_muestras == 1
            assert abs(especifica.gr_por_kg - 0.40) < 1e-6


# ============================================================
#  2. Molde on-the-fly al crear OP
# ============================================================

class TestMoldeOnTheFly:

    def test_molde_se_crea_si_no_existe(self, client, app):
        """
        Cuando se crea una OP con un molde_id que NO existe en el catálogo,
        el side-effect debe crear el Molde (y no romperse).
        El molde_id referenciado no necesita existir — _aprender_de_op lo crea.
        """
        with app.app_context():
            # Confirmar que el molde no existe aún
            assert db.session.get(Molde, "MOL-OTF-01") is None

        payload = {
            "numero_op": "OP-OTF-MOLDE",
            "maquina_id": 1,
            "molde_id":   "MOL-OTF-01",
            "molde":      "Molde On The Fly",
            "snapshot_tiempo_ciclo": 20.0,
            "snapshot_horas_turno": 8.0,
            "snapshot_peso_colada_gr": 3.0,
            # Manual snapshot con pieza_sku=None (molde genérico)
            "snapshot_composicion": [
                {"pieza_sku": None, "cavidades": 4, "peso_unit_gr": 25.0}
            ],
            "lotes": []
        }

        resp = client.post('/api/ordenes', json=payload)
        if resp.status_code != 201:
            print(f"\nError: {resp.get_json()}")
        assert resp.status_code == 201

        with app.app_context():
            # El molde debe haber sido creado por _aprender_de_op
            molde = db.session.get(Molde, "MOL-OTF-01")
            assert molde is not None
            assert molde.activo is True

    def test_molde_existente_no_es_sobreescrito(self, client, app):
        """
        Si el molde YA EXISTE en el catálogo, _aprender_de_op NO lo toca.
        El peso_tiro_gr original se preserva aunque el snapshot diga otro valor.
        """
        with app.app_context():
            linea = Linea.query.first()
            fam   = Familia.query.first()

            molde_orig = Molde(
                codigo="MOL-ORIG-01",
                nombre="Molde Original",
                peso_tiro_gr=100.0,  # valor original
                tiempo_ciclo_std=15.0,
                activo=True
            )
            db.session.add(molde_orig)

            pieza_orig = Pieza(sku="PIEZA-ORIG", piezas="Pieza Original",
                               tipo="SIMPLE", linea_id=linea.id, familia_id=fam.id,
                               cavidad=2, peso=45.0)
            db.session.add(pieza_orig)
            db.session.flush()

            mp_orig = MoldePieza(molde_id="MOL-ORIG-01", pieza_sku="PIEZA-ORIG",
                                 cavidades=2, peso_unitario_gr=45.0)
            db.session.add(mp_orig)
            db.session.commit()

        # Crear OP con snapshot que intenta usar otro peso
        payload = {
            "numero_op": "OP-NO-OVERWRITE",
            "maquina_id": 1,
            "molde_id": "MOL-ORIG-01",
            "snapshot_tiempo_ciclo": 15.0,
            "snapshot_horas_turno": 8.0,
            "snapshot_peso_colada_gr": 5.0,
            "snapshot_composicion": [
                {"pieza_sku": "PIEZA-ORIG", "cavidades": 2, "peso_unit_gr": 999.0}  # valor diferente
            ],
            "lotes": []
        }

        resp = client.post('/api/ordenes', json=payload)
        assert resp.status_code == 201

        with app.app_context():
            mp = MoldePieza.query.filter_by(molde_id="MOL-ORIG-01", pieza_sku="PIEZA-ORIG").first()
            # Debe mantenerse el valor original (45g), no el del snapshot (999g)
            assert mp.peso_unitario_gr == 45.0


# ============================================================
#  3. Endpoint GET /api/catalogo/receta-color
# ============================================================

class TestEndpointRecetaColor:

    def test_sin_receta_devuelve_tiene_receta_false(self, client, app):
        """Color sin OPs previas → tiene_receta=False y lista vacía."""
        color_id = _make_color(app, "ROSA SIN RECETA", 77)

        resp = client.get(f'/api/catalogo/receta-color?color_id={color_id}')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['tiene_receta'] is False
        assert data['pigmentos'] == []

    def test_con_receta_devuelve_pigmentos(self, app, client):
        """Después de insertar una RecetaColorNormalizada, el endpoint la devuelve."""
        color_id = _make_color(app, "GRIS CON RECETA", 76)
        colo_id  = _make_colorante(app, "NEGRO CARBON TEST")

        with app.app_context():
            RecetaColorNormalizada.upsert(db.session, color_id, colo_id, None, 0.25)
            db.session.commit()

        resp = client.get(f'/api/catalogo/receta-color?color_id={color_id}')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['tiene_receta'] is True
        assert len(data['pigmentos']) == 1
        pig = data['pigmentos'][0]
        assert pig['colorante_id'] == colo_id
        assert abs(pig['gr_por_kg'] - 0.25) < 1e-4

    def test_meta_kg_calcula_gramos_absolutos(self, app, client):
        """Si se pasa meta_kg=200, la respuesta incluye gramos = gr_por_kg * 200."""
        color_id = _make_color(app, "CELESTE META", 75)
        colo_id  = _make_colorante(app, "AZUL CIELO TEST")

        with app.app_context():
            RecetaColorNormalizada.upsert(db.session, color_id, colo_id, None, 0.50)
            db.session.commit()

        resp = client.get(f'/api/catalogo/receta-color?color_id={color_id}&meta_kg=200')
        assert resp.status_code == 200
        data = resp.get_json()
        pig = data['pigmentos'][0]
        assert 'gramos' in pig
        assert abs(pig['gramos'] - 100.0) < 1e-2  # 0.50 * 200 = 100
