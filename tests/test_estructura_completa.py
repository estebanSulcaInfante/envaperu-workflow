import pytest
from datetime import datetime, timezone
from app.models.orden import OrdenProduccion
from app.models.lote import LoteColor
from app.models.recetas import SeCompone, SeColorea
from app.models.materiales import MateriaPrima, Colorante
from app.extensions import db

def test_estructura_completa_json(client, app):
    """
    Verifica la estructura completa del JSON (Reporte Normalizado):
    - Cabecera
    - Lotes Polimórficos con Extra
    - Materiales (Lista dinámica con cálculo de peso por fracción)
    - Pigmentos (Lista dinámica con dosis de gramos)
    - Mano de Obra (Cálculo de HH)
    - Tabla Auxiliar
    """
    with app.app_context():
        # 1. SETUP: MATERIALES Y COLORANTES
        mp1 = MateriaPrima(nombre="POLIPROPILENO", tipo="VIRGEN")
        mp2 = MateriaPrima(nombre="MOLIDO", tipo="MOLIDO")
        color = Colorante(nombre="AZUL")
        db.session.add_all([mp1, mp2, color])
        db.session.commit()

        # 2. SETUP: ORDEN (Por Peso)
        orden = OrdenProduccion(
            numero_op="OP-FULL-STRUCT",
            tipo_estrategia="POR_PESO",
            meta_total_kg=1000.0,
            peso_unitario_gr=50.0,
            peso_inc_colada=200.0, # 200g tiro
            cavidades=4,
            tiempo_ciclo=20.0,
            horas_turno=24.0,
            fecha_inicio=datetime.now(timezone.utc)
        )
        db.session.add(orden)
        db.session.commit()

        # 3. SETUP: LOTE (1 solo lote -> 1000kg)
        lote = LoteColor(
            numero_op=orden.numero_op,
            color_nombre="AZUL TEST",
            personas=2 # 2 Operarios
        )
        db.session.add(lote)
        db.session.commit()

        # 4. SETUP: RECETA
        # Mezcla: 60% PP, 40% Molido
        r1 = SeCompone(lote_id=lote.id, materia_prima_id=mp1.id, fraccion=0.60)
        r2 = SeCompone(lote_id=lote.id, materia_prima_id=mp2.id, fraccion=0.40)
        # Color: 50g dosis
        c1 = SeColorea(lote_id=lote.id, colorante_id=color.id, gramos=50.0)
        
        db.session.add_all([r1, r2, c1])
        db.session.commit()

        # 5. VALIDACIÓN DEL DICCIONARIO (Endpoint simulated)
        data = orden.to_dict()
        lote_data = data['lotes'][0]
        resumen = data['resumen_totales']

        # A. CÁLCULO DE PESOS
        # Meta: 1000kg. Merma: (200 - 4*50)/200 = 0/200 = 0% ?? 
        # Espera: 50*4 = 200. Tiro = 200. Merma = 0.
        # Merma 0 -> Extra 0.
        # Peso Produccion: 1000 kg.
        # Total a Maquina: 1000 kg.
        
        assert resumen['Peso(Kg) PRODUCCION'] == 1000.0
        assert resumen['%Merma'] == 0.0
        assert resumen['EXTRA'] == 0.0
        
        # B. LOTE DATA
        assert lote_data['Peso (Kg)'] == 1000.0 # Col D activa
        assert lote_data['TOTAL + EXTRA (Kg)'] == 1000.0
        
        # C. MATERIALES (Cálculo dinámico)
        # PP: 60% de 1000 = 600kg
        # Molido: 40% de 1000 = 400kg
        mats = lote_data['materiales']
        assert len(mats) == 2
        
        pp_data = next(m for m in mats if m['nombre'] == "POLIPROPILENO")
        assert pp_data['fraccion'] == 0.60
        assert pp_data['peso_kg'] == 600.0
        
        mol_data = next(m for m in mats if m['nombre'] == "MOLIDO")
        assert mol_data['peso_kg'] == 400.0
        
        # D. PIGMENTOS
        pigs = lote_data['pigmentos']
        assert pigs[0]['nombre'] == "AZUL"
        assert pigs[0]['dosis_gr'] == 50.0
        
        # E. MANO DE OBRA
        # Coladas = (1000kg * 1000) / 200g = 5000 golpes
        # Segundos = 5000 * 20s = 100,000 s
        # Horas = 100,000 / 3600 = 27.777...
        # Dias = 27.77 / 24 = 1.157...
        # HH = (Dias * HorasTurno * Personas) / #Colores
        # HH = (1.157 * 24 * 2) / 1 = 27.77 * 2 = 55.55...
        
        mo = lote_data['mano_obra']
        assert mo['personas'] == 2
        
        horas_maq = 100000 / 3600
        dias_maq = horas_maq / 24
        
        # The system now uses full precision for 'Días'
        hh_esperadas = dias_maq * 24 * 2
        
        assert mo['horas_hombre'] == pytest.approx(hh_esperadas, abs=0.01)
