import copy
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'fixtures' / 'stress'
sys.path.insert(0, str(ROOT / 'src'))

from loan_schedule.calculation import half_up
from loan_schedule.stress import validate_input, build_scenario


def call(*args):
    env = dict(os.environ)
    env['PYTHONPATH'] = str(ROOT / 'src') + (
        os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
    return subprocess.run([sys.executable, '-m', 'loan_schedule.cli', *map(str, args)],
                          capture_output=True, text=True, env=env)


def zero_loan(loan_id, principal=100, periods=2):
    return {'loan_id': loan_id,
            'contract': {'principal': principal, 'periods': periods,
                         'rate_numerator': 0, 'rate_denominator': 1}}


def base_input(overrides=None):
    data = {
        'pool_id': 'POOL-X', 'horizon': 3,
        'loans': [zero_loan('L1'), zero_loan('L2')],
        'exits': [{'loan_id': 'L1', 'kind': 'default', 'period': 1,
                   'recovery_numerator': 1, 'recovery_denominator': 1,
                   'recovery_lag': 2}],
        'senior': {'principal': 120, 'rate_numerator': 0, 'rate_denominator': 1},
        'junior': {'principal': 80, 'rate_numerator': 0, 'rate_denominator': 1},
        'reserve': 0,
        'cleanup': {'threshold_numerator': 1, 'threshold_denominator': 4,
                    'price_numerator': 1, 'price_denominator': 1},
    }
    if overrides:
        data.update(overrides)
    return data


def loan_map(rows):
    return {(r['loan_id'], r['period']): r for r in rows}


def pool_map(rows):
    return {r['period']: r for r in rows}


def tranche_map(rows):
    return {(r['tranche'], r['period']): r for r in rows}


class ValidationTests(unittest.TestCase):
    def assert_invalid(self, data, needle=None):
        with self.assertRaises((ValueError, TypeError)) as caught:
            validate_input(data)
        if needle is not None:
            self.assertIn(needle, str(caught.exception))

    def test_minimal_valid(self):
        spec = validate_input(base_input())
        self.assertEqual(spec['initial_principal'], 200)

    def test_unknown_top_field(self):
        data = base_input(); data['nope'] = 1
        self.assert_invalid(data, 'expected object fields')

    def test_boolean_rejected_everywhere(self):
        data = base_input(); data['horizon'] = True
        self.assert_invalid(data, 'integer')
        data = base_input(); data['reserve'] = False
        self.assert_invalid(data, 'integer')
        data = base_input(); data['loans'][0]['contract']['principal'] = True
        self.assert_invalid(data, 'integer')

    def test_horizon_bounds(self):
        self.assert_invalid(base_input({'horizon': 0}))
        self.assert_invalid(base_input({'horizon': 49}))

    def test_periods_must_fit_horizon(self):
        data = base_input()
        data['loans'][1]['contract']['periods'] = 4
        self.assert_invalid(data, 'exceed horizon')

    def test_duplicate_loan_id(self):
        data = base_input()
        data['loans'][1]['loan_id'] = 'L1'
        self.assert_invalid(data, 'duplicate loan_id')

    def test_unknown_exit_reference_and_duplicate_exit(self):
        data = base_input()
        data['exits'].append({'loan_id': 'GHOST', 'kind': 'prepay', 'period': 1})
        self.assert_invalid(data, 'unknown loan_id')
        data = base_input()
        data['exits'].append(dict(data['exits'][0]))
        self.assert_invalid(data, 'more than one exit')

    def test_exit_unknown_field_and_bad_kind(self):
        data = base_input()
        data['exits'][0]['extra'] = 3
        self.assert_invalid(data, 'unknown field')
        data = base_input()
        data['exits'][0]['kind'] = 'repurchase'
        self.assert_invalid(data, 'kind must be')

    def test_default_period_within_contract_and_lag_within_horizon(self):
        data = base_input()
        data['exits'][0]['period'] = 3
        self.assert_invalid(data, 'within 1..2')
        data = base_input()
        data['exits'][0]['recovery_lag'] = 3
        self.assert_invalid(data, 'exceeds horizon')

    def test_recovery_fraction_bounds(self):
        data = base_input()
        data['exits'][0]['recovery_numerator'] = 2
        data['exits'][0]['recovery_denominator'] = 1
        self.assert_invalid(data, 'recovery fraction')

    def test_tranche_total_must_equal_assets(self):
        data = base_input()
        data['junior'] = dict(data['junior'], principal=79)
        self.assert_invalid(data, 'tranche principal total')

    def test_rate_must_not_exceed_one(self):
        data = base_input()
        data['loans'][0]['contract']['rate_numerator'] = 2
        self.assert_invalid(data, 'rate must be <= 1')
        data = base_input()
        data['senior'] = dict(data['senior'], rate_numerator=2, rate_denominator=1)
        self.assert_invalid(data, 'rate must be <= 1')

    def test_reserve_bounds(self):
        self.assert_invalid(base_input({'reserve': 201}), 'reserve')

    def test_cleanup_bounds(self):
        data = base_input()
        data['cleanup'] = dict(data['cleanup'], threshold_numerator=2,
                               threshold_denominator=1)
        self.assert_invalid(data, 'threshold must be within 0..1')
        data = base_input()
        data['cleanup'] = dict(data['cleanup'], price_numerator=3,
                               price_denominator=1)
        self.assert_invalid(data, 'price ratio must be within 0..2')

    def test_aggregate_principal_cap(self):
        loans = [zero_loan('L%d' % i, principal=10 ** 11) for i in range(11)]
        data = base_input({'loans': loans,
                           'senior': {'principal': 10 ** 12, 'rate_numerator': 0,
                                      'rate_denominator': 1},
                           'junior': {'principal': 10 ** 12, 'rate_numerator': 0,
                                      'rate_denominator': 1}})
        self.assert_invalid(data, 'aggregate loan principal')

    def test_contract_period_sum_cap(self):
        data = base_input({'horizon': 48})
        data['loans'] = [zero_loan('L%d' % i, principal=10000, periods=48)
                         for i in range(418)]
        self.assert_invalid(data, 'sum of contract periods')


class EngineAcceptanceTests(unittest.TestCase):
    def test_asset_default_lag_and_normal_amortization(self):
        data = base_input()
        # A zero sale price can never cover the debt, so the defaulted
        # recovery survives until its scheduled arrival instead of being sold.
        data['cleanup'] = dict(data['cleanup'], price_numerator=0, price_denominator=1)
        assets, _, pools = build_scenario(validate_input(data))
        am = loan_map(assets)
        defaulted = am[('L1', 1)]
        self.assertEqual(defaulted['contract_principal'], 0)
        self.assertEqual(defaulted['defaulted_principal'], 100)
        self.assertEqual(defaulted['recovery_recognized'], 100)
        self.assertEqual(defaulted['pending_close'], 100)
        self.assertEqual(am[('L1', 3)]['recovery_received'], 100)
        self.assertEqual(am[('L1', 3)]['pending_open'], 100)
        performing = [am[('L2', p)]['contract_principal'] for p in (1, 2)]
        self.assertEqual(performing, [50, 50])
        # No contractual flow from an exited loan.
        for period in (2, 3):
            row = am[('L1', period)]
            self.assertEqual((row['contract_interest'], row['contract_principal'],
                              row['prepay_principal']), (0, 0, 0))
        pm = pool_map(pools)
        self.assertEqual(pm[3]['recovery_cash_in'], 100)

    def test_liability_waterfall_without_cleanup(self):
        data = base_input()
        data['exits'] = []
        # A zero sale price can never cover the debt, so the normal waterfall
        # runs even though the pool keeps qualifying for a quote.
        data['cleanup'] = dict(data['cleanup'], price_numerator=0, price_denominator=1)
        assets, tranches, pools = build_scenario(validate_input(data))
        tm = tranche_map(tranches)
        # Period 1: cash 100 (50+50), senior takes all available principal.
        self.assertEqual(tm[('senior', 1)]['principal_paid'], 100)
        self.assertEqual(tm[('senior', 1)]['closing_principal'], 20)
        self.assertEqual(tm[('junior', 1)]['principal_paid'], 0)
        self.assertEqual(tm[('junior', 1)]['closing_principal'], 80)
        # Period 2: remaining 100 clears senior 20 then junior 80.
        self.assertEqual(tm[('senior', 2)]['closing_principal'], 0)
        self.assertEqual(tm[('junior', 2)]['principal_paid'], 80)
        self.assertEqual(tm[('junior', 2)]['closing_principal'], 0)
        pm = pool_map(pools)
        self.assertEqual(pm[1]['sponsor_cash'], 0)

    def test_linked_cleanup_exercised(self):
        assets, tranches, pools = build_scenario(validate_input(base_input()))
        pm = pool_map(pools)
        self.assertEqual(pm[1]['contract_cash_in'], 50)
        self.assertEqual(pm[1]['quote_offered'], 1)
        self.assertEqual(pm[1]['quote_price'], 150)
        self.assertEqual(pm[1]['cleanup_exercised'], 1)
        self.assertEqual(pm[1]['principal_paid'], 200)
        self.assertEqual(pm[1]['sale_proceeds'], 150)
        am = loan_map(assets)
        self.assertEqual(am[('L1', 1)]['canceled_recovery'], 100)
        self.assertEqual(am[('L1', 1)]['sale_proceeds'], 100)
        self.assertEqual(am[('L2', 1)]['sold_principal'], 50)
        self.assertEqual(am[('L2', 1)]['sale_proceeds'], 50)
        # The period-3 recovery is cancelled, never received.
        self.assertEqual(am[('L1', 3)]['recovery_received'], 0)
        for period in (2, 3):
            self.assertEqual(pm[period]['cleanup_exercised'], 0)
            self.assertEqual(pm[period]['quote_offered'], 0)
        tm = tranche_map(tranches)
        self.assertEqual(tm[('senior', 1)]['closing_principal'], 0)
        self.assertEqual(tm[('junior', 1)]['closing_principal'], 0)

    def test_linked_cleanup_blocked_by_interest_keeps_assets(self):
        data = base_input()
        data['junior'] = {'principal': 80, 'rate_numerator': 1,
                          'rate_denominator': 10}
        assets, tranches, pools = build_scenario(validate_input(data))
        pm = pool_map(pools)
        self.assertEqual(pm[1]['quote_offered'], 1)
        self.assertEqual(pm[1]['cleanup_exercised'], 0)
        self.assertEqual(pm[1]['coverage_gap'], 8)
        self.assertEqual(pm[1]['sale_proceeds'], 0)
        self.assertEqual(pm[1]['interest_paid'], 8)
        self.assertEqual(pm[1]['principal_paid'], 42)
        # Assets stay in place and the recovery still arrives in period 3.
        am = loan_map(assets)
        self.assertEqual(am[('L1', 1)]['sold_principal'], 0)
        self.assertEqual(am[('L1', 1)]['canceled_recovery'], 0)
        self.assertEqual(am[('L1', 3)]['recovery_received'], 100)
        self.assertEqual(pm[3]['recovery_cash_in'], 100)
        tm = tranche_map(tranches)
        self.assertEqual(tm[('junior', 1)]['accrued_interest'], 8)
        self.assertEqual(tm[('junior', 1)]['interest_paid'], 8)
        self.assertEqual(tm[('senior', 1)]['closing_principal'], 78)

    def test_prepay_replaces_scheduled_principal(self):
        data = base_input()
        data['exits'] = [{'loan_id': 'L1', 'kind': 'prepay', 'period': 1}]
        data['loans'][0]['contract'] = {'principal': 100, 'periods': 2,
                                        'rate_numerator': 1, 'rate_denominator': 10}
        assets, _, _ = build_scenario(validate_input(data))
        row = loan_map(assets)[('L1', 1)]
        self.assertEqual(row['contract_interest'], 10)
        self.assertEqual(row['prepay_principal'], 100)
        self.assertEqual(row['contract_principal'], 0)
        self.assertEqual(row['closing_principal'], 0)
        later = loan_map(assets)[('L1', 2)]
        self.assertEqual((later['contract_interest'], later['contract_principal']), (0, 0))

    def test_recovery_arriving_in_quote_period_is_not_double_counted(self):
        # L1 defaults in period 1 with lag 1: its 100 recovery arrives in
        # period 2, the first period in which a quote is eligible.  The cash
        # already received must not be part of the sale base again.
        data = base_input({'horizon': 4})
        data['loans'] = [
            {'loan_id': 'L1', 'contract': {'principal': 100, 'periods': 2,
                                           'rate_numerator': 0, 'rate_denominator': 1}},
            {'loan_id': 'L2', 'contract': {'principal': 100, 'periods': 3,
                                           'rate_numerator': 0, 'rate_denominator': 1}},
        ]
        data['exits'] = [{'loan_id': 'L1', 'kind': 'default', 'period': 1,
                          'recovery_numerator': 1, 'recovery_denominator': 1,
                          'recovery_lag': 1}]
        # Junior interest makes every quote unaffordable, so nothing is sold
        # and the quote composition stays observable.
        data['junior'] = {'principal': 80, 'rate_numerator': 1,
                          'rate_denominator': 10}
        assets, _, pools = build_scenario(validate_input(data))
        pm = pool_map(pools)
        # Period 1: 67 normal principal remains, 67/200 > 1/4: no quote yet.
        self.assertEqual(pm[1]['quote_offered'], 0)
        self.assertEqual(pm[1]['quote_ineligible_reason'], 'above_threshold')
        # Period 2: the 100 recovery lands; L2's 34 normal principal is the
        # only saleable base, so the quote is 34, never 134.
        self.assertEqual(pm[2]['recovery_cash_in'], 100)
        self.assertEqual(pm[2]['quote_offered'], 1)
        self.assertEqual(pm[2]['quote_price'], 34)
        self.assertEqual(pm[2]['cleanup_exercised'], 0)
        am = loan_map(assets)
        self.assertEqual(am[('L1', 2)]['recovery_received'], 100)
        self.assertEqual(am[('L1', 2)]['pending_open'], 100)
        self.assertEqual(am[('L1', 2)]['pending_close'], 0)
        self.assertEqual(am[('L1', 2)]['sale_proceeds'], 0)
        self.assertEqual(am[('L1', 2)]['canceled_recovery'], 0)
        self.assertEqual(am[('L2', 2)]['sale_proceeds'], 0)

    def test_half_up_tie_on_recovery_and_interest(self):
        self.assertEqual(half_up(Fraction(1, 2)), 1)
        data = base_input()
        data['exits'] = [{'loan_id': 'L1', 'kind': 'default', 'period': 1,
                          'recovery_numerator': 1, 'recovery_denominator': 2,
                          'recovery_lag': 1}]
        assets, _, _ = build_scenario(validate_input(data))
        row = loan_map(assets)[('L1', 1)]
        self.assertEqual(row['recovery_recognized'], 50)
        self.assertEqual(row['asset_loss'], 50)

    def test_reserve_covers_interest_then_replenishes_and_releases(self):
        # One loan pays in period 2 only, so period 1 has interest due but no
        # cash: the reserve must fill the senior interest.  It is rebuilt in
        # period 2 and released in the final period.
        data = base_input()
        data['reserve'] = 20
        data['senior'] = {'principal': 200, 'rate_numerator': 1, 'rate_denominator': 10}
        data['junior'] = {'principal': 0, 'rate_numerator': 0, 'rate_denominator': 1}
        data['loans'] = [
            {'loan_id': 'L1', 'contract': {'principal': 100, 'periods': 3,
                                           'rate_numerator': 0, 'rate_denominator': 1}},
            {'loan_id': 'L2', 'contract': {'principal': 100, 'periods': 3,
                                           'rate_numerator': 0, 'rate_denominator': 1}},
        ]
        # L2 prepays at period 2: period 1 has only L2's small scheduled
        # payment, which cannot cover the 20 interest, so reserve fills it.
        data['exits'] = [
            {'loan_id': 'L1', 'kind': 'default', 'period': 1,
             'recovery_numerator': 0, 'recovery_denominator': 1, 'recovery_lag': 2},
            {'loan_id': 'L2', 'kind': 'prepay', 'period': 2},
        ]
        data['cleanup'] = dict(data['cleanup'], price_numerator=0, price_denominator=1)
        _, tranches, pools = build_scenario(validate_input(data))
        pm = pool_map(pools)
        # Period 1: cash 33 (L2 scheduled), senior interest 20 is covered by
        # cash, reserve untouched; period 2 L2 prepays 100 and rebuilds reserve.
        self.assertEqual(pm[1]['contract_cash_in'], 33)
        self.assertEqual(pm[1]['reserve_used'], 0)
        self.assertEqual(pm[1]['closing_reserve'], 20)
        self.assertEqual(pm[2]['reserve_replenished'], 0)
        self.assertEqual(pm[2]['closing_reserve'], 20)
        # Final period releases the reserve into cash and never replenishes.
        self.assertEqual(pm[3]['reserve_released'], 20)
        self.assertEqual(pm[3]['reserve_replenished'], 0)
        self.assertEqual(pm[3]['closing_reserve'], 0)

        # A separately constructed case forces reserve usage: both loans
        # default at opening, so period 1 has zero cash and the reserve pays
        # the whole senior interest.
        data['exits'] = [
            {'loan_id': 'L1', 'kind': 'default', 'period': 1,
             'recovery_numerator': 0, 'recovery_denominator': 1, 'recovery_lag': 2},
            {'loan_id': 'L2', 'kind': 'default', 'period': 1,
             'recovery_numerator': 0, 'recovery_denominator': 1, 'recovery_lag': 2},
        ]
        _, _, pools = build_scenario(validate_input(data))
        pm = pool_map(pools)
        self.assertEqual(pm[1]['contract_cash_in'], 0)
        self.assertEqual(pm[1]['reserve_used'], 20)
        self.assertEqual(pm[1]['interest_paid'], 20)
        self.assertEqual(pm[1]['closing_reserve'], 0)

    def test_discounted_cleanup_quote_is_not_exercised_and_scenario_runs_to_horizon(self):
        data = base_input()
        data['cleanup'] = dict(data['cleanup'], price_numerator=9, price_denominator=10)
        _, _, pools = build_scenario(validate_input(data))
        pm = pool_map(pools)
        # 50 cash + 135 discounted quote < 200 debt: hold, run to horizon.
        self.assertEqual(pm[1]['quote_offered'], 1)
        self.assertEqual(pm[1]['cleanup_exercised'], 0)
        self.assertEqual(pm[1]['quote_price'], 135)
        self.assertEqual(pm[3]['recovery_cash_in'], 100)

    def test_underfunded_scenario_still_discloses_final_shortfall(self):
        # A default whose recovery arrives after the horizon, plus a zero sale
        # price: the scenario stays legal and the unpaid principal is disclosed
        # at the horizon rather than rejected.
        data = base_input({'horizon': 5})
        data['exits'] = [{'loan_id': 'L1', 'kind': 'default', 'period': 1,
                          'recovery_numerator': 0, 'recovery_denominator': 1,
                          'recovery_lag': 4}]
        data['cleanup'] = dict(data['cleanup'], price_numerator=0, price_denominator=1)
        assets, tranches, pools = build_scenario(validate_input(data))
        tm = tranche_map(tranches)
        total_principal = sum(tm[(t, 5)]['closing_principal'] for t in ('senior', 'junior'))
        self.assertEqual(total_principal, 100)
        pm = pool_map(pools)
        self.assertEqual(pm[5]['cleanup_exercised'], 0)
        # The zero recovery means nothing is ever pending or received on L1,
        # and the 100 asset loss surfaces as unpaid tranche principal rather
        # than an automatic write-down.
        am = loan_map(assets)
        self.assertEqual(am[('L1', 1)]['asset_loss'], 100)
        self.assertEqual(am[('L1', 5)]['pending_close'], 0)
        self.assertEqual(pm[5]['recovery_cash_in'], 0)

    def test_threshold_uses_exact_rational(self):
        # Remaining normal principal 50 vs initial 200, threshold 1/4: eligible
        # exactly at equality; 51/200 would not be.
        data = base_input()
        data['cleanup'] = dict(data['cleanup'], threshold_numerator=25,
                               threshold_denominator=100)
        _, _, pools = build_scenario(validate_input(data))
        self.assertEqual(pool_map(pools)[1]['quote_offered'], 1)

    def test_coverage_gap_zero_exercises_but_gap_one_holds(self):
        data = {
            'pool_id': 'B', 'horizon': 2,
            'loans': [{'loan_id': 'L', 'contract': {'principal': 100, 'periods': 2,
                                                    'rate_numerator': 0,
                                                    'rate_denominator': 1}}],
            'exits': [],
            'senior': {'principal': 100, 'rate_numerator': 0, 'rate_denominator': 1},
            'junior': {'principal': 0, 'rate_numerator': 0, 'rate_denominator': 1},
            'reserve': 0,
            'cleanup': {'threshold_numerator': 1, 'threshold_denominator': 1,
                        'price_numerator': 100, 'price_denominator': 100},
        }
        # Period 1: cash 50 + quote 50 = 100 debt exactly: gap 0 -> exercise.
        first = pool_map(build_scenario(validate_input(data))[2])[1]
        self.assertEqual(first['quote_price'], 50)
        self.assertEqual(first['coverage_gap'], 0)
        self.assertEqual(first['cleanup_exercised'], 1)
        # Same quote discounted by 2: funds 99, gap 1 -> hold, no sale.
        data['cleanup']['price_numerator'] = 98
        held = pool_map(build_scenario(validate_input(data))[2])[1]
        self.assertEqual(held['quote_price'], 49)
        self.assertEqual(held['coverage_gap'], 1)
        self.assertEqual(held['cleanup_exercised'], 0)
        self.assertEqual(held['sale_proceeds'], 0)


class SqliteDatasetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, fixture, name):
        out = self.dir / name
        result = call('tranche-build', '--source', fixture, '--output', out)
        self.assertEqual(result.returncode, 0, result.stderr)
        return out, json.loads(result.stdout)

    def test_fixture_datasets_and_independent_sql_audit(self):
        cleanup_db, summary = self.build(FIXTURES / 'scenario-cleanup.json', 'cleanup.db')
        self.assertEqual(summary['cleanup_period'], 1)
        hold_db, summary = self.build(FIXTURES / 'scenario-hold.json', 'hold.db')
        self.assertIsNone(summary['cleanup_period'])
        for db_path, exercised in ((cleanup_db, 1), (hold_db, 0)):
            db = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
            try:
                db.execute('PRAGMA foreign_keys=ON')
                self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                horizon = db.execute('SELECT horizon FROM pool_config').fetchone()[0]
                loans = db.execute('SELECT count(*) FROM loan_contracts').fetchone()[0]
                self.assertEqual(
                    db.execute('SELECT count(*) FROM asset_facts').fetchone()[0],
                    loans * horizon)
                self.assertEqual(
                    db.execute('SELECT count(*) FROM tranche_facts').fetchone()[0],
                    2 * horizon)
                self.assertEqual(
                    db.execute('SELECT count(*) FROM pool_facts').fetchone()[0], horizon)
                # Period 1 and 3 asset rows join back to source contracts.
                rows = db.execute('''
                    SELECT c.loan_id, f.period, f.contract_principal,
                           f.defaulted_principal, f.recovery_received,
                           f.sold_principal, f.canceled_recovery
                    FROM asset_facts f JOIN loan_contracts c ON c.loan_id=f.loan_id
                    WHERE f.period IN (1,3) ORDER BY c.ordinal, f.period''').fetchall()
                self.assertEqual(len(rows), 4)
                bad = db.execute(
                    'SELECT count(*) FROM v_pool_cash_conservation WHERE balanced=0'
                ).fetchone()[0]
                self.assertEqual(bad, 0)
                bad = db.execute(
                    'SELECT count(*) FROM v_loan_principal_conservation WHERE balanced=0'
                ).fetchone()[0]
                self.assertEqual(bad, 0)
                bad = db.execute(
                    'SELECT count(*) FROM v_recovery_conservation WHERE balanced=0'
                ).fetchone()[0]
                self.assertEqual(bad, 0)
                bad = db.execute('''SELECT count(*) FROM v_tranche_rollforward
                                    WHERE principal_ok=0 OR interest_ok=0''').fetchone()[0]
                self.assertEqual(bad, 0)
                self.assertEqual(
                    db.execute('SELECT cleanup_exercised FROM pool_facts WHERE period=1'
                               ).fetchone()[0], exercised)
                sha = db.execute('SELECT source_sha256 FROM dataset_meta').fetchone()[0]
                self.assertEqual(len(sha), 64)
            finally:
                db.close()

    def test_replay_ordered_queries_are_stable_under_input_reordering(self):
        canonical = json.loads((FIXTURES / 'scenario-cleanup.json').read_text())
        shuffled = copy.deepcopy(canonical)
        shuffled['loans'] = list(reversed(shuffled['loans']))
        shuffled['exits'] = list(reversed(shuffled['exits']))
        reordered = self.dir / 'reordered.json'
        reordered.write_text(json.dumps(shuffled))
        out_a = self.dir / 'a.db'
        out_b = self.dir / 'b.db'
        self.assertEqual(call('tranche-build', '--source', reordered,
                              '--output', out_a).returncode, 0)
        self.assertEqual(call('tranche-build', '--source', FIXTURES / 'scenario-cleanup.json',
                              '--output', out_b).returncode, 0)

        def snapshot(path):
            db = sqlite3.connect(str(path))
            try:
                return {
                    'asset': db.execute('SELECT * FROM asset_facts ORDER BY loan_id,period'
                                        ).fetchall(),
                    'tranche': db.execute('SELECT * FROM tranche_facts ORDER BY tranche,period'
                                          ).fetchall(),
                    'pool': db.execute('SELECT * FROM pool_facts ORDER BY period').fetchall(),
                }
            finally:
                db.close()
        self.assertEqual(snapshot(out_a), snapshot(out_b))

    def test_failure_preserves_existing_database(self):
        out, _ = self.build(FIXTURES / 'scenario-cleanup.json', 'keep.db')
        original = out.read_bytes()
        bad = self.dir / 'bad.json'
        for body in ['{}', '{"a":1,"a":2}', 'NaN',
                     json.dumps(base_input({'horizon': 99}))]:
            bad.write_text(body)
            result = call('tranche-build', '--source', bad, '--output', out)
            self.assertEqual(result.returncode, 2, body)
            self.assertTrue(result.stderr.strip())
            self.assertEqual(out.read_bytes(), original)
        self.assertFalse(list(self.dir.glob('.tranche-build-*')))

    def test_output_safety(self):
        src = self.dir / 'in.json'
        src.write_text((FIXTURES / 'scenario-cleanup.json').read_text())
        before = src.read_bytes()
        self.assertEqual(call('tranche-build', '--source', src, '--output', src).returncode, 2)
        self.assertEqual(src.read_bytes(), before)
        link = self.dir / 'link.db'
        link.symlink_to(self.dir / 'target.db')
        self.assertEqual(call('tranche-build', '--source', src, '--output', link).returncode, 2)
        self.assertEqual(src.read_bytes(), before)

    def test_source_is_not_modified(self):
        src = self.dir / 'src.json'
        src.write_text((FIXTURES / 'scenario-cleanup.json').read_text())
        before = src.read_bytes()
        out = self.dir / 'o.db'
        self.assertEqual(call('tranche-build', '--source', src, '--output', out).returncode, 0)
        self.assertEqual(src.read_bytes(), before)


class CommandRegressionTests(unittest.TestCase):
    def test_static_and_ledger_commands_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            static_out = td / 'plan.json'
            result = call('--source', ROOT / 'fixtures' / 'input.json',
                          '--output', static_out)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(static_out.read_text())
            self.assertEqual(report['schema_version'], 1)
            self.assertEqual([r['principal'] for r in report['result']['schedule']],
                             [333, 333, 334])
            db = td / 'ledger.db'
            result = call('ledger-init', '--db', db,
                          '--source', ROOT / 'fixtures' / 'ledger' / 'contract.json')
            self.assertEqual(result.returncode, 0, result.stderr)
            result = call('ledger-import', '--db', db,
                          '--source', ROOT / 'fixtures' / 'ledger' / 'batch-01-receipt.json')
            self.assertEqual(result.returncode, 0, result.stderr)
            ledger_out = td / 'report.json'
            result = call('ledger-report', '--db', db, '--known-seq', 1,
                          '--through-period', 3, '--output', ledger_out)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(ledger_out.read_text())['schema_version'], 2)


if __name__ == '__main__':
    unittest.main()
