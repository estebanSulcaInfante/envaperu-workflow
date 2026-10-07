"""Merge reprint ledger after the exact KG/OF0928 release history.

Local integration base: 0928d53d1afe31d04feb982afb273a3edaf49406.
Both parent histories are preserved. No DDL or data mutation in this revision.
"""
revision = "f074de2410ac"
down_revision = ("fd1a2b3c4d50", "fe5f60718293")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
