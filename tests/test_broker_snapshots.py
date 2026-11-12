"""Offline broker request counts, retained snapshots and process coordination."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time
from unittest.mock import patch

import pytest
import requests

from strategies.broker_reads import BrokerReads, BrokerReadUnavailable, fresh_broker_reads, install_sdk_reads


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def response(payload=None, status=200, headers=None):
    value = requests.Response()
    value.status_code = status
    value._content = json.dumps(payload).encode()
    value.headers.update(headers or {})
    return value


class Transport:
    def __init__(self):
        self.calls = Counter()
        self.status = {}
        self.active = False
        self.empty = False
        self.missing = set()

    def __call__(self, method, url, **kwargs):
        name = url.rsplit('/', 1)[-1]
        self.calls[name] += 1
        failure = self.status.get(name)
        if isinstance(failure, Exception):
            raise failure
        if failure:
            return response({}, failure)
        payloads = {
            'balances': {'balances': {'total_equity': 100000, 'total_cash': 90000,
                                     'margin': {'option_buying_power': 50000, 'option_requirement': 5000}}},
            'positions': {'positions': None if self.empty else {'position': {'symbol': 'XSP261120P00500000', 'quantity': -1, 'cost_basis': -200}}},
            'orders': {'orders': {'order': [{'id': 1, 'status': 'open', 'symbol': 'XSP'}]} if self.active else None},
            'quotes': {'quotes': {'quote': [{'symbol': s, 'bid': 1, 'ask': 1.2, 'bid_date': 1793629800000}
                        for s in kwargs.get('params', {}).get('symbols', '').split(',') if s not in self.missing]}},
        }
        return response(payloads[name])


@pytest.fixture
def setup_reads(tmp_path, monkeypatch):
    for name in ('BALANCES_INTERVAL', 'POSITIONS_INTERVAL', 'ORDERS_IDLE_INTERVAL', 'ORDERS_ACTIVE_INTERVAL', 'QUOTES_INTERVAL', 'DISPLAY_MAX_AGE'):
        monkeypatch.delenv('BROKER_' + name, raising=False)
    clock, transport = Clock(), Transport()
    reads = BrokerReads('secret', 'private-account', path=tmp_path/'snapshots.sqlite3',
                        transport=transport, clock=clock, sleep=clock.advance, jitter=lambda: 0, market_open=lambda: True)
    return reads, clock, transport


def test_independent_ttls_and_outage_retention(setup_reads):
    reads, clock, transport = setup_reads
    endpoint = '/accounts/private-account/positions'
    original = reads.get(endpoint)
    assert reads.get(endpoint) == original
    assert transport.calls['positions'] == 1
    clock.advance(16)
    transport.status['positions'] = 500
    with pytest.raises(BrokerReadUnavailable, match='HTTP 500'):
        reads.get(endpoint)
    snap = reads.snapshot('positions')
    assert snap['data'] == original
    assert snap['status'] == 'stale'
    assert snap['success_at'] == 1000
    attempts = transport.calls['positions']
    with pytest.raises(BrokerReadUnavailable):
        reads.get(endpoint, fresh=True)
    assert transport.calls['positions'] == attempts
    clock.advance(10)
    transport.status.clear()
    transport.empty = True
    assert reads.get(endpoint)['positions'] is None
    assert reads.snapshot('positions')['status'] == 'ready'


@pytest.mark.parametrize('failure', [500, requests.exceptions.RetryError('offline'), requests.Timeout('offline')])
def test_failed_balance_does_not_skip_inventory_and_orders(setup_reads, failure):
    reads, _, transport = setup_reads
    transport.status['balances'] = failure
    reads.collect_once()
    assert reads.snapshot('balances')['data'] is None
    assert reads.snapshot('positions')['data'] is not None
    assert reads.snapshot('orders')['data'] == {'orders': None}
    assert transport.calls['orders'] == 1


def test_fresh_execution_never_receives_stale_retained_values(setup_reads):
    reads, clock, transport = setup_reads
    endpoint = '/accounts/private-account/positions'
    reads.get(endpoint)
    clock.advance(1)
    with fresh_broker_reads():
        reads.get(endpoint)
    assert transport.calls['positions'] == 2
    transport.status['positions'] = 503
    with fresh_broker_reads(), pytest.raises(BrokerReadUnavailable):
        reads.get(endpoint)
    assert reads.snapshot('positions')['data'] is not None


@pytest.mark.parametrize('active,standard', [(False, 8), (True, 18)])
def test_one_minute_call_budget_shared_by_multiple_viewers(setup_reads, active, standard):
    reads, clock, transport = setup_reads
    transport.active = active
    for _ in range(60):
        reads.collect_once()
        for _ in range(2):
            reads.display()
        clock.advance(1)
    assert sum(transport.calls[name] for name in ('balances', 'positions', 'orders')) == standard
    assert transport.calls['quotes'] == 12
    assert len(reads.metrics()['attempts']) == standard + 12


def test_background_budget_reserves_fresh_execution_capacity(setup_reads):
    reads, _, transport = setup_reads
    for _ in range(30):
        reads.get('/accounts/private-account/positions', fresh=True)
    with pytest.raises(BrokerReadUnavailable, match='budget'):
        reads.get('/accounts/private-account/balances', fresh=True)
    with fresh_broker_reads():
        reads.get('/accounts/private-account/orders')
    assert sum(transport.calls.values()) == 31


def test_account_cache_isolation_but_token_quota_is_shared(setup_reads):
    reads, clock, transport = setup_reads
    other = BrokerReads('secret', 'other-account', path=reads.path, transport=transport,
                         clock=clock, sleep=clock.advance, jitter=lambda: 0)
    reads.get('/accounts/private-account/positions')
    assert other.snapshot('positions')['data'] is None
    assert other.metrics()['quotas'][0]['used'] == 1
    live = BrokerReads('secret', 'private-account', False, path=reads.path, transport=transport)
    assert live.snapshot('positions')['data'] is None
    with pytest.raises(BrokerReadUnavailable, match='scope'):
        reads.get('/accounts/other-account/positions')


def test_concurrent_readers_coalesce_across_store_instances(tmp_path):
    transport, lock = Transport(), threading.Lock()
    def slow(*args, **kwargs):
        time.sleep(.08)
        with lock:
            return transport(*args, **kwargs)
    clients = [BrokerReads('secret', 'private-account', path=tmp_path/'snapshots.sqlite3', transport=slow) for _ in range(8)]
    barrier = threading.Barrier(8)
    def read(client):
        barrier.wait()
        return client.get('/accounts/private-account/positions')
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(read, clients))
    assert all(result == results[0] for result in results)
    assert transport.calls['positions'] == 1


def test_expired_lease_cannot_publish_and_restart_keeps_success(setup_reads):
    reads, clock, transport = setup_reads
    reads.get('/accounts/private-account/positions')
    _, lease = reads._claim('positions', clock() + 2, True)
    clock.advance(5)
    with pytest.raises(BrokerReadUnavailable, match='Expired'):
        reads._publish('positions', lease, {'positions': None})
    restarted = BrokerReads('secret', 'private-account', path=reads.path, transport=transport, clock=clock)
    assert restarted.snapshot('positions')['data']['positions'] is not None


def test_collector_prefers_bot_and_dashboard_takes_over_after_expiry(setup_reads):
    bot, clock, transport = setup_reads
    dashboard = BrokerReads('secret', 'private-account', path=bot.path, transport=transport,
                             clock=clock, sleep=clock.advance, jitter=lambda: 0)
    assert bot.collect_once('bot')
    assert not dashboard.collect_once('dashboard')
    clock.advance(31)
    assert dashboard.collect_once('dashboard')


def test_quote_batches_partial_data_retention_and_recovery(setup_reads):
    reads, clock, transport = setup_reads
    reads.quotes(['A', 'B', 'A'])
    assert transport.calls['quotes'] == 1
    reads.quotes(['A'])
    assert transport.calls['quotes'] == 1
    clock.advance(6)
    transport.missing = {'B'}
    with pytest.raises(BrokerReadUnavailable, match='Incomplete'):
        reads.quotes(['A', 'B'])
    assert reads.snapshot('quote:B')['status'] == 'stale'
    assert reads.snapshot('quote:B')['data']['bid'] == 1
    clock.advance(6)
    transport.missing.clear()
    reads.quotes(['A', 'B'])
    assert reads.snapshot('quote:B')['status'] == 'ready'


def test_quote_server_failure_has_shared_probe_cooldown(setup_reads):
    reads, _, transport = setup_reads
    transport.status['quotes'] = 500
    for _ in range(3):
        with pytest.raises(BrokerReadUnavailable):
            reads.quotes(['A', 'B'])
    assert transport.calls['quotes'] == 2


def test_missing_symbol_does_not_block_fresh_quotes_for_other_positions(setup_reads):
    reads, _, transport = setup_reads
    transport.missing = {'EXPIRED'}
    with pytest.raises(BrokerReadUnavailable):
        reads.quotes(['EXPIRED', 'HELD'])
    # An individual symbol's cooldown must not block other positions either.
    with pytest.raises(BrokerReadUnavailable):
        reads.quotes(['EXPIRED'])
    attempts = transport.calls['quotes']
    with fresh_broker_reads():
        payload = reads.quotes(['HELD'])
    assert payload['quotes']['quote'][0]['symbol'] == 'HELD'
    assert transport.calls['quotes'] == attempts + 1


@pytest.mark.parametrize('missing', ['A_EXPIRED', 'Z_EXPIRED'])
@pytest.mark.parametrize('fresh', [False, True])
def test_cooling_symbol_in_collector_batch_does_not_poison_healthy_quote(setup_reads, missing, fresh):
    reads, clock, transport = setup_reads
    reads.quotes(['HELD'])
    clock.advance(6)
    transport.missing = {missing}
    with pytest.raises(BrokerReadUnavailable):
        reads.quotes([missing])
    missing_snapshot = reads.snapshot('quote:' + missing)
    attempts = transport.calls['quotes']
    with pytest.raises(BrokerReadUnavailable):
        reads.quotes(['HELD', missing], fresh=fresh)
    healthy = reads.snapshot('quote:HELD', ttl=5)
    assert healthy['error'] == ''
    assert healthy['age'] == 0
    assert reads.snapshot('quote:' + missing) == missing_snapshot
    assert transport.calls['quotes'] == attempts + 1
    assert reads.quotes(['HELD'])['quotes']['quote'][0]['symbol'] == 'HELD'


def test_partial_first_chunk_does_not_skip_later_healthy_quotes(setup_reads):
    reads, _, transport = setup_reads
    transport.missing = {'A_EXPIRED'}
    symbols = ['A_EXPIRED', *(f'HELD_{index:03d}' for index in range(101))]
    with pytest.raises(BrokerReadUnavailable, match='Incomplete'):
        reads.quotes(symbols)
    assert transport.calls['quotes'] == 2
    assert all(reads.snapshot('quote:' + symbol)['status'] == 'ready' for symbol in symbols[1:])


def test_all_cooling_symbols_preserve_cooldowns_without_transport_retry(setup_reads):
    reads, _, transport = setup_reads
    transport.missing = {'EXPIRED'}
    with pytest.raises(BrokerReadUnavailable):
        reads.quotes(['EXPIRED'])
    missing = reads.snapshot('quote:EXPIRED')
    attempts = transport.calls['quotes']
    with pytest.raises(BrokerReadUnavailable):
        reads.quotes(['EXPIRED'], fresh=True)
    assert transport.calls['quotes'] == attempts
    assert reads.snapshot('quote:EXPIRED') == missing


def test_collector_refreshes_healthy_demand_while_expired_order_cools(setup_reads):
    reads, clock, transport = setup_reads
    reads.quotes(['HELD'])
    clock.advance(6)
    transport.missing = {'XSP'}
    transport.active = True  # The active order contributes XSP to each collector batch.
    with pytest.raises(BrokerReadUnavailable):
        reads.quotes(['XSP'])
    assert reads.collect_once('bot')
    assert reads.snapshot('quote:HELD', ttl=5)['status'] == 'ready'
    assert reads.quotes(['HELD'])['quotes']['quote'][0]['symbol'] == 'HELD'


def test_writes_are_not_replayed_and_invalidate_snapshots(setup_reads):
    reads, _, transport = setup_reads
    reads.get('/accounts/private-account/positions')
    with pytest.raises(requests.Timeout):
        reads.write(lambda: (_ for _ in ()).throw(requests.Timeout('ambiguous write')))
    assert reads.snapshot('positions')['status'] == 'stale'
    reads.get('/accounts/private-account/positions')
    assert transport.calls['positions'] == 2
    assert reads.metrics()['quotas'][1]['used'] == 1


def test_metrics_do_not_disclose_account_or_token(setup_reads):
    reads, _, _ = setup_reads
    reads.get('/accounts/private-account/positions')
    metrics = json.dumps(reads.metrics())
    assert 'private-account' not in metrics
    assert 'secret' not in metrics


def test_deployed_sdk_request_adapter_batches_and_parses_actual_schema(setup_reads):
    sdk = pytest.importorskip('lumiwealth_tradier')
    from types import SimpleNamespace
    reads, _, transport = setup_reads
    api = sdk.Tradier('private-account', 'secret', True)
    broker = SimpleNamespace(tradier=api)
    with patch('strategies.broker_reads.get_broker_reads', return_value=reads):
        install_sdk_reads(broker, 'secret', 'private-account')
        install_sdk_reads(broker, 'secret', 'private-account')
    assert not api.account.get_positions().empty
    assert not api.account.get_positions().empty
    assert transport.calls['positions'] == 1
    assert len(api.market.get_quotes(['A', 'B'])) == 2
    assert transport.calls['quotes'] == 1


def test_malformed_and_expired_data_do_not_replace_success(setup_reads):
    reads, clock, transport = setup_reads
    reads.get('/accounts/private-account/positions')
    reads.transport = lambda *args, **kwargs: response({'positions': {}})
    clock.advance(16)
    with pytest.raises(BrokerReadUnavailable, match='Invalid'):
        reads.get('/accounts/private-account/positions')
    clock.advance(300)
    snapshot = reads.display()['positions']
    assert snapshot['status'] == 'expired'
    assert snapshot['data']['positions'] is not None
    assert snapshot['success_at'] == 1000


def test_quota_violation_and_provider_headers_stop_other_processes(setup_reads):
    reads, clock, transport = setup_reads
    rejected = response({}, 400, {'X-Ratelimit-Allowed': '60', 'X-Ratelimit-Used': '60',
                                 'X-Ratelimit-Available': '0', 'X-Ratelimit-Expiry': '1060000'})
    rejected._content = b'Quota Violation: Expires 1060000'
    reads.transport = lambda *args, **kwargs: rejected
    with pytest.raises(BrokerReadUnavailable):
        reads.get('/accounts/private-account/positions')
    other = BrokerReads('secret', 'other', path=reads.path, transport=transport, clock=clock)
    with fresh_broker_reads(), pytest.raises(BrokerReadUnavailable, match='budget'):
        other.get('/accounts/other/positions')
    assert transport.calls == {}
    assert reads.metrics()['quotas'][0]['used'] == 60


def test_confirmed_order_change_invalidates_inventory(setup_reads):
    reads, clock, transport = setup_reads
    reads.collect_once()
    transport.active = True
    clock.advance(1)
    reads.get('/accounts/private-account/orders', fresh=True)
    assert reads.snapshot('positions')['error'] == 'Refresh required'
    reads.get('/accounts/private-account/positions')
    assert transport.calls['positions'] == 2


def test_collector_does_not_keep_expired_quote_demand_alive(setup_reads):
    reads, clock, _ = setup_reads
    reads.quotes(['TEMP'])
    clock.advance(901)
    reads.collect_once()
    with reads.db() as db:
        assert db.execute("SELECT 1 FROM demand WHERE symbol='TEMP'").fetchone() is None


def test_forced_quote_failure_marks_retained_quote_stale(setup_reads):
    reads, clock, transport = setup_reads
    reads.quotes(['A'])
    clock.advance(1)
    transport.status['quotes'] = 500
    with fresh_broker_reads(), pytest.raises(BrokerReadUnavailable):
        reads.quotes(['A'])
    assert reads.snapshot('quote:A')['status'] == 'stale'


def test_dashboard_renders_retained_snapshot_without_http(setup_reads):
    import importlib.util
    from pathlib import Path
    reads, clock, transport = setup_reads
    spec = importlib.util.spec_from_file_location('snapshot_adapters', Path(__file__).resolve().parents[1] /
        'reflex_dashboard/reflex_dashboard/data/adapters.py')
    adapters = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapters)
    def render():
        result = {'orders': [], 'positions': [], 'equity': None}
        adapters._apply_broker_snapshot(result, reads.display())
        return result
    reads.collect_once()
    good = render()
    assert good['equity'] == 100000
    assert good['unrealized_pnl'] == pytest.approx(90)
    clock.advance(31)
    transport.status['positions'] = 500
    reads.collect_once()
    attempts = sum(transport.calls.values())
    for _ in range(50):
        retained = render()
        assert retained['raw_positions'] == good['raw_positions']
        assert retained['unrealized_pnl'] == pytest.approx(90)
        assert retained['sync_status'] == 'stale'
    assert sum(transport.calls.values()) == attempts
    clock.advance(10)
    transport.status.clear()
    transport.empty = True
    reads.collect_once()
    assert render()['raw_positions'] == []


@pytest.mark.parametrize('storage_failure', ['readonly', 'unavailable'])
def test_full_dashboard_refresh_survives_cache_storage_failure(setup_reads, monkeypatch, storage_failure):
    import sqlite3
    import sys
    from contextlib import contextmanager
    from pathlib import Path
    from unittest.mock import Mock

    pytest.importorskip('reflex')
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'reflex_dashboard'))
    from reflex_dashboard.data import adapters
    from reflex_dashboard.state import DashboardState

    reads, _, transport = setup_reads
    reads.collect_once()
    attempts = sum(transport.calls.values())
    env = {'TRADING_BROKER': 'tradier', 'TRADIER_ACCESS_TOKEN': 'secret',
           'TRADIER_ACCOUNT_NUMBER': 'private-account'}
    monkeypatch.setattr(adapters, 'PROJECT_ROOT', reads.path.parent)
    monkeypatch.setattr(adapters, 'dotenv_values', lambda _: env)
    packages = [{'package_id': 'opening', 'trade_id': 'retained-trade', 'strategy_id': 'IC_V23',
                 'state': 'open', 'intent': 'open', 'legs': [{
                     'asset_key': json.dumps({'symbol': 'XSP', 'expiration': '2026-11-20',
                                              'right': 'put', 'strike': 500}),
                     'filled_signed_quantity': -1, 'requested_signed_quantity': -1, 'fill_price': 2}]}]
    monkeypatch.setattr(adapters, 'read_packages', lambda _: packages)
    start = Mock()
    monkeypatch.setattr(reads, 'start', start)

    @contextmanager
    def readonly_db(write=False):
        connection = sqlite3.connect(reads.path.as_uri() + '?mode=ro', uri=True)
        connection.row_factory = sqlite3.Row
        try:
            if write:
                connection.execute('BEGIN IMMEDIATE')
            yield connection
        finally:
            connection.close()

    if storage_failure == 'readonly':
        monkeypatch.setattr(reads, 'db', readonly_db)
        monkeypatch.setattr('strategies.broker_reads.get_broker_reads', lambda *args: reads)
    else:
        monkeypatch.setattr('strategies.broker_reads.get_broker_reads',
                            Mock(side_effect=sqlite3.OperationalError('unable to open database file')))

    state = DashboardState(_reflex_internal_init=True)
    state.load_all()
    assert state.configured_variant_count > 0
    assert state.active_grid_str != 'Loading'
    assert 'storage' in state.broker_sync_message.lower()
    if storage_failure == 'readonly':
        assert state.equity == 100000
        assert len(state.position_records) == 1
        assert state.position_data_status == 'ready'
        assert state.broker_sync_status == 'stale'
        assert 'read-only' in state.broker_sync_message
    else:
        assert state.equity is None
        assert state.position_data_status == 'unavailable'
        assert state.broker_sync_status == 'unavailable'
    start.assert_not_called()
    assert sum(transport.calls.values()) == attempts


def test_real_broker_cannot_convert_budget_or_transport_failure_to_empty_inventory(setup_reads):
    sdk = pytest.importorskip('lumiwealth_tradier')
    from lumibot.brokers.tradier import Tradier
    from lumibot.brokers.broker import Broker
    from unittest.mock import Mock
    reads, _, transport = setup_reads
    broker = Tradier.__new__(Tradier)
    Broker.__init__(broker, name='Tradier', data_source=Mock(), connect_stream=False, start_orders_thread=False)
    broker.tradier = sdk.Tradier('private-account', 'secret', True)
    with patch('strategies.broker_reads.get_broker_reads', return_value=reads):
        install_sdk_reads(broker, 'secret', 'private-account')
    transport.status['positions'] = requests.Timeout('offline')
    with pytest.raises(BrokerReadUnavailable):
        broker._pull_positions(None)
    transport.status['orders'] = requests.Timeout('offline')
    with pytest.raises(BrokerReadUnavailable):
        broker._pull_broker_all_orders()

    from datetime import date
    from lumibot.entities import Asset, Position
    held = Position('owner', Asset('XSP', asset_type='option', expiration=date(2026, 11, 20),
                                  strike=500, right='put'), -1)
    broker._filled_positions.append(held)
    with patch.object(reads, 'get', side_effect=BrokerReadUnavailable('standard request budget unavailable')):
        # Exercise the installed SDK's polling -> sync_positions -> adapter path.
        broker.do_polling()
    assert held in broker._filled_positions.get_list()
    assert held.quantity == -1


def test_native_option_package_batch_preserves_asset_identity(setup_reads):
    pytest.importorskip('lumibot')
    from datetime import date
    from types import SimpleNamespace
    from lumibot.entities import Asset
    from strategies.iron_condor import SPXIronCondor
    reads, _, transport = setup_reads
    strategy = SPXIronCondor.__new__(SPXIronCondor)
    strategy.broker = SimpleNamespace(_shared_broker_reads=reads)
    assets = [Asset('XSP', asset_type='option', expiration=date(2026, 11, 20), right='put', strike=strike)
              for strike in (490, 495, 500, 505)]
    quotes = strategy._fetch_quotes(assets)
    assert [quote.asset for quote in quotes] == assets
    assert all(quote.bid == 1 and quote.ask == 1.2 for quote in quotes)
    assert transport.calls['quotes'] == 1


def test_write_fences_a_quote_response_started_before_the_mutation(setup_reads):
    reads, _, transport = setup_reads
    def mutate_during_fetch(*args, **kwargs):
        reads.write(lambda: None)
        return transport(*args, **kwargs)
    reads.transport = mutate_during_fetch
    with pytest.raises(BrokerReadUnavailable):
        reads.quotes(['A'])
    assert reads.snapshot('quote:A')['data'] is None


def _process_reader(path, start, ready, calls):
    def transport(*args, **kwargs):
        with calls.get_lock():
            calls.value += 1
        time.sleep(.1)
        return response({'positions': None})
    reads = BrokerReads('secret', 'private-account', path=path, transport=transport)
    ready.put(True)
    start.wait(10)
    reads.get('/accounts/private-account/positions')


def test_resource_lease_coalesces_separate_processes(tmp_path):
    import multiprocessing
    ctx = multiprocessing.get_context('spawn')
    start, ready, calls = ctx.Event(), ctx.Queue(), ctx.Value('i', 0)
    path = str(tmp_path / 'snapshots.sqlite3')
    workers = [ctx.Process(target=_process_reader, args=(path, start, ready, calls)) for _ in range(2)]
    try:
        for worker in workers:
            worker.start()
        for _ in workers:
            assert ready.get(timeout=10)
        start.set()
        for worker in workers:
            worker.join(10)
            assert worker.exitcode == 0
        assert calls.value == 1
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(2)


def test_retry_after_survives_a_quota_window_reset(setup_reads):
    reads, clock, _ = setup_reads
    reads._reserve('standard', 'background')
    clock.advance(59)
    reads._record(response({}, 503, {'Retry-After': '10'}), category='standard',
                  endpoint='/accounts/{account}/positions', caller='test', started=clock(), retry=0)
    clock.advance(2)
    with pytest.raises(BrokerReadUnavailable, match='budget'):
        reads._reserve('standard', 'execution')


def test_closed_hours_slow_routine_reads_but_not_pending_orders(setup_reads):
    reads, clock, transport = setup_reads
    reads.market_open = lambda: False
    reads.collect_once()
    clock.advance(16)
    reads.collect_once()
    assert transport.calls['positions'] == 1
    assert transport.calls['quotes'] == 1
    transport.active = True
    reads.get('/accounts/private-account/orders', fresh=True)
    clock.advance(6)
    reads.collect_once()
    assert transport.calls['orders'] == 3


def test_twenty_strategies_two_positions_and_two_viewers_share_one_schedule(setup_reads):
    reads, clock, transport = setup_reads
    transport.active = True
    symbols = [f'XSP261120P{strike * 1000:08d}' for strike in range(400, 560)]
    reads.demand(symbols)
    for second in range(60):
        reads.collect_once('bot')
        if second % 5 == 0:
            for strategy in range(20):
                for position in range(2):
                    reads.get('/accounts/private-account/positions')
                    reads.get('/accounts/private-account/orders')
                    start = strategy * 8 + position * 4
                    reads.quotes(symbols[start:start+4])
        for _ in range(2):
            reads.display()
        clock.advance(1)
    assert sum(transport.calls[key] for key in ('balances', 'positions', 'orders')) == 18
    assert transport.calls['quotes'] == 24  # Two batches of at most 100 symbols.
    assert len(reads.metrics()['attempts']) == 42


def test_collector_orders_include_tags_required_for_sdk_routing(setup_reads):
    reads, _, transport = setup_reads
    seen = []
    def tagged(method, url, **kwargs):
        if url.endswith('/orders'):
            seen.append(kwargs['params'])
        return transport(method, url, **kwargs)
    reads.transport = tagged
    reads.get('/accounts/private-account/orders')
    reads.get('/accounts/private-account/orders', {'includeTags': True})
    assert seen == [{'includeTags': True}]


@pytest.mark.parametrize("falsy_position", [False, 0, ""])
def test_malformed_falsy_inventory_rejected_and_retains_data(setup_reads, falsy_position):
    reads, clock, transport = setup_reads
    endpoint = '/accounts/private-account/positions'
    original = reads.get(endpoint)
    clock.advance(16)
    reads.transport = lambda *args, **kwargs: response({'positions': {'position': falsy_position}})
    with pytest.raises(BrokerReadUnavailable, match='Malformed|Invalid'):
        reads.get(endpoint)
    # The previous successful data must be retained
    snap = reads.snapshot('positions')
    assert snap['data'] == original
    assert snap['status'] == 'stale'


def test_storage_failure_sdk_guard(setup_reads):
    import sqlite3
    from types import SimpleNamespace
    reads, _, transport = setup_reads
    client = SimpleNamespace(
        account=SimpleNamespace(request=lambda *args, **kwargs: {}),
        orders=SimpleNamespace(request=lambda *args, **kwargs: {}),
        market=SimpleNamespace(request=lambda *args, **kwargs: {}),
    )
    # Simulate SDK method that catches exceptions and swallows them as empty list
    def _pull_positions():
        try:
            return client.account.request("accounts/private-account/positions")
        except Exception:
            return []

    broker = SimpleNamespace(tradier=client, _pull_positions=_pull_positions)
    with patch('strategies.broker_reads.get_broker_reads', return_value=reads):
        install_sdk_reads(broker, 'secret', 'private-account')

    with patch.object(reads, "get", side_effect=sqlite3.OperationalError("disk I/O error")):
        with pytest.raises(BrokerReadUnavailable, match="storage or adapter failure"):
            broker._pull_positions()
