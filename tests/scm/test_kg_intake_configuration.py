"""Regression for the pilot configuration incident: intake is not custody."""
from sqlalchemy import text
from decimal import Decimal
import pytest
from app import db
from app.models.scm_inventory import ScmUbicacionInventario
from app.models.scm_inventory_kg import ScmExistenciaMangaKg, ScmMovimientoInventarioKg, ScmSaldoInventarioKg
from app.services.scm_kg_custody_service import assert_custody_enabled
from app.services.scm_service_support import ScmServiceError
from tests.scm.test_scm_kg_production import _weigh_kg_fixture

@pytest.mark.parametrize('enabled', [False, True])
def test_intake_independent_from_disabled_custody(app, enabled):
    with app.app_context():
        app.config.update(KG_AUTOMATIC_INTAKE_ENABLED=enabled,
                          KG_CUSTODY_WRITE_ENABLED=False,
                          KG_RECEIPT_WRITE_ENABLED=False,
                          KG_PRODUCTION_LOCATION_CODE='PRODUCCION_KG')
        db.session.add(ScmUbicacionInventario(codigo='PRODUCCION_KG', nombre='Produccion',
            tipo='PUNTO_PRODUCCION', activo=True, permite_saldo_libre=True,
            clases_articulo_json=['PIEZA_COLOR']))
        db.session.commit()
        _weigh_kg_fixture()
        assert ScmExistenciaMangaKg.query.count() == int(enabled)
        assert ScmMovimientoInventarioKg.query.count() == int(enabled)
        if enabled:
            assert ScmSaldoInventarioKg.query.one().cantidad_fisica_kg == Decimal('12.000')
        with pytest.raises(ScmServiceError) as blocked:
            assert_custody_enabled()
        assert blocked.value.code == 'KG_OPERATION_NOT_ENABLED'
        # Switching off intake is prospective: it must never erase the ledger.
        app.config['KG_AUTOMATIC_INTAKE_ENABLED'] = False
        assert ScmMovimientoInventarioKg.query.count() == int(enabled)

def test_invalid_production_location_rolls_back_weighing(app):
    from app.models.scm_ot import ScmPesajeManga
    with app.app_context():
        app.config.update(KG_AUTOMATIC_INTAKE_ENABLED=True,
                          KG_CUSTODY_WRITE_ENABLED=False,
                          KG_PRODUCTION_LOCATION_CODE='MISSING')
        with pytest.raises(ScmServiceError) as blocked:
            _weigh_kg_fixture()
        assert blocked.value.code == 'KG_PRODUCTION_LOCATION_INVALID'
        assert ScmPesajeManga.query.count() == 0
        assert ScmMovimientoInventarioKg.query.count() == 0
        assert ScmExistenciaMangaKg.query.count() == 0

from datetime import datetime, timezone
from uuid import UUID, uuid4
from app.models.scm_ot import ScmManga, ScmPesajeManga, ScmControlPesoManga
from app.services.scm_weighing_service import confirm_manga_weighing, register_manga_weighing_control
from app.services.scm_ot_service import transition_color_work
from tests.scm.test_scm_kg_receipt import _seed_aggregate_color_work, _print_color_manga


def prepare_capture(app, *, created_at='2026-09-18T00:00:00+00:00', quantity=120):
    app.config.update(KG_AUTOMATIC_INTAKE_ENABLED=True,
                      KG_AUTOMATIC_INTAKE_CUTOFF_AT='2026-09-18T00:00:00+00:00',
                      KG_CUSTODY_WRITE_ENABLED=False, KG_RECEIPT_WRITE_ENABLED=False,
                      KG_PRODUCTION_LOCATION_CODE='PRODUCCION_KG')
    creator, _, _, _, _, _, _, created = _seed_aggregate_color_work(quantity=quantity)
    manga = ScmManga.query.filter_by(public_id=UUID(created['mangas'][0]['public_id'])).one()
    manga.created_at = datetime.fromisoformat(created_at)
    manga.lote_articulo.articulo.unidad_inventario = 'KG'
    if not ScmUbicacionInventario.query.filter_by(codigo='PRODUCCION_KG').first():
        db.session.add(ScmUbicacionInventario(codigo='PRODUCCION_KG', nombre='Produccion',
            tipo='PUNTO_PRODUCCION', activo=True, permite_saldo_libre=True,
            clases_articulo_json=['PIEZA_COLOR']))
    db.session.flush()
    transition_color_work(db.session, actor_id=creator.id,
        work_id=UUID(created['trabajo_color']['id']), operation_id=uuid4(),
        data={'version': created['trabajo_color']['version']}, action='iniciar')
    station, label = _print_color_manga(actor=creator, manga_id=manga.public_id,
                                      station_code='CUT-' + uuid4().hex[:8])
    db.session.commit()
    return dict(actor_id=creator.id, station_id=station.station_id, operation_id=uuid4(),
                data={'label_id': label['public_id'], 'capture_id': str(uuid4()),
                      'peso_bruto_kg': '12.100', 'tara_kg': '0.100', 'tara_fuente': 'TIPO_MANGA',
                      'pesada_at': '2026-09-18T16:55:00-05:00', 'reading_stable': True})


@pytest.mark.parametrize('control', [False, True])
@pytest.mark.parametrize('reason', ['offline_capture', 'missing_cutoff', 'naive_cutoff'])
def test_cutover_quarantines_without_partial_persistence(app, control, reason):
    with app.app_context():
        command = prepare_capture(app)
        if reason == 'offline_capture':
            command['data']['pesada_at'] = '2026-09-17T18:59:59-05:00'
        if reason == 'missing_cutoff':
            app.config['KG_AUTOMATIC_INTAKE_CUTOFF_AT'] = ''
        if reason == 'naive_cutoff':
            app.config['KG_AUTOMATIC_INTAKE_CUTOFF_AT'] = '2026-09-18T00:00:00'
        if control:
            command['data']['control_type'] = 'AVANCE_KG'
        call = register_manga_weighing_control if control else confirm_manga_weighing
        for _ in range(2):
            # SQLite legacy transaction control otherwise commits the existing
            # service's idempotency SAVEPOINT independently of its transaction.
            db.session.execute(text("BEGIN"))
            with pytest.raises(ScmServiceError) as blocked:
                call(db.session, **command)
            assert blocked.value.code == ('KG_INTAKE_CUTOFF_REQUIRED' if 'cutoff' in reason
                                          else 'KG_INTAKE_CUTOVER_REVIEW_REQUIRED')
            assert ScmPesajeManga.query.count() == 0
            assert ScmControlPesoManga.query.count() == 0
            assert ScmMovimientoInventarioKg.query.count() == 0
            assert ScmSaldoInventarioKg.query.count() == 0


def test_cutover_new_manga_replay_and_offline_after_cutoff(app):
    with app.app_context():
        command = prepare_capture(app)
        # Equivalent local offset at the exact inclusive boundary.
        app.config['KG_AUTOMATIC_INTAKE_CUTOFF_AT'] = '2026-09-17T19:00:00-05:00'
        command['data']['pesada_at'] = '2026-09-18T00:00:00+00:00'
        first = confirm_manga_weighing(db.session, **command)
        again = confirm_manga_weighing(db.session, **command)
        assert again == first
        assert ScmMovimientoInventarioKg.query.count() == 1
        assert ScmSaldoInventarioKg.query.one().cantidad_fisica_kg == Decimal('12.000')
        assert app.config['KG_CUSTODY_WRITE_ENABLED'] is False


@pytest.mark.parametrize('gross', ['11.600', '12.600'])
def test_disabled_intake_cannot_approve_unprojected_inventory_correction(app, gross):
    from tests.scm.test_scm_kg_production import _auto_final_kg_fixture, _grant_capabilities
    from app.services.scm_weighing_service import request_weighing_correction, approve_weighing_correction
    from app.models.scm_ot import ScmCorreccionPesajeManga
    with app.app_context():
        creator, approver, manga, station, label, weighed = _auto_final_kg_fixture(app, station_code='CUT-ROLLBACK')
        _grant_capabilities(creator, ('PESAJE_CORRECCION_SOLICITAR',))
        _grant_capabilities(approver, ('PESAJE_CORRECCION_APROBAR',))
        db.session.commit()
        correction = request_weighing_correction(db.session, actor_id=creator.id,
            weighing_id=UUID(weighed['weighing']['public_id']), operation_id=uuid4(),
            data={'proposed': {'peso_bruto_kg': gross, 'tara_kg': '0.100'},
                  'motivo': 'Prueba rollback'})['correction']
        app.config['KG_AUTOMATIC_INTAKE_ENABLED'] = False
        with pytest.raises(ScmServiceError) as blocked:
            approve_weighing_correction(db.session, actor_id=approver.id,
                correction_id=UUID(correction['id']), operation_id=uuid4(),
                data={'motivo_aprobacion': 'Prueba rollback'})
        assert blocked.value.code == 'KG_INTAKE_DISABLED_WITH_EXISTENCE'
        assert ScmSaldoInventarioKg.query.one().cantidad_fisica_kg == Decimal('12.000')
        assert ScmMovimientoInventarioKg.query.count() == 1
        assert ScmCorreccionPesajeManga.query.filter_by(public_id=UUID(correction['id'])).one().estado == 'PENDIENTE'


def test_disabled_intake_blocks_next_control_for_existing_stock(app):
    with app.app_context():
        command = prepare_capture(app)
        command['data']['control_type'] = 'AVANCE_KG'
        command['data']['peso_bruto_kg'] = '5.100'
        register_manga_weighing_control(db.session, **command)
        app.config['KG_AUTOMATIC_INTAKE_ENABLED'] = False
        command['operation_id'] = uuid4()
        command['data']['capture_id'] = str(uuid4())
        command['data']['peso_bruto_kg'] = '8.100'
        with pytest.raises(ScmServiceError) as blocked:
            register_manga_weighing_control(db.session, **command)
        assert blocked.value.code == 'KG_INTAKE_DISABLED_WITH_EXISTENCE'
        assert ScmControlPesoManga.query.count() == 1
        assert ScmSaldoInventarioKg.query.one().cantidad_fisica_kg == Decimal('5.000')
        assert ScmMovimientoInventarioKg.query.count() == 1



def test_force_recovery_keeps_separate_authority_and_bypasses_cutoff(app):
    from app.services.scm_kg_production_service import sync_kg_production_inventory
    from app.models.scm_ot import ScmEtiquetaManga
    with app.app_context():
        command = prepare_capture(app, created_at='2026-09-17T00:00:00+00:00')
        app.config['KG_AUTOMATIC_INTAKE_ENABLED'] = False
        weighed = confirm_manga_weighing(db.session, **command)
        app.config['KG_AUTOMATIC_INTAKE_CUTOFF_AT'] = 'INVALID'
        manga = ScmEtiquetaManga.query.filter_by(public_id=UUID(command['data']['label_id'])).one().manga
        result = sync_kg_production_inventory(db.session, actor_id=command['actor_id'], manga=manga,
            net_kg=Decimal('12.000'), operation_id=command['operation_id'], source_type='PESAJE_FINAL',
            source_id=UUID(weighed['weighing']['public_id']),
            source_at=datetime.fromisoformat('2026-09-18T00:00:00+00:00'), final=True, force=True)
        assert result['delta_kg'] == '12.000'
        assert ScmMovimientoInventarioKg.query.count() == 1


def test_close_from_old_control_http_rolls_back(app, client):
    from app.models.scm_ot import ScmEtiquetaManga, ScmTramoMangaTrabajo
    from app.models.trabajador import Trabajador
    from tests.scm.test_scm_kg_production import _grant_capabilities
    with app.app_context():
        command = prepare_capture(app)
        command['data']['control_type'] = 'AVANCE_KG'
        register_manga_weighing_control(db.session, **command)
        manga = ScmEtiquetaManga.query.filter_by(public_id=UUID(command['data']['label_id'])).one().manga
        manga_id = manga.id
        public_id = manga.public_id
        version = manga.version
        _grant_capabilities(db.session.get(Trabajador, command['actor_id']), ('MANGA_FINALIZAR_PARCIAL',))
        db.session.commit()
        app.config['KG_AUTOMATIC_INTAKE_CUTOFF_AT'] = '2026-09-19T00:00:00+00:00'
    response = client.post(f'/api/scm/v1/mangas/{public_id}/cerrar-desde-control',
        headers={'X-Actor-Id': str(command['actor_id']), 'Idempotency-Key': str(uuid4())},
        json={'version': version, 'motivo': 'Cierre anterior al corte'})
    assert response.status_code == 409
    assert response.get_json()['error']['code'] == 'KG_INTAKE_CUTOVER_REVIEW_REQUIRED'
    with app.app_context():
        db.session.expire_all()
        assert db.session.get(ScmManga, manga_id).estado == 'EN_LLENADO'
        assert ScmTramoMangaTrabajo.query.filter_by(manga_id=manga_id, estado='ACTIVO').count() == 1
        assert ScmSaldoInventarioKg.query.one().cantidad_fisica_kg == Decimal('12.000')
        assert ScmMovimientoInventarioKg.query.count() == 1


@pytest.mark.parametrize('control', [False, True])
def test_precreated_clean_manga_can_start_after_cutoff(app, control):
    with app.app_context():
        command = prepare_capture(app, created_at='2026-08-01T00:00:00+00:00')
        if control:
            command['data']['control_type'] = 'AVANCE_KG'
        call = register_manga_weighing_control if control else confirm_manga_weighing
        first = call(db.session, **command)
        again = call(db.session, **command)
        assert {k:v for k,v in again.items() if k != 'idempotent_replay'} == {k:v for k,v in first.items() if k != 'idempotent_replay'}
        assert ScmMovimientoInventarioKg.query.count() == 1
        assert ScmSaldoInventarioKg.query.one().cantidad_fisica_kg == Decimal('12.000')


@pytest.mark.parametrize('kind', ['control', 'final', 'server_time', 'recovered', 'un'])
def test_cutoff_history_cannot_be_relabelled_as_new(app, kind):
    from app.models.scm_ot import ScmEtiquetaManga
    from app.services.scm_kg_production_service import _assert_automatic_intake_cutoff
    with app.app_context():
        command = prepare_capture(app)
        if kind in ('control', 'server_time'):
            command['data']['control_type'] = 'AVANCE_KG'
            register_manga_weighing_control(db.session, **command)
            fact = ScmControlPesoManga.query.one()
            if kind == 'control': fact.pesado_at = datetime.fromisoformat('2026-09-17T00:00:00+00:00')
            else: fact.created_at = datetime.fromisoformat('2026-09-17T00:00:00+00:00')
        else:
            confirm_manga_weighing(db.session, **command)
            if kind == 'final':
                fact = ScmPesajeManga.query.one()
                fact.estado = 'ANULADO'
                fact.pesada_at = datetime.fromisoformat('2026-09-17T00:00:00+00:00')
            elif kind == 'recovered': ScmExistenciaMangaKg.query.one().resuelta_por = 'KG_HISTORY_RECOVERY'
            else: ScmPesajeManga.query.one().cantidad_confirmada = Decimal('10')
        db.session.commit()
        manga = ScmEtiquetaManga.query.filter_by(public_id=UUID(command['data']['label_id'])).one().manga
        with pytest.raises(ScmServiceError) as blocked:
            _assert_automatic_intake_cutoff(db.session,manga,datetime.fromisoformat('2026-09-20T00:00:00+00:00'),command['operation_id'])
        assert blocked.value.code == 'KG_INTAKE_CUTOVER_REVIEW_REQUIRED'
        assert ScmMovimientoInventarioKg.query.count() == 1


def test_operation_reserved_before_cutoff_rejected(app):
    from app.models.scm_ot import ScmEtiquetaManga
    from app.models.scm_auditoria import ScmOperacion
    from app.services.scm_kg_production_service import _assert_automatic_intake_cutoff
    with app.app_context():
        command = prepare_capture(app)
        confirm_manga_weighing(db.session, **command)
        operation = db.session.get(ScmOperacion,command['operation_id'])
        operation.created_at = datetime.fromisoformat('2026-09-17T00:00:00+00:00')
        db.session.commit()
        manga = ScmEtiquetaManga.query.filter_by(public_id=UUID(command['data']['label_id'])).one().manga
        with pytest.raises(ScmServiceError) as blocked:
            _assert_automatic_intake_cutoff(db.session,manga,datetime.fromisoformat('2026-09-20T00:00:00+00:00'),command['operation_id'])
        assert blocked.value.details['reason'] == 'OPERATION_BEFORE_CUTOFF'
        assert ScmMovimientoInventarioKg.query.count() == 1


def test_old_control_blocks_new_final_transaction(app):
    with app.app_context():
        command = prepare_capture(app)
        control = {**command,'data':{**command['data'],'control_type':'AVANCE_KG','peso_bruto_kg':'5.100'}}
        register_manga_weighing_control(db.session, **control)
        fact = ScmControlPesoManga.query.one()
        fact.created_at = datetime.fromisoformat('2026-09-17T00:00:00+00:00')
        db.session.commit()
        command['operation_id'] = uuid4()
        command['data']['capture_id'] = str(uuid4())
        with pytest.raises(ScmServiceError) as blocked:
            confirm_manga_weighing(db.session, **command)
        assert blocked.value.code == 'KG_INTAKE_CUTOVER_REVIEW_REQUIRED'
        assert ScmPesajeManga.query.count() == 0
        assert ScmControlPesoManga.query.count() == 1
        assert ScmSaldoInventarioKg.query.one().cantidad_fisica_kg == Decimal('5.000')


@pytest.mark.parametrize('historical', [False, True])
def test_cutover_correction_uses_original_history(app, historical):
    from tests.scm.test_scm_kg_production import _auto_final_kg_fixture, _grant_capabilities
    from app.services.scm_weighing_service import request_weighing_correction, approve_weighing_correction
    from app.models.scm_ot import ScmCorreccionPesajeManga
    with app.app_context():
        creator, approver, manga, station, label, weighed = _auto_final_kg_fixture(app, station_code='CUT-CORRECTION')
        app.config['KG_AUTOMATIC_INTAKE_CUTOFF_AT'] = '2026-09-18T00:00:00+00:00'
        if historical:
            ScmExistenciaMangaKg.query.one().resuelta_por = 'KG_HISTORY_RECOVERY'
        _grant_capabilities(creator, ('PESAJE_CORRECCION_SOLICITAR',))
        _grant_capabilities(approver, ('PESAJE_CORRECCION_APROBAR',))
        db.session.commit()
        correction = request_weighing_correction(db.session,actor_id=creator.id,
            weighing_id=UUID(weighed['weighing']['public_id']),operation_id=uuid4(),
            data={'proposed':{'peso_bruto_kg':'12.600','tara_kg':'0.100'},'motivo':'Corte KG'})['correction']
        args=dict(actor_id=approver.id,correction_id=UUID(correction['id']),operation_id=uuid4(),
                  data={'motivo_aprobacion':'Correccion prueba'})
        if historical:
            with pytest.raises(ScmServiceError) as blocked:
                approve_weighing_correction(db.session,**args)
            assert blocked.value.code == 'KG_INTAKE_CUTOVER_REVIEW_REQUIRED'
            assert ScmCorreccionPesajeManga.query.one().estado == 'PENDIENTE'
        else:
            approve_weighing_correction(db.session,**args)
        assert ScmSaldoInventarioKg.query.one().cantidad_fisica_kg == Decimal('12.000' if historical else '12.500')
        assert ScmMovimientoInventarioKg.query.count() == (1 if historical else 2)


def test_precreated_controls_and_final_credit_only_cumulative_net(app):
    with app.app_context():
        command=prepare_capture(app,created_at='2026-08-01T00:00:00+00:00')
        for gross in ('5.100','8.100'):
            args={**command,'operation_id':uuid4(),'data':{**command['data'],
                'capture_id':str(uuid4()),'control_type':'AVANCE_KG','peso_bruto_kg':gross}}
            register_manga_weighing_control(db.session,**args)
        confirm_manga_weighing(db.session,**command)
        assert ScmMovimientoInventarioKg.query.count()==3
        assert ScmSaldoInventarioKg.query.one().cantidad_fisica_kg==Decimal('12.000')
