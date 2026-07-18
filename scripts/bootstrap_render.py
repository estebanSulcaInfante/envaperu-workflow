import sys
from pathlib import Path

from sqlalchemy import inspect


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import create_app
from app.extensions import db


def main():
    app = create_app()
    with app.app_context():
        before = set(inspect(db.engine).get_table_names())
        db.create_all()
        after = set(inspect(db.engine).get_table_names())

    created = sorted(after - before)
    print(f"Render database ready: {len(after)} tables, {len(created)} created.")


if __name__ == '__main__':
    main()
