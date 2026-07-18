import argparse
import os
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def _parser():
    parser = argparse.ArgumentParser(description="Isolated station monitoring provider")
    parser.add_argument("--database", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--station-id", required=True)
    parser.add_argument("--station-code", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--demo-op")
    parser.add_argument("--demo-target-kg", type=float)
    return parser


def main():
    args = _parser().parse_args()
    database = Path(args.database).expanduser().resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    os.environ["DATABASE_URL"] = f"sqlite:///{database.as_posix()}"
    os.environ["HEARTBEAT_SECONDS"] = "5"

    from werkzeug.serving import make_server

    from app import create_app
    from app.extensions import db
    from app.models.estacion_pesaje import EstacionPesaje
    from app.models.orden import OrdenProduccion
    from app.services.station_auth import hash_station_token

    app = create_app()
    with app.app_context():
        db.create_all()
        station = db.session.get(EstacionPesaje, args.station_id)
        if station is None:
            station = EstacionPesaje(
                station_id=args.station_id,
                codigo=args.station_code,
                nombre="Balanza E2E",
                ubicacion="Planta E2E aislada",
                estado_admin="ACTIVA",
                token_hash=hash_station_token(args.token),
            )
            db.session.add(station)
        if args.demo_op:
            order = db.session.get(OrdenProduccion, args.demo_op)
            if order is None:
                order = OrdenProduccion(
                    numero_op=args.demo_op,
                    producto="Tapa 38 mm",
                )
                db.session.add(order)
            order.calculo_peso_produccion = args.demo_target_kg or 0
        db.session.commit()

    server = make_server("127.0.0.1", args.port, app, threaded=True)
    print(
        f"MONITORING_PROVIDER_READY origin=http://127.0.0.1:{args.port}",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
