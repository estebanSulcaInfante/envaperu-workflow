import os

from app import create_app, db
from app.models.maquina import Maquina, TipoMaquina
from app.models.orden import OrdenProduccion


app = create_app()


def seed_database():
    with app.app_context():
        db.create_all()

        machine_type = TipoMaquina(
            codigo="E2E-INYECCION",
            nombre="E2E INYECCION",
            proceso="PRODUCCION",
        )
        db.session.add(machine_type)
        db.session.flush()

        machine = Maquina(
            codigo="MQ-E2E",
            nombre="MAQUINA E2E",
            tipo_maquina_id=machine_type.id,
            estado="OPERATIVA",
            activo=True,
        )
        db.session.add(machine)
        db.session.flush()

        db.session.add(
            OrdenProduccion(
                numero_op="OP-E2E-SYNC-001",
                maquina_id=machine.id,
                producto="PRODUCTO E2E",
                molde="MOLDE E2E",
                calculo_cavidades_totales=1,
                calculo_peso_neto_golpe=100.0,
            )
        )
        db.session.commit()


if __name__ == "__main__":
    seed_database()
    app.run(
        host="127.0.0.1",
        port=int(os.environ["TEST_PORT"]),
        debug=False,
        use_reloader=False,
    )
