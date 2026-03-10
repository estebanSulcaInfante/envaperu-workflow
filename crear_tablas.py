from app import create_app
from app.extensions import db
from datetime import datetime, timezone

# Importamos TODOS los modelos necesarios
from app.models.materiales import MateriaPrima, Colorante
from app.models.orden import OrdenProduccion, SnapshotComposicionMolde
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
            print(f"   Detalle del error: {e}")
            return
        except Exception as e:
            print(f"\n❌ Ocurrió un error inesperado al conectar con la BD: {e}")
            return

        print("🌱 Insertando datos semilla (Seed Data)...")

        # ---------------------------------------------------------
        # 1. CATALOGO DE MATERIALES
        # ---------------------------------------------------------
        mp_pp_clarif = MateriaPrima(nombre="PP Clarif", tipo="VIRGEN")
        mp_segunda   = MateriaPrima(nombre="Segunda",   tipo="MOLIDO")

        pig_amarillo = Colorante(nombre="Amarillo CH 1041")
        pig_azul     = Colorante(nombre="Azul Ultra")
        pig_rojo     = Colorante(nombre="Rojo R120")
        pig_magenta  = Colorante(nombre="Magenta 21")
        pig_verde    = Colorante(nombre="Verde 7041")
        pig_dioxido  = Colorante(nombre="Dioxido Titanio")

        db.session.add_all([mp_pp_clarif, mp_segunda,
                            pig_amarillo, pig_azul, pig_rojo, pig_magenta, pig_verde, pig_dioxido])
        db.session.commit()

        # ---------------------------------------------------------
        # 2. MÁQUINAS
        # ---------------------------------------------------------
        maq_iny05  = Maquina(nombre="INY-05",   tipo="HAI TIAN 350T")
        maq_ht320a = Maquina(nombre="HT-320 A", tipo="HAI TIAN 320T")
        maq_iny02  = Maquina(nombre="INY-02",   tipo="HAI TIAN 250T")

        db.session.add_all([maq_iny05, maq_ht320a, maq_iny02])
        db.session.commit()

        # ---------------------------------------------------------
        # 3. CATÁLOGO DE PRODUCTOS
        # ---------------------------------------------------------
        linea_industrial = Linea(codigo=1, nombre='INDUSTRIAL')
        linea_hogar      = Linea(codigo=2, nombre='HOGAR')
        db.session.add_all([linea_industrial, linea_hogar])
        db.session.flush()

        fam_solido = FamiliaColor(nombre="SOLIDO", codigo=1)
        db.session.add(fam_solido)
        db.session.flush()

        col_amarillo = ColorProducto(nombre="Amarillo", codigo=1, familia_id=fam_solido.id)
        col_rojo     = ColorProducto(nombre="Rojo",     codigo=2, familia_id=fam_solido.id)
        col_azul     = ColorProducto(nombre="Azul",     codigo=3, familia_id=fam_solido.id)
        col_magenta  = ColorProducto(nombre="Magenta",  codigo=4, familia_id=fam_solido.id)
        col_verde    = ColorProducto(nombre="Verde",    codigo=5, familia_id=fam_solido.id)
        col_lila     = ColorProducto(nombre="Lila",     codigo=6, familia_id=fam_solido.id)

        db.session.add_all([col_amarillo, col_rojo, col_azul, col_magenta, col_verde, col_lila])
        db.session.flush()

        familia_baldes = Familia(codigo=10, nombre='BALDES')
        db.session.add(familia_baldes)
        db.session.flush()

        pieza_balde = Pieza(
            sku="10101-BALDE", piezas="Cuerpo Balde 20L",
            linea_id=linea_industrial.id, familia_id=familia_baldes.id,
            cod_pieza=1, cod_col="01", tipo_color="Solido",
            cavidad=1, peso=600.0,
            cod_extru=1, tipo_extruccion="Inyeccion",
            cod_mp="MP01", mp="PP"
        )
        pieza_asa = Pieza(
            sku="10102-ASA", piezas="Asa Balde 20L",
            linea_id=linea_industrial.id, familia_id=familia_baldes.id,
            cod_pieza=2, cod_col="01", tipo_color="Solido",
            cavidad=2, peso=50.0,
            cod_extru=1, tipo_extruccion="Inyeccion",
            cod_mp="MP02", mp="HDPE"
        )
        db.session.add_all([pieza_balde, pieza_asa])
        db.session.flush()

        pt_balde_romano = ProductoTerminado(
            cod_sku_pt="PT-BALDE-ROMANO",
            producto="Balde Romano 20L Completo",
            linea_id=linea_industrial.id,
            familia_id=familia_baldes.id,
            cod_producto=100,
            cod_familia_color=1,
            familia_color="SOLIDO",
            familia_color_id=fam_solido.id,
            um="UND",
            peso_g=650.0,
            status="ACTIVO",
            estado_revision="VERIFICADO"
        )
        db.session.add(pt_balde_romano)
        db.session.flush()

        db.session.add(ProductoPieza(producto_terminado_id=pt_balde_romano.cod_sku_pt, pieza_sku=pieza_balde.sku, cantidad=1))
        db.session.add(ProductoPieza(producto_terminado_id=pt_balde_romano.cod_sku_pt, pieza_sku=pieza_asa.sku, cantidad=1))
        db.session.commit()

        # ---------------------------------------------------------
        # 4. MOLDE (catálogo)
        # ---------------------------------------------------------
        # Balde 20L: 1 cavidad × 600g + 10g colada = 610g tiro
        molde_balde = Molde(
            codigo="MOL-BALDE-20L",
            nombre="Balde Romano 20L",
            peso_tiro_gr=610.0,
            tiempo_ciclo_std=35.0
        )
        db.session.add(molde_balde)
        db.session.flush()

        db.session.add(MoldePieza(
            molde_id=molde_balde.codigo,
            pieza_sku=pieza_balde.sku,
            cavidades=1,
            peso_unitario_gr=600.0
        ))
        db.session.commit()

        # ---------------------------------------------------------
        # 5. ORDEN DE PRODUCCIÓN: OP-1322 (Balde Romano)
        # ---------------------------------------------------------
        # Molde: 1 cav × 600g + 10g colada = 610g tiro
        # Merma = 10/610 ≈ 1.64%
        orden = OrdenProduccion(
            numero_op   = "OP-1322",
            maquina_id  = maq_iny05.id,
            fecha_inicio= datetime.now(timezone.utc),
            producto    = "BALDE ROMANO 20L",
            producto_sku= pt_balde_romano.cod_sku_pt,
            molde       = "BALDE ROMANO 20L",
            molde_id    = molde_balde.codigo,

            # Parámetros técnicos
            snapshot_tiempo_ciclo   = 35.0,
            snapshot_horas_turno    = 24.0,
            snapshot_peso_colada_gr = 10.0,
        )
        db.session.add(orden)
        db.session.flush()

        # Snapshot de composición (congelado al crear)
        db.session.add(SnapshotComposicionMolde(
            orden_id     = orden.numero_op,
            pieza_sku    = pieza_balde.sku,
            cavidades    = 1,
            peso_unit_gr = 600.0,
        ))
        db.session.flush()

        # ---------------------------------------------------------
        # 6. LOTES DE COLOR (con meta_kg por lote)
        # ---------------------------------------------------------
        # 6 colores × 175 kg = 1050 kg total
        lotes_config = [
            ("Amarillo", [(pig_amarillo, 30.0), (pig_dioxido, 5.0)],  1, 175.0),
            ("Azul",     [(pig_azul,    60.0), (pig_dioxido, 5.0)],  1, 175.0),
            ("Rojo",     [(pig_rojo,    40.0), (pig_dioxido, 5.0)],  1, 175.0),
            ("Magenta",  [(pig_magenta, 40.0), (pig_dioxido, 5.0)],  1, 175.0),
            ("Verde",    [(pig_verde,   20.0), (pig_amarillo, 5.0), (pig_dioxido, 5.0)], 1, 175.0),
            ("Lila",     [(pig_dioxido,  5.0), (pig_magenta, 40.0), (pig_azul,   42.0)], 1, 175.0),
        ]

        for nombre_color, lista_pigmentos, num_personas, kg_meta in lotes_config:
            color_obj = ColorProducto.query.filter_by(nombre=nombre_color).first()
            if not color_obj:
                print(f"⚠️  Color {nombre_color} no encontrado en catálogo, saltando...")
                continue

            lote = LoteColor(
                numero_op = orden.numero_op,
                color_id  = color_obj.id,
                personas  = num_personas,
                meta_kg   = kg_meta,
            )
            db.session.add(lote)
            db.session.flush()

            # Receta de materiales (50/50)
            db.session.add(SeCompone(lote_id=lote.id, materia_prima_id=mp_pp_clarif.id, fraccion=0.5))
            db.session.add(SeCompone(lote_id=lote.id, materia_prima_id=mp_segunda.id,   fraccion=0.5))

            # Pigmentos
            for pig_obj, dosis in lista_pigmentos:
                db.session.add(SeColorea(lote_id=lote.id, colorante_id=pig_obj.id, gramos=dosis))

        # Calcular métricas en cascada
        orden.actualizar_metricas()
        db.session.commit()

        # ---------------------------------------------------------
        # 7. REGISTRO DIARIO (simulación)
        # ---------------------------------------------------------
        reg_header = RegistroDiarioProduccion(
            orden_id  = orden.numero_op,
            maquina_id= maq_iny05.id,
            fecha     = datetime.now(timezone.utc).date(),
            turno     = "DIA",
            hora_inicio= "07:00",

            colada_inicial= 1000,
            colada_final  = 1500,   # 500 coladas

            tiempo_ciclo_reportado = 35.0,
            cantidad_por_hora_meta = 100,
            tiempo_enfriamiento    = 5.0,

            # Snapshots desde los valores cacheados de la orden
            snapshot_cavidades    = orden.calculo_cavidades_totales,
            snapshot_peso_neto_gr = orden.calculo_peso_neto_golpe,
            snapshot_peso_colada_gr = orden.snapshot_peso_colada_gr,
            snapshot_peso_extra_gr  = 0.0,
        )
        reg_header.actualizar_totales()
        db.session.add(reg_header)
        db.session.flush()

        detalles_muestra = [
            ("07:00", "JUAN PEREZ",  "AMARILLO", 50),
            ("08:00", "JUAN PEREZ",  "AMARILLO", 110),
            ("09:00", "JUAN PEREZ",  "ROJO",     100),
            ("10:00", "JUAN PEREZ",  "ROJO",     0),
        ]

        peso_tiro = reg_header.snapshot_peso_neto_gr * reg_header.snapshot_cavidades

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
        # 8. CONTROL DE PESO (simulación)
        # ---------------------------------------------------------
        bultos_sample = [
            (10.0, "AMARILLO"), (10.0, "AMARILLO"), (10.0, "AMARILLO"),
            (10.0, "AMARILLO"), (10.0, "AMARILLO"),
            (10.0, "ROJO"),     (10.0, "ROJO"),     (10.0, "ROJO"), (7.0, "ROJO"),
        ]

        for peso, col in bultos_sample:
            db.session.add(ControlPeso(
                registro_id  = reg_header.id,
                peso_real_kg = peso,
                color_nombre = col,
                hora_registro= datetime.now(timezone.utc)
            ))

        db.session.commit()

        # ---------------------------------------------------------
        # 9. TALONARIOS RDP
        # ---------------------------------------------------------
        db.session.add(Talonario(desde=30001, hasta=30500, descripcion="Lote Inicial 2026"))
        db.session.commit()

        # ---------------------------------------------------------
        # RESUMEN
        # ---------------------------------------------------------
        lotes_db = LoteColor.query.filter_by(numero_op=orden.numero_op).all()
        print("\n✅ ¡Base de Datos Inicializada con Éxito!")
        print("-" * 55)
        print(f"📋 Orden: {orden.numero_op} — {orden.producto}")
        print(f"   Molde:  {molde_balde.nombre} | 1 cav × 600g + 10g colada = 610g tiro")
        print(f"   Merma:  {orden.calculo_merma_pct*100:.2f}%")
        print(f"   Total producción: {orden.calculo_peso_produccion:.1f} kg ({len(lotes_db)} lotes × 175 kg)")
        print(f"   Coladas totales estimadas: {orden.calculo_peso_produccion*1000/orden.calculo_peso_neto_golpe:.1f}")
        print(f"   Días estimados: {orden.calculo_dias:.1f} días")
        print("-" * 55)
        print(f"📝 Registro: ID {reg_header.id} · {len(detalles_muestra)} horas · {len(bultos_sample)} bultos")
        print(f"📋 Talonario: 30001–30500 (500 correlativos)")


if __name__ == "__main__":
    inicializar_bd()