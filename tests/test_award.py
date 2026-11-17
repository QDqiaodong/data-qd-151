"""Contract tests for the ``contract-award`` procurement lot-award dataset.

The small-instance tests compare the production min-cost-flow optimiser with a
completely independent brute-force enumeration oracle defined in this file.
"""
import copy
import hashlib
import json
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'fixtures' / 'award'
SRC = ROOT / 'src'
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from loan_schedule import award


def call(*args):
    env = {'PYTHONPATH': str(SRC)}
    return subprocess.run([sys.executable, '-m', 'loan_schedule.cli', *map(str, args)],
                          capture_output=True, text=True, env=env)


def sample():
    return json.loads((FIXTURES / 'procurement.json').read_text(encoding='utf-8'))


def qualified_edges(spec):
    certificates = {row['supplier_id']: set(row['certificates'])
                    for row in spec['suppliers']}
    return [bid for bid in spec['bids']
            if set(next(lot for lot in spec['lots'] if lot['lot_id'] == bid['lot_id'])
                   ['required_certificates']) <= certificates[bid['supplier_id']]]


def oracle(spec):
    """Exhaustive, production-independent search over every feasible award set."""
    lots = [lot['lot_id'] for lot in spec['lots']]
    capacity = {row['supplier_id']: row['capacity'] for row in spec['suppliers']}
    edges = qualified_edges(spec)
    candidates = {lot: sorted((bid for bid in edges if bid['lot_id'] == lot),
                              key=lambda bid: (bid['supplier_id'], bid['bid_id']))
                  for lot in lots}
    best = None
    used = {supplier: 0 for supplier in capacity}

    def choose(index, chosen):
        nonlocal best
        if index == len(lots):
            key = (-len(chosen),
                   sum(bid['amount'] for _, bid in chosen),
                   tuple(sorted((lot, bid['supplier_id'], bid['bid_id'])
                                for lot, bid in chosen)))
            if best is None or key < best:
                best = key
            return
        lot = lots[index]
        choose(index + 1, chosen)  # leave this lot unawarded
        for bid in candidates[lot]:
            supplier = bid['supplier_id']
            if used[supplier] >= capacity[supplier]:
                continue
            used[supplier] += 1
            chosen.append((lot, bid))
            choose(index + 1, chosen)
            chosen.pop()
            used[supplier] -= 1

    choose(0, [])
    return best


def reported(spec, report):
    chosen = []
    for row in report['lots']:
        if not row['awarded']:
            continue
        bid = next(item for item in row['candidates'] if item['selected'])
        chosen.append((row['lot_id'], bid['supplier_id'], bid['bid_id']))
    return (-len(chosen),
            sum(row['awarded_amount'] for row in report['lots'] if row['awarded']),
            tuple(sorted(chosen)))


def run_pipeline(data):
    spec = award.validate_input(data)
    result = award.optimize(spec)
    report = award.build_report(spec, result, '0' * 64)
    award.validate_result(spec, report)
    return spec, result, report


def tiny(lots, suppliers, bids):
    return {'procurement_id': 'P', 'lots': lots, 'suppliers': suppliers, 'bids': bids}


def lot(lot_id, certs=()):
    return {'lot_id': lot_id, 'required_certificates': list(certs)}


def supplier(supplier_id, capacity, certs=()):
    return {'supplier_id': supplier_id, 'capacity': capacity, 'certificates': list(certs)}


def bid(bid_id, lot_id, supplier_id, amount):
    return {'bid_id': bid_id, 'lot_id': lot_id, 'supplier_id': supplier_id, 'amount': amount}


class OracleTests(unittest.TestCase):
    def compare(self, data):
        spec, result, report = run_pipeline(data)
        self.assertEqual(oracle(spec), reported(spec, report))
        return report

    def test_fixture_matches_oracle(self):
        report = self.compare(sample())
        # Five lots have a qualified bid but total supplier capacity is four;
        # the optimum drops the expensive LOT-A rather than pay 500 for it.
        self.assertEqual(report['objective']['awarded_lot_count'], 4)
        self.assertEqual(report['objective']['awarded_total_amount'], 44)
        reasons = {row['lot_id']: row['unawarded_reason'] for row in report['lots']}
        self.assertEqual(reasons['LOT-A'], 'capacity_competition')
        self.assertEqual(reasons['LOT-D'], 'no_bid')
        self.assertEqual(reasons['LOT-E'], 'no_qualified_bid')

    def test_qualification_blocks_edge(self):
        data = tiny([lot('L1', ['X'])],
                    [supplier('S1', 1), supplier('S2', 1, ['X'])],
                    [bid('B1', 'L1', 'S1', 1), bid('B2', 'L1', 'S2', 9)])
        report = self.compare(data)
        row = report['lots'][0]
        self.assertEqual(row['awarded_bid_id'], 'B2')
        self.assertEqual(row['candidates'][0]['missing_certificates'], ['X'])
        self.assertFalse(row['candidates'][0]['certificate_qualified'])

    def test_award_count_beats_low_amount(self):
        # One very cheap lot must not outrank awarding two more expensive lots.
        data = tiny([lot('L1'), lot('L2'), lot('L3')],
                    [supplier('S1', 3)],
                    [bid('B1', 'L1', 'S1', 1), bid('B2', 'L2', 'S1', 100),
                     bid('B3', 'L3', 'S1', 100)])
        report = self.compare(data)
        self.assertEqual(report['objective']['awarded_lot_count'], 3)
        self.assertEqual(report['objective']['awarded_total_amount'], 201)

    def test_capacity_conflict_forces_global_choice(self):
        # A cheap supplier with capacity 1 cannot serve both lots; the optimum
        # must give it one lot and the other lot to the expensive supplier.
        data = tiny([lot('L1'), lot('L2')],
                    [supplier('S1', 1), supplier('S2', 2)],
                    [bid('B1', 'L1', 'S1', 1), bid('B2', 'L2', 'S1', 1),
                     bid('B3', 'L1', 'S2', 50), bid('B4', 'L2', 'S2', 50)])
        report = self.compare(data)
        self.assertEqual(report['objective']['awarded_lot_count'], 2)
        self.assertEqual(report['objective']['awarded_total_amount'], 51)

    def test_capacity_zero_and_missing_bids(self):
        data = tiny([lot('L1'), lot('L2'), lot('L3')],
                    [supplier('S1', 0)],
                    [bid('B1', 'L1', 'S1', 1)])
        report = self.compare(data)
        reasons = {row['lot_id']: row['unawarded_reason'] for row in report['lots']}
        self.assertEqual(reasons, {'L1': 'capacity_competition',
                                   'L2': 'no_bid', 'L3': 'no_bid'})

    def test_lot_uniqueness(self):
        data = tiny([lot('L1')], [supplier('S1', 1), supplier('S2', 1)],
                    [bid('B1', 'L1', 'S1', 5), bid('B2', 'L1', 'S2', 6)])
        report = self.compare(data)
        selected = [item for row in report['lots']
                    for item in row['candidates'] if item['selected']]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]['bid_id'], 'B1')

    def test_equal_cost_tie_break_is_lexicographic(self):
        data = tiny([lot('L1'), lot('L2')],
                    [supplier('S1', 2), supplier('S2', 2)],
                    [bid('B1', 'L1', 'S1', 10), bid('B2', 'L1', 'S2', 10),
                     bid('B3', 'L2', 'S1', 10), bid('B4', 'L2', 'S2', 10)])
        report = self.compare(data)
        chosen = [(row['lot_id'], row['awarded_supplier_id'])
                  for row in report['lots'] if row['awarded']]
        self.assertEqual(chosen, [('L1', 'S1'), ('L2', 'S1')])

    def test_random_small_instances_against_oracle(self):
        rng = random.Random(2026)
        for _ in range(120):
            lot_count = rng.randint(1, 4)
            supplier_count = rng.randint(1, 3)
            certificate_pool = ['C1', 'C2', 'C3']
            lots = [lot('L%d' % (i + 1),
                        rng.sample(certificate_pool, rng.randint(0, 2)))
                    for i in range(lot_count)]
            suppliers = [supplier('S%d' % (i + 1), rng.randint(0, lot_count),
                                  rng.sample(certificate_pool, rng.randint(0, 3)))
                         for i in range(supplier_count)]
            bids = []
            serial = 0
            for lot_row in lots:
                for supplier_row in suppliers:
                    if rng.random() < 0.65:
                        serial += 1
                        bids.append(bid('B%02d' % serial, lot_row['lot_id'],
                                        supplier_row['supplier_id'],
                                        rng.choice([1, 2, 3, 5, 8, 13])))
            data = tiny(lots, suppliers, bids)
            with self.subTest(index=_):
                self.compare(data)


class OutputValidationTests(unittest.TestCase):
    def test_published_rows_are_consistent(self):
        spec, result, report = run_pipeline(sample())
        input_bids = {row['bid_id']: row for row in spec['bids']}
        input_capacity = {row['supplier_id']: row['capacity'] for row in spec['suppliers']}
        input_certs = {row['supplier_id']: set(row['certificates']) for row in spec['suppliers']}
        required = {row['lot_id']: set(row['required_certificates']) for row in spec['lots']}
        seen_lots = set()
        usage = {}
        for row in report['lots']:
            if not row['awarded']:
                self.assertIsNotNone(row['unawarded_reason'])
                continue
            self.assertNotIn(row['lot_id'], seen_lots)
            seen_lots.add(row['lot_id'])
            source = input_bids[row['awarded_bid_id']]
            self.assertEqual(source['lot_id'], row['lot_id'])
            self.assertEqual(source['supplier_id'], row['awarded_supplier_id'])
            self.assertEqual(source['amount'], row['awarded_amount'])
            self.assertTrue(required[row['lot_id']] <= input_certs[row['awarded_supplier_id']])
            usage[row['awarded_supplier_id']] = usage.get(row['awarded_supplier_id'], 0) + 1
        for supplier_id, count in usage.items():
            self.assertLessEqual(count, input_capacity[supplier_id])

    def test_validate_result_detects_tampering(self):
        spec, _, report = run_pipeline(sample())
        broken = copy.deepcopy(report)
        awarded = next(row for row in broken['lots'] if row['awarded'])
        awarded['candidates'][0]['selected'] = True  # two selected bids on one lot
        with self.assertRaises(ValueError):
            award.validate_result(spec, broken)

        broken = copy.deepcopy(report)
        for row in broken['suppliers']:
            if row['awarded_lot_count']:
                row['remaining_capacity'] += 1
                break
        with self.assertRaises(ValueError):
            award.validate_result(spec, broken)


class RejectionTests(unittest.TestCase):
    def assert_invalid(self, mutate, needle=None):
        data = sample()
        mutate(data)
        with self.assertRaises((ValueError, TypeError)) as caught:
            award.validate_input(data)
        if needle is not None:
            self.assertIn(needle, str(caught.exception))

    def test_unknown_and_missing_fields(self):
        self.assert_invalid(lambda d: d.update(extra=1), 'unknown field')
        self.assert_invalid(lambda d: d['lots'][0].update(extra=1), 'unknown field')
        self.assert_invalid(lambda d: d['suppliers'][0].update(extra=1), 'unknown field')
        self.assert_invalid(lambda d: d['bids'][0].update(extra=1), 'unknown field')
        self.assert_invalid(lambda d: d['lots'][0].pop('required_certificates'), 'missing field')
        self.assert_invalid(lambda d: d['suppliers'][0].pop('capacity'), 'missing field')
        self.assert_invalid(lambda d: d['bids'][0].pop('amount'), 'missing field')

    def test_boolean_impersonating_integer(self):
        self.assert_invalid(lambda d: d['suppliers'][0].update(capacity=True), 'integer')
        self.assert_invalid(lambda d: d['bids'][0].update(amount=False), 'integer')

    def test_ranges(self):
        self.assert_invalid(lambda d: d['suppliers'][0].update(capacity=len(d['lots']) + 1))
        self.assert_invalid(lambda d: d['suppliers'][0].update(capacity=-1))
        self.assert_invalid(lambda d: d['bids'][0].update(amount=0))
        self.assert_invalid(lambda d: d['bids'][0].update(amount=10 ** 12 + 1))
        self.assert_invalid(lambda d: d.update(lots=[]))
        self.assert_invalid(lambda d: d.update(suppliers=[]))
        self.assert_invalid(lambda d: d.update(bids=[{}] * 5001))

    def test_duplicates_and_dangling_refs(self):
        self.assert_invalid(lambda d: d['lots'].append(copy.deepcopy(d['lots'][0])), 'duplicate lot_id')
        self.assert_invalid(lambda d: d['suppliers'].append(copy.deepcopy(d['suppliers'][0])), 'duplicate supplier_id')
        self.assert_invalid(lambda d: d['bids'].append(copy.deepcopy(d['bids'][0])), 'duplicate bid_id')
        self.assert_invalid(lambda d: d['bids'].append(
            bid('B-99', 'LOT-B', 'SUP-1', 3)), 'duplicate bid for supplier')
        self.assert_invalid(lambda d: d['bids'].append(
            bid('B-99', 'GHOST', 'SUP-1', 3)), 'unknown lot_id')
        self.assert_invalid(lambda d: d['bids'].append(
            bid('B-99', 'LOT-B', 'GHOST', 3)), 'unknown supplier_id')
        self.assert_invalid(lambda d: d['suppliers'][0]['certificates'].append('ISO9001'),
                            'duplicate certificate')
        self.assert_invalid(lambda d: d['lots'][0]['required_certificates'].append('SECRET'),
                            'duplicate certificate')

    def test_empty_bids_is_valid(self):
        data = sample()
        data['bids'] = []
        report = run_pipeline(data)[2]
        self.assertEqual(report['objective']['awarded_lot_count'], 0)
        self.assertTrue(all(row['unawarded_reason'] == 'no_bid' for row in report['lots']))

    def test_identifier_rules(self):
        self.assert_invalid(lambda d: d.update(procurement_id=' x'), 'identifier')
        self.assert_invalid(lambda d: d['lots'][0].update(lot_id=''), 'identifier')
        self.assert_invalid(lambda d: d['suppliers'][0].update(supplier_id='a\x01b'), 'identifier')


class CommandTests(unittest.TestCase):
    def test_cli_replay_and_digest(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / 'awards.json'
            source = FIXTURES / 'procurement.json'
            first = call('contract-award', '--source', source, '--output', out)
            self.assertEqual(first.returncode, 0, first.stderr)
            payload = out.read_bytes()
            self.assertEqual(json.loads(payload)['source_sha256'],
                             hashlib.sha256(source.read_bytes()).hexdigest())
            second = call('contract-award', '--source', source, '--output', out)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(payload, out.read_bytes())

    def test_shuffled_input_is_stable(self):
        with tempfile.TemporaryDirectory() as td:
            data = sample()
            rng = random.Random(7)
            shuffled = copy.deepcopy(data)
            for key in ('lots', 'suppliers', 'bids'):
                rng.shuffle(shuffled[key])
            ordered = Path(td) / 'ordered.json'
            mixed = Path(td) / 'mixed.json'
            ordered.write_text(json.dumps(data, ensure_ascii=False))
            mixed.write_text(json.dumps(shuffled, ensure_ascii=False))
            out_a = Path(td) / 'a.json'
            out_b = Path(td) / 'b.json'
            self.assertEqual(call('contract-award', '--source', ordered, '--output', out_a).returncode, 0)
            self.assertEqual(call('contract-award', '--source', mixed, '--output', out_b).returncode, 0)
            first, second = json.loads(out_a.read_text()), json.loads(out_b.read_text())
            first.pop('source_sha256')
            second.pop('source_sha256')
            self.assertEqual(first, second)

    def test_failure_preserves_previous_output(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / 'awards.json'
            out.write_text('prior')
            source = Path(td) / 'broken.json'
            for body in ['{}', '{"procurement_id":"P","procurement_id":"Q"}', 'NaN',
                         '[1,2,3]']:
                source.write_text(body)
                result = call('contract-award', '--source', source, '--output', out)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, '')
                self.assertTrue(result.stderr)
                self.assertEqual(out.read_text(), 'prior')

    def test_output_safety(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / 'in.json'
            source.write_text(json.dumps(sample()))
            before = source.read_bytes()
            self.assertEqual(call('contract-award', '--source', source,
                                  '--output', source).returncode, 2)
            self.assertEqual(source.read_bytes(), before)
            link = Path(td) / 'link.json'
            link.symlink_to(source)
            self.assertEqual(call('contract-award', '--source', source,
                                  '--output', link).returncode, 2)
            self.assertEqual(source.read_bytes(), before)

    def test_console_entry_point_ignores_leading_option(self):
        source = FIXTURES / 'procurement.json'
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / 'awards.json'
            env = {'PYTHONPATH': str(SRC)}
            result = subprocess.run(
                [sys.executable, '-c',
                 'from loan_schedule.cli import contract_award_main; '
                 'raise SystemExit(contract_award_main())',
                 '--source', str(source), '--output', str(out)],
                capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(out.read_text())['procurement_id'], 'PROC-2026-001')

    def test_nested_duplicate_key_and_nonfinite_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / 'awards.json'
            out.write_text('prior')
            source = Path(td) / 'in.json'
            body = json.dumps(sample())
            for payload in (body.replace('"amount"', '"amount":1,"amount"', 1),
                            body.replace(str(sample()['bids'][0]['amount']), 'NaN', 1),
                            body.replace(str(sample()['bids'][0]['amount']), '1.5', 1)):
                source.write_text(payload)
                result = call('contract-award', '--source', source, '--output', out)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(out.read_text(), 'prior')

    def test_oversized_source_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / 'awards.json'
            out.write_text('prior')
            source = Path(td) / 'big.json'
            source.write_bytes(b'{"x":1}' + b' ' * 8_000_001)
            result = call('contract-award', '--source', source, '--output', out)
            self.assertEqual(result.returncode, 2)
            self.assertIn('8 MB', result.stderr)
            self.assertEqual(out.read_text(), 'prior')

    def test_entry_point_declared_and_callable(self):
        self.assertIn('contract-award = "loan_schedule.cli:contract_award_main"',
                      (ROOT / 'pyproject.toml').read_text())

    def test_declared_entry_point(self):
        text = (ROOT / 'pyproject.toml').read_text()
        self.assertIn('contract-award = "loan_schedule.cli:contract_award_main"', text)
        self.assertIn('loan-schedule = "loan_schedule.cli:main"', text)


if __name__ == '__main__':
    unittest.main()
