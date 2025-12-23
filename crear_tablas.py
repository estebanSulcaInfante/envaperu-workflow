from app import create_app
from app.extensions import db
from datetime import datetime, timezone

# Importamos TODOS los modelos necesarios
# Asegúrate de que lote.py, materiales.py y recetas.py existan en tu carpeta models/
from app.models.materiales import MateriaPrima, Colorante
from app.models.orden import OrdenProduccion
from app.models.lote import LoteColor
from app.models.recetas import SeCompone, SeColorea
from app.models.producto import ProductoTerminado, Pieza, ProductoPieza
from app.models.maquina import Maquina
from app.models.registro import RegistroDiarioProduccion

app = create_app()

def inicializar_bd():
    with app.app_context():
        print("🗑️  Borrando base de datos antigua...")
        try:
            db.drop_all()
            print("🏗️  Creando tablas nuevas con la estructura actualizada...")
            db.create_all()
        except UnicodeDecodeError as e:
            print("\n❌ ERROR DE CODIFICACIÓN EN LA CONEXIÓN A LA BASE DE DATOS")
            print("   Parece que tu contraseña o usuario en '.env' tiene caracteres especiales (tildes, ñ, etc).")
            print("   Por favor, reemplaza esos caracteres con su código URL (ej: 'ó' -> '%C3%B3').")
            print(f"   Detalle del error: {e}")
            return
        except Exception as e:
            print(f"\n❌ Ocurrió un error inesperado al conectar con la BD: {e}")
            return

        print("🌱 Insertando datos semilla (Seed Data)...")

        # ---------------------------------------------------------
        # 1. CATALOGO DE MATERIALES
        # ---------------------------------------------------------
        # Materiales
        mp_pp_clarif = MateriaPrima(nombre="PP Clarif", tipo="VIRGEN")
        mp_segunda = MateriaPrima(nombre="Segunda", tipo="MOLIDO")
        
        # Pigmentos
        pig_amarillo = Colorante(nombre="Amarillo CH 1041")
        pig_azul = Colorante(nombre="Azul Ultra")
        pig_rojo = Colorante(nombre="Rojo R120")
        pig_magenta = Colorante(nombre="Magenta 21")
        pig_verde = Colorante(nombre="Verde 7041")
        pig_dioxido = Colorante(nombre="Dioxido Titanio")
        
        db.session.add_all([mp_pp_clarif, mp_segunda, pig_amarillo, pig_azul, pig_rojo, pig_magenta, pig_verde, pig_dioxido])
        db.session.commit()

        # ---------------------------------------------------------
        # 1.5 CATALOGO DE MAQUINAS
        # ---------------------------------------------------------
        maq_iny05 = Maquina(nombre="INY-05", tipo="HAI TIAN 350T")
        maq_ht320a = Maquina(nombre="HT-320 A", tipo="HAI TIAN 320T")
        maq_iny02 = Maquina(nombre="INY-02", tipo="HAI TIAN 250T")
        
        db.session.add_all([maq_iny05, maq_ht320a, maq_iny02])
        db.session.commit()

        # ---------------------------------------------------------
        # 2. ORDEN DE PRODUCCIÓN: OP-1322 (Balde Romano)
        # ---------------------------------------------------------
        orden = OrdenProduccion(
            numero_op="OP-1322",
            maquina_id=maq_iny05.id,  # FK a Maquina
            fecha_inicio=datetime.now(timezone.utc),
            producto="BALDE ROMANO",
            molde="BALDE PLAYERO ROMANO",
            
            # --- PARAMETROS TÉCNICOS ---
            peso_unitario_gr=87.0,
            peso_inc_colada=176.0,
            cavidades=2,
            
            # --- TIEMPOS ---
            tiempo_ciclo=30.0,
            horas_turno=23.0,
            
            # --- ESTRATEGIA (Por Peso) ---
            tipo_estrategia="POR_PESO",
            meta_total_kg=1050.0,
            meta_total_doc=None
        )
        db.session.add(orden)
        db.session.commit()

        # ---------------------------------------------------------
        # 3. LOTES Y RECETAS
        # ---------------------------------------------------------
        
        # DATASET DE COLORES Y RECETAS
        # Estructura: (NombreColor, [(PigmentoObj, Dosis)], Personas)
        # Nota: Materiales son siempre 50/50 PP y Segunda.
        lotes_config = [
            ("Amarillo", [(pig_amarillo, 30.0), (pig_dioxido, 5.0)], 1),
            ("Azul",    [(pig_azul, 60.0), (pig_dioxido, 5.0)], 1),
            ("Rojo",    [(pig_rojo, 40.0), (pig_dioxido, 5.0)], 1),
            ("Magenta", [(pig_magenta, 40.0), (pig_dioxido, 5.0)], 1),
            ("Verde",   [(pig_verde, 20.0), (pig_amarillo, 5.0), (pig_dioxido, 5.0)], 1),
            ("Lila", [(pig_dioxido, 5.0), (pig_magenta, 40.0), (pig_azul, 42.0)], 1), 
        ]

        for nombre_color, lista_pigmentos, num_personas in lotes_config:
            # Crear Lote
            lote = LoteColor(
                numero_op=orden.numero_op,
                color_nombre=nombre_color,
                personas=num_personas
            )
            db.session.add(lote)
            db.session.flush() # Para ID
            
            # Receta Materiales (Fijos 50/50)
            db.session.add(SeCompone(lote_id=lote.id, materia_prima_id=mp_pp_clarif.id, fraccion=0.5))
            db.session.add(SeCompone(lote_id=lote.id, materia_prima_id=mp_segunda.id, fraccion=0.5))
            
            # Receta Pigmentos (Dinámica)
            for pig_obj, dosis in lista_pigmentos:
                db.session.add(SeColorea(lote_id=lote.id, colorante_id=pig_obj.id, gramos=dosis))
        
        db.session.commit()

        # ---------------------------------------------------------
        # 4. REGISTRO DIARIO (Simulacion)
        # ---------------------------------------------------------
        # Simulamos que en Iny-05, OP-1322, fecha hoy, se produjo algo
        # ---------------------------------------------------------
        # 4. REGISTRO DIARIO (Simulacion Master-Detail)
        # ---------------------------------------------------------
        # Importar modelos detalle si no están arriba
        from app.models.registro import DetalleProduccionHora
        
        # Crear Cabecera (Sheet)
        reg_header = RegistroDiarioProduccion(
            orden_id=orden.numero_op,
            maquina_id=maq_iny05.id,
            fecha=datetime.now(timezone.utc).date(),
            turno="DIA",
            hora_inicio="07:00",
            
            # Contadores
            colada_inicial=1000,
            colada_final=1500, # 500 coladas total
            
            # Parametros
            tiempo_ciclo_reportado=30.5,
            cantidad_por_hora_meta=120,
            tiempo_enfriamiento=5.0,
            
            # Snapshots
            snapshot_cavidades=orden.cavidades,
            snapshot_peso_neto_gr=orden.peso_unitario_gr,
            snapshot_peso_colada_gr=10.0,
            snapshot_peso_extra_gr=0.0
        )
        
        # Calcular totales cabecera
        reg_header.actualizar_totales()
        db.session.add(reg_header)
        db.session.flush()
        
        # Crear Detalles (Horas)
        detalles_muestra = [
            ("07:00", "JUAN PEREZ", "AMARILLO", 50),
            ("08:00", "JUAN PEREZ", "AMARILLO", 110),
            ("09:00", "JUAN PEREZ", "ROJO", 100), # Cambio color
            ("10:00", "JUAN PEREZ", "ROJO", 0),   # Parada?
        ]
        
        peso_tiro = (reg_header.snapshot_peso_neto_gr * reg_header.snapshot_cavidades)
        
        for hora, maq, col, cant in detalles_muestra:
            det = DetalleProduccionHora(
                registro_id=reg_header.id,
                hora=hora,
                maquinista=maq,
                color=col,
                observacion="Normal" if cant > 0 else "Parada Mantenimiento",
                coladas_realizadas=cant
            )
            det.calcular_metricas(reg_header.snapshot_cavidades, peso_tiro)
            db.session.add(det)
            
        db.session.commit()

        # ---------------------------------------------------------
        # VERIFICACIÓN FINAL
        # ---------------------------------------------------------
        print("\n✅ ¡Base de Datos Inicializada con Éxito!")
        print("-" * 50)
        print(f"📄 Orden Generada: {orden.numero_op}")
        print(f"   Producto: {orden.producto} ({orden.cavidades} cavidades)")
        print(f"   Peso Tiro (inc. colada): {orden.peso_inc_colada} gr")
        print(f"   T/C: {orden.tiempo_ciclo} seg")
        print("-" * 50)
        print(f"📝 Registro Diario Generado: ID {reg_header.id}")
        print(f"   Total Coladas: {reg_header.total_coladas_calculada}")
        print(f"   Detalles Hora: {len(detalles_muestra)}")

if __name__ == "__main__":
    inicializar_bd()