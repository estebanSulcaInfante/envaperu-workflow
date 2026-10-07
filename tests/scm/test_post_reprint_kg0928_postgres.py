"""Real POST contracts after the exact KG/OF0928 migrations, local test DB only."""
import pytest
from tests.scm.test_scm_kg009_concurrency_postgres import (
    kg009_schema_url, postgres_kg009_app, _reset_dedicated_data,
)
from tests.scm.test_postpesaje_reprint_contract import (
    test_real_postpesaje_preview_confirm_claim_ack_contract as run_contract,
)

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize('legacy_fixture', [False, True, 'legacy_nullable'])
def test_post_original_and_copy_after_kg_of0928_migrations(postgres_kg009_app, legacy_fixture):
    _reset_dedicated_data(postgres_kg009_app)
    run_contract(postgres_kg009_app, legacy_fixture)
