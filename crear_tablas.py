from app import create_app
from app.extensions import db
from datetime import datetime, timezone

# Importamos TODOS los modelos necesarios
from app.models.materiales import MateriaPrima, Colorante
from app.models.orden import OrdenProduccion
from app.models.lote import LoteColor
from app.models.recetas import SeCompone, SeColorea
from app.models.producto import ProductoTerminado, Pieza, ProductoPieza, FamiliaColor, ColorProducto, Linea, Familia
from app.models.maquina import Maquina
from app.models.registro import RegistroDiarioProduccion, DetalleProduccionHora
from app.models.control_peso import ControlPeso
from app.models.talonario import Talonario
from app.models.molde import Molde, MoldePieza

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
            print("   Detalle del error: {e}")
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
        # 1.6 CATALOGO DE PRODUCTOS (Linea, Color, Pieza, Producto)
        # ---------------------------------------------------------
        
        # Crear Líneas (REFACTORIZADO - entidad normalizada)
        linea_industrial = Linea(codigo=1, nombre='INDUSTRIAL')
        linea_hogar = Linea(codigo=2, nombre='HOGAR')
        db.session.add_all([linea_industrial, linea_hogar])
        db.session.flush()
        
        # Familia y Colores de Producto
        # FamiliaColor ahora tiene código para ProductoTerminado
        fam_solido = FamiliaColor(nombre="SOLIDO", codigo=1)
        db.session.add(fam_solido)
        db.session.flush()

        col_amarillo_prod = ColorProducto(nombre="Amarillo", codigo=1, familia_id=fam_solido.id)
        col_rojo_prod = ColorProducto(nombre="Rojo", codigo=2, familia_id=fam_solido.id)
        col_azul_prod = ColorProducto(nombre="Azul", codigo=3, familia_id=fam_solido.id)
        col_magenta_prod = ColorProducto(nombre="Magenta", codigo=4, familia_id=fam_solido.id)
        col_verde_prod = ColorProducto(nombre="Verde", codigo=5, familia_id=fam_solido.id)
        col_lila_prod = ColorProducto(nombre="Lila", codigo=6, familia_id=fam_solido.id)
        
        db.session.add_all([col_amarillo_prod, col_rojo_prod, col_azul_prod, col_magenta_prod, col_verde_prod, col_lila_prod])
        db.session.flush()
        
        # Crear Familias (entidad normalizada - antes era campo string)
        familia_baldes = Familia(codigo=10, nombre='BALDES')
        db.session.add(familia_baldes)
        db.session.flush()

        # Piezas (usando linea_id y familia_id FKs)
        pieza_balde = Pieza(
            sku="10101-BALDE",
            piezas="Cuerpo Balde 20L",
            linea_id=linea_industrial.id,  # FK normalizada
            familia_id=familia_baldes.id,   # FK normalizada
            cod_pieza=1,
            cod_col="01",
            tipo_color="Solido",
            cavidad=1,
            peso=600.0,
            cod_extru=1, tipo_extruccion="Inyeccion",
            cod_mp="MP01", mp="PP"
        )
        pieza_asa = Pieza(
            sku="10102-ASA",
            piezas="Asa Balde 20L",
            linea_id=linea_industrial.id,  # FK normalizada
            familia_id=familia_baldes.id,   # FK normalizada
            cod_pieza=2,
            cod_col="01",
            tipo_color="Solido",
            cavidad=2,
            peso=50.0,
            cod_extru=1, tipo_extruccion="Inyeccion",
            cod_mp="MP02", mp="HDPE"
        )
        db.session.add_all([pieza_balde, pieza_asa])
        db.session.flush()

        # Producto Terminado (usando linea_id y familia_id FKs)
        pt_balde_romano = ProductoTerminado(
            cod_sku_pt="PT-BALDE-ROMANO",
            producto="Balde Romano 20L Completo",
            linea_id=linea_industrial.id,   # FK normalizada
            familia_id=familia_baldes.id,   # FK normalizada
            cod_producto=100,
            cod_familia_color=1,  # Código de FamiliaColor (antes cod_color)
            familia_color="SOLIDO",
            familia_color_id=fam_solido.id,  # FK a FamiliaColor
            um="UND",
            peso_g=650.0,
            status="ACTIVO",
            estado_revision="VERIFICADO"  # Datos de ejemplo ya verificados
        )
        db.session.add(pt_balde_romano)
        db.session.flush()

        # Relacion Producto-Pieza
        db.session.add(ProductoPieza(producto_terminado_id=pt_balde_romano.cod_sku_pt, pieza_sku=pieza_balde.sku, cantidad=1))
        db.session.add(ProductoPieza(producto_terminado_id=pt_balde_romano.cod_sku_pt, pieza_sku=pieza_asa.sku, cantidad=1))
        
        db.session.commit()

        # ---------------------------------------------------------
        # 2. ORDEN DE PRODUCCIÓN: OP-1322 (Balde Romano)
        # ---------------------------------------------------------
        orden = OrdenProduccion(
            numero_op="OP-1322",
            maquina_id=maq_iny05.id,  # FK a Maquina
            fecha_inicio=datetime.now(timezone.utc),
            producto="BALDE ROMANO",
            producto_sku=pt_balde_romano.cod_sku_pt,
            molde="BALDE PLAYERO ROMANO",
            
            # --- PARAMETROS TÉCNICOS ---
            snapshot_peso_unitario_gr=650.0, # Peso real aprox Balde 20L
            snapshot_peso_inc_colada=660.0,
            snapshot_cavidades=1, # Balde grande suele ser 1 cavidad
            
            # --- TIEMPOS ---
            snapshot_tiempo_ciclo=35.0,
            snapshot_horas_turno=24.0,
            
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
        # Tupla: (Nombre Color, Pigmentos, Personas, Peso Asignado Kg)
        lotes_config = [
            ("Amarillo", [(pig_amarillo, 30.0), (pig_dioxido, 5.0)], 1, 175.0),
            ("Azul",    [(pig_azul, 60.0), (pig_dioxido, 5.0)], 1, 175.0),
            ("Rojo",    [(pig_rojo, 40.0), (pig_dioxido, 5.0)], 1, 175.0),
            ("Magenta", [(pig_magenta, 40.0), (pig_dioxido, 5.0)], 1, 175.0),
            ("Verde",   [(pig_verde, 20.0), (pig_amarillo, 5.0), (pig_dioxido, 5.0)], 1, 175.0),
            ("Lila", [(pig_dioxido, 5.0), (pig_magenta, 40.0), (pig_azul, 42.0)], 1, 175.0), 
        ]

        for nombre_color, lista_pigmentos, num_personas, peso_asignado in lotes_config:
            # Buscar ID del color
            color_obj = ColorProducto.query.filter_by(nombre=nombre_color).first()
            if not color_obj:
                print(f"⚠️ Color {nombre_color} no encontrado en catalogo, saltando...")
                continue

            # Crear Lote con ID
            lote = LoteColor(
                numero_op=orden.numero_op,
                color_id=color_obj.id, # FK
                personas=num_personas,
                stock_kg_manual=peso_asignado, # Asignamos la parte de la meta a este lote
                # producto_sku_output=... 
            )
            db.session.add(lote)
            db.session.flush() # Para ID
            
            # Receta Materiales (Fijos 50/50)
            db.session.add(SeCompone(lote_id=lote.id, materia_prima_id=mp_pp_clarif.id, fraccion=0.5))
            db.session.add(SeCompone(lote_id=lote.id, materia_prima_id=mp_segunda.id, fraccion=0.5))
            
            # Receta Pigmentos (Dinámica)
            for pig_obj, dosis in lista_pigmentos:
                db.session.add(SeColorea(lote_id=lote.id, colorante_id=pig_obj.id, gramos=dosis))
        
        # IMPORTANTE: Calcular métricas iniciales de la orden para poblar resumen_totales
        orden.actualizar_metricas()
        db.session.add(orden)
        
        db.session.commit()

        # ---------------------------------------------------------
        # 4. REGISTRO DIARIO (Simulacion)
        # ---------------------------------------------------------
        
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
            snapshot_cavidades=orden.snapshot_cavidades,
            snapshot_peso_neto_gr=orden.snapshot_peso_unitario_gr,
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
        # 5. CONTROL DE PESO (Simulacion)
        # ---------------------------------------------------------
        bultos_sample = [
            (10.0, "AMARILLO"),
            (10.0, "AMARILLO"),
            (10.0, "AMARILLO"),
            (10.0, "AMARILLO"),
            (10.0, "AMARILLO"),
            (10.0, "ROJO"),
            (10.0, "ROJO"),
            (10.0, "ROJO"),
            (7.0, "ROJO"),
        ]
        
        for peso, col in bultos_sample:
            ctrl = ControlPeso(
                registro_id=reg_header.id,
                peso_real_kg=peso,
                color_nombre=col,
                hora_registro=datetime.now(timezone.utc)
            )
            db.session.add(ctrl)
            
        db.session.commit()

        # ---------------------------------------------------------
        # 6. TALONARIOS RDP (Seed)
        # ---------------------------------------------------------
        talonario1 = Talonario(
            desde=30001,
            hasta=30500,
            descripcion="Lote Inicial 2026"
        )
        db.session.add(talonario1)
        db.session.commit()

        # ---------------------------------------------------------
        # VERIFICACIÓN FINAL
        # ---------------------------------------------------------
        print("\n✅ ¡Base de Datos Inicializada con Éxito!")
        print("-" * 50)
        print(f"📄 Orden Generada: {orden.numero_op}")
        print(f"   Producto: {orden.producto} ({orden.snapshot_cavidades} cavidades)")
        print(f"   Peso Tiro (inc. colada): {orden.snapshot_peso_inc_colada} gr")
        print(f"   T/C: {orden.snapshot_tiempo_ciclo} seg")
        print("-" * 50)
        print(f"📝 Registro Diario Generado: ID {reg_header.id}")
        print(f"   Total Coladas: {reg_header.total_coladas_calculada}")
        print(f"   Detalles Hora: {len(detalles_muestra)}")
        print(f"   Bultos Pesados: {len(bultos_sample)}")
        print("-" * 50)
        print(f"📋 Talonario RDP: {talonario1.desde}-{talonario1.hasta}")
        print(f"   Correlativos disponibles: {talonario1.disponibles}")

if __name__ == "__main__":
    inicializar_bd()