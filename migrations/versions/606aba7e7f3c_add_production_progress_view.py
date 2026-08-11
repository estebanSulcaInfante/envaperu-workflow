"""add production progress view

Revision ID: 606aba7e7f3c
Revises: f82e1f7a3c64
Create Date: 2026-08-11 10:50:16.432274

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "606aba7e7f3c"
down_revision = "f82e1f7a3c64"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    create_clause = (
        "CREATE OR REPLACE VIEW v_avance_produccion WITH (security_invoker = true)"
        if connection.dialect.name == "postgresql"
        else "CREATE VIEW v_avance_produccion"
    )
    op.execute(sa.text(f"""
        {create_clause} AS
        SELECT
            station_id,
            operational_date,
            op_normalized AS op,
            ot_normalized AS ot,
            mold_normalized AS mold,
            color_normalized AS color,
            machine_normalized AS machine_code,
            shift_normalized AS shift,
            COUNT(*) AS bags,
            SUM(weight_kg) AS weight_kg,
            MIN(captured_at_utc) AS first_capture_at_utc,
            MAX(captured_at_utc) AS last_capture_at_utc,
            MAX(imported_at_utc) AS last_received_at_utc
        FROM estacion_pesaje_legacy
        WHERE is_deleted = false
        GROUP BY
            station_id,
            operational_date,
            op_normalized,
            ot_normalized,
            mold_normalized,
            color_normalized,
            machine_normalized,
            shift_normalized
    """))

    if connection.dialect.name == "postgresql":
        # The Flask API is the only public authority for this read model.
        # Keep the view outside the Supabase Data API even though it also
        # respects the caller's underlying-table privileges.
        op.execute(sa.text("""
            DO $body$
            BEGIN
              IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
                REVOKE ALL PRIVILEGES ON v_avance_produccion FROM anon;
              END IF;
              IF EXISTS (
                SELECT 1 FROM pg_roles WHERE rolname = 'authenticated'
              ) THEN
                REVOKE ALL PRIVILEGES ON v_avance_produccion FROM authenticated;
              END IF;
            END
            $body$;
        """))


def downgrade():
    op.execute("DROP VIEW IF EXISTS v_avance_produccion")
