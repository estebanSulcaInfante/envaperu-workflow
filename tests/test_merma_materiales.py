import pytest
from app.models.orden import OrdenProduccion
from app.models.lote import LoteColor
from app.models.recetas import SeCompone
from app.models.materiales import MateriaPrima
from app.extensions import db

def test_calculo_materiales_con_merma(client, app):
    with app.app_context():
        # Materia Prima
        mp = MateriaPrima(nombre="VIRGEN TEST", tipo="VIRGEN")
        db.session.add(mp)
        db.session.commit()

        # Orden con Merma
        # Tiro = 110g. Neto = 100g (2*50). Merma = 10g/110g = 9.09%
        orden = OrdenProduccion(
            numero_op="OP-MERMA-TEST",
            tipo_estrategia="POR_PESO",
            meta_total_kg=1000.0,
            snapshot_peso_unitario_gr=50.0,
            snapshot_peso_inc_colada=110.0, 
            snapshot_cavidades=2,
            snapshot_tiempo_ciclo=20.0,
            snapshot_horas_turno=24.0
        )
        db.session.add(orden)
        db.session.commit()

        # Lote - crear color primero
        from app.models.producto import ColorProducto
        c_test = ColorProducto(nombre="A", codigo=1)
        db.session.add(c_test)
        db.session.commit()
        
        lote = LoteColor(numero_op=orden.numero_op, color_id=c_test.id)
        db.session.add(lote)
        db.session.commit()

        # Receta (100% Virgen)
        # SeCompone debería devolver PesoTotal * (1 + %Merma)
        receta = SeCompone(lote_id=lote.id, materia_prima_id=mp.id, fraccion=1.0)
        db.session.add(receta)
        db.session.commit()
        
        # Actualizar métricas en cascada
        orden.actualizar_metricas()
        db.session.commit()

        # Cálculos Esperados
        # 1. Merma %
        # (110 - 100) / 110 = 0.090909...
        merma_pct = (110 - 100) / 110
        
        # 2. Extra (regla negocio: si > 5%, cobramos mitad -> 4.54%)
        extra_pct = merma_pct / 2
        
        # 3. Peso Base = 1000kg
        # 4. Peso Extra = 1000 * 4.54% = 45.45 kg
        peso_base_mas_extra = 1000.0 + (1000.0 * extra_pct)
        
        # 5. Peso Material Esperado (CON AJUSTE MERMA)
        # = (Base + Extra) * (1 + Merma%)
        peso_material_esperado = peso_base_mas_extra * (1 + merma_pct)
        
        print(f"Merma %: {merma_pct}")
        print(f"Extra %: {extra_pct}")
        print(f"Peso Base+Extra: {peso_base_mas_extra}")
        print(f"Peso Material Calc: {receta.peso_kg}")
        print(f"Peso Material Esperado: {peso_material_esperado}")

        assert receta.peso_kg == pytest.approx(peso_material_esperado, abs=0.01)
