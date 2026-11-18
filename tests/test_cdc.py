"""Contract tests for the ``cdc-materialize`` transaction-log product.

The naive reference interpreter in this file is deliberately independent of
the production engine: it never imports or calls
``loan_schedule.cdc.materialize``, ``_replay_transaction`` or
``_check_all_foreign_keys``.  It re-parses the raw validated input itself and
re-simulates every commit with plain dictionaries and locally defined
type-aware helpers.  Randomized interleaved logs are compared against it
verdict-by-verdict and row-by-row, including the audit entry produced at each
commit point, not just the final row counts.
"""
import copy
import hashlib
import json
import os
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'fixtures' / 'cdc'
SRC = ROOT / 'src'
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from loan_schedule import cdc  # production package; oracle below must not use its engine
from loan_schedule.common import read


# --------------------------------------------------------------------------- #
# Naive reference interpreter: standalone, no production replay/constraint calls
# --------------------------------------------------------------------------- #

def _typed(value):
    """Type rank mirroring the production typed ordering (null<bool<int<str)."""
    if value is None:
        return (0, 0)
    if isinstance(value, bool):
        return (1, int(value))
    if isinstance(value, int):
        return (2, value)
    return (3, value)


def _key_tuple(row, positions):
    return tuple(row[i] for i in positions)


def _sort_key(key):
    return tuple(_typed(part) for part in key)


def reference_materialize(raw):
    """Return ``(final_rows, audit_entries)`` computed from the raw valid input.

    This is the small, deliberately simple oracle used for differential
    testing.  It assumes the frame/structure is already valid (the production
    validator is responsible for rejecting malformed input).
    """
    tables = {}
    for declaration in raw['tables']:
        width = len(declaration['columns'])
        index = {column['name']: i for i, column in enumerate(declaration['columns'])}
        tables[declaration['table']] = {
            'width': width,
            'pk': [index[name] for name in declaration['primary_key']],
            'fks': [{'fk_id': fk['fk_id'],
                     'columns': [index[name] for name in fk['columns']],
                     'ref_table': fk['ref_table']}
                    for fk in declaration['foreign_keys']]}

    state = {name: {} for name in tables}
    for item in raw['initial']:
        row = list(item['values'])
        state[item['table']][_key_tuple(row, tables[item['table']]['pk'])] = row

    def equal_rows(left, right):
        if len(left) != len(right):
            return False
        for a, b in zip(left, right):
            if type(a) is not type(b) or a != b:
                return False
        return True

    def first_violation(candidate):
        found = []
        for name, spec in tables.items():
            for key, row in candidate[name].items():
                for fk in spec['fks']:
                    values = [row[position] for position in fk['columns']]
                    if any(value is None for value in values):
                        continue
                    target = tuple(values)
                    if target not in candidate[fk['ref_table']]:
                        found.append((name, fk['fk_id'], key))
        found.sort(key=lambda triple: (triple[0], triple[1], _sort_key(triple[2])))
        return found[0] if found else None

    staged = {}
    begin_lsn = {}
    order = []
    book = {}
    for record in raw['records']:
        txid = record['txid']
        kind = record['kind']
        if kind == 'begin':
            staged[txid] = []
            begin_lsn[txid] = record['lsn']
            order.append(txid)
            book[txid] = {'txid': txid, 'begin_lsn': record['lsn'],
                          'end_lsn': None, 'status': 'open',
                          'change_lsns': [], 'error': None}
        elif kind == 'change':
            staged[txid].append(record)
            book[txid]['change_lsns'].append(record['lsn'])
        elif kind == 'abort':
            book[txid]['status'] = 'aborted'
            book[txid]['end_lsn'] = record['lsn']
            del staged[txid]
        else:  # commit: full private replay on the latest accepted snapshot
            candidate = {name: dict(rows) for name, rows in state.items()}
            error = None
            for change in staged[txid]:
                name = change['table']
                spec = tables[name]
                rel = candidate[name]
                before = change['before']
                after = change['after']
                if before is None:
                    target = _key_tuple(after, spec['pk'])
                    if target in rel:
                        error = {'code': 'primary_key_conflict',
                                 'change_lsn': change['lsn'], 'table': name,
                                 'primary_key': list(target), 'fk_id': None}
                        break
                    rel[target] = list(after)
                    continue
                before_key = _key_tuple(before, spec['pk'])
                if before_key not in rel:
                    error = {'code': 'missing_row', 'change_lsn': change['lsn'],
                             'table': name, 'primary_key': list(before_key),
                             'fk_id': None}
                    break
                if not equal_rows(rel[before_key], before):
                    error = {'code': 'before_mismatch',
                             'change_lsn': change['lsn'], 'table': name,
                             'primary_key': list(before_key), 'fk_id': None}
                    break
                del rel[before_key]
                if after is not None:
                    after_key = _key_tuple(after, spec['pk'])
                    if after_key in rel:
                        error = {'code': 'primary_key_conflict',
                                 'change_lsn': change['lsn'], 'table': name,
                                 'primary_key': list(after_key), 'fk_id': None}
                        break
                    rel[after_key] = list(after)
            if error is None:
                violation = first_violation(candidate)
                if violation is not None:
                    table_name, fk_id, child_key = violation
                    error = {'code': 'foreign_key', 'change_lsn': None,
                             'table': table_name,
                             'primary_key': list(child_key), 'fk_id': fk_id}
            entry = book[txid]
            entry['end_lsn'] = record['lsn']
            if error is None:
                state = candidate
                entry['status'] = 'accepted'
            else:
                entry['status'] = 'rejected'
                entry['error'] = error
            del staged[txid]

    final_rows = {}
    for name, spec in tables.items():
        final_rows[name] = [list(state[name][key]) for key in
                            sorted(state[name], key=_sort_key)]
    audits = [book[tx] for tx in sorted(order, key=lambda one: begin_lsn[one])]
    return final_rows, audits


# --------------------------------------------------------------------------- #
# Random valid-log generator
# --------------------------------------------------------------------------- #

TYPES = ('integer', 'string', 'boolean')


def _random_scalar(rng, column_type):
    if column_type == 'integer':
        return rng.randint(1, 40)
    if column_type == 'boolean':
        return rng.choice((True, False))
    return rng.choice(('alpha', 'beta', 'gamma', 'δ', 'E', '1'))


def build_random_case(seed):
    """Construct a frame-valid interleaved CDC log with rich FK structure."""
    rng = random.Random(seed)
    table_count = rng.randint(2, 4)
    declarations = []
    names = ['T%d' % i for i in range(table_count)]
    for i, name in enumerate(names):
        width = rng.randint(2, 5)
        pk_width = rng.choice((1, 1, 2))
        columns = []
        for j in range(width):
            if j < pk_width:
                columns.append({'name': 'k%d' % j, 'type': 'integer',
                                'nullable': False})
            else:
                columns.append({'name': 'c%d' % j,
                                'type': rng.choice(TYPES),
                                'nullable': rng.random() < 0.4})
        pk = ['k%d' % j for j in range(pk_width)]
        fks = []
        if i > 0 and rng.random() < 0.8:
            target = declarations[rng.randrange(len(declarations))]
            if len(target['primary_key']) == pk_width and all(
                    target['columns'][j]['type'] == 'integer'
                    for j in range(pk_width)):
                fk_columns = ['k%d' % j for j in range(pk_width)]
                # FK shares the local primary key here, so its columns stay
                # nullable=false; null-component exemptions are covered by the
                # fixed gallery fixture's self-referencing nullable FK.
                fks.append({'fk_id': 'fk_%s' % name,
                            'columns': fk_columns,
                            'ref_table': target['table']})
        declarations.append({'table': name, 'columns': columns,
                             'primary_key': pk, 'foreign_keys': fks})

    def index_of(declaration, column_name):
        return next(i for i, c in enumerate(declaration['columns'])
                    if c['name'] == column_name)

    # Initial rows, built table-by-table in declaration order.  Every FK
    # references an earlier table, so target keys already exist when a child
    # table is populated; this guarantees a unique-PK, FK-valid initial state
    # without post-hoc repair.
    initial = []
    relations = {name: {} for name in names}
    fk_map = {}
    for declaration in declarations:
        name = declaration['table']
        pk_positions = [index_of(declaration, column)
                        for column in declaration['primary_key']]
        fk_map[name] = [
            ([index_of(declaration, column) for column in fk['columns']],
             fk['ref_table']) for fk in declaration['foreign_keys']]
        for _ in range(rng.randint(0, 6)):
            row = [None] * len(declaration['columns'])
            for _ in range(100):
                for position, column in enumerate(declaration['columns']):
                    row[position] = _random_scalar(rng, column['type']) \
                        if column['nullable'] and rng.random() < 0.2 else \
                        _random_scalar(rng, column['type'])
                candidate_key = tuple(row[p] for p in pk_positions)
                if candidate_key not in relations[name]:
                    break
            else:  # pragma: no cover - exhausted only under pathological luck
                continue
            for positions, ref_table in fk_map[name]:
                target_keys = list(relations[ref_table])
                if not target_keys:
                    continue  # empty parent: leave random value; row gets dropped below
                chosen = rng.choice(target_keys)
                for offset, position in enumerate(positions):
                    row[position] = chosen[offset]
            key = tuple(row[p] for p in pk_positions)
            if key in relations[name]:
                continue
            # Re-verify every FK now so an unreferenced row is never published.
            dangling = any(
                tuple(row[p] for p in positions) not in relations[ref_table]
                for positions, ref_table in fk_map[name])
            if dangling:
                continue
            relations[name][key] = list(row)
            initial.append({'table': name, 'values': list(row)})

    # Transactions: interleaved begins/changes, mixed endings.
    lsn = 0
    records = []
    tx_count = rng.randint(2, 6)
    endings = []
    open_tx = None
    for tx_number in range(tx_count):
        lsn += 1
        txid = 'tx%d' % tx_number
        records.append({'lsn': lsn, 'txid': txid, 'kind': 'begin'})
        roll = rng.random()
        if roll < 0.15:
            endings.append((txid, 'empty-commit'))
        elif roll < 0.3:
            endings.append((txid, 'abort'))
        elif roll < 0.4 and open_tx is None:
            open_tx = txid  # left open at EOF
        else:
            endings.append((txid, 'commit'))
        if open_tx == txid:
            # one staged change that never becomes visible
            declaration = rng.choice(declarations)
            row = [_random_scalar(rng, c['type']) for c in declaration['columns']]
            lsn += 1
            records.append({'lsn': lsn, 'txid': txid, 'kind': 'change',
                            'table': declaration['table'], 'before': None,
                            'after': row})
            continue
        change_count = rng.randint(0, 4)
        for _ in range(change_count):
            declaration = rng.choice(declarations)
            name = declaration['table']
            pk_positions = [index_of(declaration, c)
                            for c in declaration['primary_key']]
            rel = relations[name]
            lsn += 1
            if not rel or rng.random() < 0.4:
                row = [_random_scalar(rng, c['type'])
                       for c in declaration['columns']]
                records.append({'lsn': lsn, 'txid': txid, 'kind': 'change',
                                'table': name, 'before': None, 'after': row})
                relations[name][tuple(row[p] for p in pk_positions)] = row
            else:
                key = rng.choice(list(rel))
                before = list(rel[key])
                if rng.random() < 0.3:
                    records.append({'lsn': lsn, 'txid': txid,
                                    'kind': 'change', 'table': name,
                                    'before': before, 'after': None})
                    del rel[key]
                else:
                    after = list(before)
                    for position, column in enumerate(declaration['columns']):
                        if column['name'] not in declaration['primary_key'] and \
                                rng.random() < 0.5:
                            after[position] = _random_scalar(rng, column['type'])
                    records.append({'lsn': lsn, 'txid': txid,
                                    'kind': 'change', 'table': name,
                                    'before': before, 'after': after})
                    del rel[key]
                    relations[name][tuple(after[p] for p in pk_positions)] = after

    # Interleave endings after all changes are staged.
    for txid, ending in endings:
        lsn += 1
        records.append({'lsn': lsn, 'txid': txid,
                        'kind': 'abort' if ending == 'abort' else 'commit'})

    return {'dataset_id': 'random-%d' % seed, 'tables': declarations,
            'initial': initial, 'records': records}


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

def run_pipeline(raw):
    spec = cdc.validate_input(raw)
    material = cdc.materialize(spec)
    report = cdc.build_report(spec, 'a' * 64, material)
    cdc.validate_result(spec, 'a' * 64, report)
    return report


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding='utf-8'))


class FixedFixtureTest(unittest.TestCase):
    """The mandated linked, ordering and cyclic scenarios, audited in full."""

    def audit_map(self, report):
        return {entry['txid']: entry for entry in report['transactions']}

    def table_rows(self, report, name):
        table = next(item for item in report['tables'] if item['table'] == name)
        return table['rows']

    def test_linked_acceptance(self):
        report = run_pipeline(load_fixture('linked-acceptance.json'))
        audits = self.audit_map(report)
        self.assertEqual(
            audits['A'],
            {'txid': 'A', 'begin_lsn': 1, 'end_lsn': 6, 'status': 'rejected',
             'change_lsns': [3],
             'error': {'code': 'before_mismatch', 'change_lsn': 3,
                       'table': 'P', 'primary_key': [1], 'fk_id': None}})
        self.assertEqual(audits['B']['status'], 'accepted')
        self.assertEqual(audits['B']['end_lsn'], 5)
        self.assertEqual(audits['D']['status'], 'accepted')
        self.assertEqual(audits['D']['change_lsns'], [8, 9])
        self.assertEqual(
            audits['E']['error'],
            {'code': 'foreign_key', 'change_lsn': None, 'table': 'C',
             'primary_key': [10], 'fk_id': 'fk_c_p'})
        self.assertEqual(audits['E']['status'], 'rejected')
        self.assertEqual(audits['F']['status'], 'accepted')
        # Begin-order audit, including the exact intermediate sequence.
        self.assertEqual([entry['txid'] for entry in report['transactions']],
                         ['A', 'B', 'D', 'E', 'F'])
        self.assertEqual(self.table_rows(report, 'P'), [[1, 'B']])
        self.assertEqual(self.table_rows(report, 'C'), [])

    def test_commit_order_pair(self):
        first = run_pipeline(load_fixture('commit-order-y-first.json'))
        self.assertEqual([e['status'] for e in first['transactions']],
                         ['accepted', 'accepted'])
        self.assertEqual(self.table_rows(first, 'P'), [])
        self.assertEqual(self.table_rows(first, 'C'), [])

        second = run_pipeline(load_fixture('commit-order-x-first.json'))
        x, y = second['transactions']
        self.assertEqual(x['status'], 'rejected')
        self.assertEqual(x['error'],
                         {'code': 'foreign_key', 'change_lsn': None,
                          'table': 'C', 'primary_key': [10],
                          'fk_id': 'fk_c_p'})
        self.assertEqual(y['status'], 'accepted')
        self.assertEqual(self.table_rows(second, 'P'), [[1, 'root']])
        self.assertEqual(self.table_rows(second, 'C'), [])

    def test_cyclic_foreign_keys(self):
        report = run_pipeline(load_fixture('cyclic-foreign-keys.json'))
        t1, t2, t3 = report['transactions']
        self.assertEqual(t1['status'], 'accepted')
        self.assertEqual(t2['status'], 'rejected')
        self.assertEqual(t2['error']['code'], 'foreign_key')
        self.assertEqual(t2['error']['table'], 'V')
        self.assertEqual(t3['status'], 'accepted')
        self.assertEqual(self.table_rows(report, 'U'), [])
        self.assertEqual(self.table_rows(report, 'V'), [])

    def test_coverage_gallery(self):
        report = run_pipeline(load_fixture('coverage-gallery.json'))
        audits = self.audit_map(report)
        self.assertEqual(audits['empty-ok']['status'], 'accepted')
        self.assertEqual(audits['aborted-noise']['status'], 'aborted')
        self.assertIsNone(audits['aborted-noise']['error'])
        self.assertEqual(audits['left-open']['status'], 'open')
        self.assertIsNone(audits['left-open']['end_lsn'])
        for txid in ('self-insert', 'pk-update', 'same-key-twice',
                     'delete-reinsert', 'null-fk-exempt'):
            self.assertEqual(audits[txid]['status'], 'accepted', txid)
        self.assertEqual(audits['composite-fk-violation']['error']['code'],
                         'foreign_key')
        self.assertEqual(audits['composite-fk-violation']['error']['primary_key'],
                         ['ghost', 99])
        self.assertEqual(audits['missing-row']['error']['code'], 'missing_row')
        self.assertEqual(audits['before-mismatch']['error']['code'],
                         'before_mismatch')
        self.assertEqual(audits['pk-conflict']['error']['code'],
                         'primary_key_conflict')
        self.assertEqual(
            audits['unchanged-child-blocks-parent']['error'],
            {'code': 'foreign_key', 'change_lsn': None, 'table': 'leaf',
             'primary_key': ['root', 1], 'fk_id': 'fk_leaf_node'})
        # Open and rejected rows never leak.
        node_rows = self.table_rows(report, 'node')
        self.assertNotIn(['open', 7, None, None, True], node_rows)
        self.assertNotIn(['ghost', 99, None, None, True], node_rows)
        # Boolean false sorts before true; integer before string at same rank.
        self.assertEqual([row[:2] for row in node_rows],
                         [['orphan', 5], ['root', 1], ['root', 3],
                          ['root', 22]])

    def test_array_permutation_does_not_change_business_result(self):
        raw = load_fixture('linked-acceptance.json')
        shuffled = copy.deepcopy(raw)
        random.Random(7).shuffle(shuffled['tables'])
        random.Random(9).shuffle(shuffled['initial'])
        base = run_pipeline(raw)
        other = run_pipeline(shuffled)
        self.assertEqual(
            sorted((t['table'], t['rows']) for t in other['tables']),
            sorted((t['table'], t['rows']) for t in base['tables']))
        self.assertEqual(other['transactions'], base['transactions'])


class RandomDifferentialTest(unittest.TestCase):
    """Compare the engine against the naive interpreter on random logs."""

    def test_randomized_interleavings(self):
        for seed in range(120):
            raw = build_random_case(seed)
            report = run_pipeline(raw)
            expected_rows, expected_audits = reference_materialize(raw)
            self.assertEqual(
                report['transactions'], expected_audits,
                msg='audit mismatch on seed %d' % seed)
            for table in report['tables']:
                self.assertEqual(
                    table['rows'], expected_rows[table['table']],
                    msg='row mismatch on seed %d table %s'
                        % (seed, table['table']))

    def test_prefix_audits_are_stable_under_later_commits(self):
        """Every intermediate audit must equal the final report's audit entry."""
        for seed in (1, 4, 12, 23, 44):
            raw = build_random_case(seed)
            commit_points = [i for i, record in enumerate(raw['records'])
                             if record['kind'] in ('commit', 'abort')]
            final_report = run_pipeline(raw)
            final_by_tx = {e['txid']: e for e in final_report['transactions']}
            for point in commit_points:
                prefix = copy.deepcopy(raw)
                prefix['records'] = raw['records'][:point + 1]
                prefix_report = run_pipeline(prefix)
                for entry in prefix_report['transactions']:
                    if entry['status'] in ('accepted', 'rejected', 'aborted'):
                        self.assertEqual(entry, final_by_tx[entry['txid']],
                                         msg='seed %d point %d' % (seed, point))


class StructuralValidationTest(unittest.TestCase):
    """Whole-input failures: frame, type, key and ordering violations."""

    def base(self):
        return load_fixture('linked-acceptance.json')

    def reject(self, raw):
        with self.assertRaises((ValueError, TypeError)):
            cdc.validate_input(raw)

    def test_top_level_strictness(self):
        raw = self.base()
        raw['extra'] = 1
        self.reject(raw)
        del raw['records']
        self.reject(raw)
        self.reject({})

    def test_table_and_column_declarations(self):
        raw = self.base()
        raw['tables'][0]['columns'][0]['nullable'] = 'yes'
        self.reject(raw)
        raw = self.base()
        raw['tables'][0]['columns'][0]['type'] = 'float'
        self.reject(raw)
        raw = self.base()
        raw['tables'][0]['table'] = 'P'
        raw['tables'][1]['table'] = 'P'
        self.reject(raw)
        raw = self.base()
        raw['tables'][0]['columns'][0]['name'] = 'id'
        raw['tables'][0]['columns'][1]['name'] = 'id'
        self.reject(raw)
        raw = self.base()
        raw['tables'][0]['primary_key'] = ['nope']
        self.reject(raw)
        raw = self.base()
        raw['tables'][0]['primary_key'] = ['label']  # nullable=false required
        self.reject(raw)
        raw = self.base()
        raw['tables'][1]['foreign_keys'][0]['ref_table'] = 'GHOST'
        self.reject(raw)
        raw = self.base()
        raw['tables'][1]['foreign_keys'][0]['columns'] = ['id']
        # width matches but type of P.id is integer and C.id is integer too;
        # break positional type correspondence instead:
        raw['tables'][1]['columns'][0]['type'] = 'string'
        self.reject(raw)
        raw = self.base()
        raw['tables'][1]['foreign_keys'][0]['columns'] = ['id', 'p_id']
        self.reject(raw)  # must match full PK width

    def test_initial_failures(self):
        raw = self.base()
        raw['initial'].append({'table': 'P', 'values': [1, 'dup']})  # duplicate PK
        self.reject(raw)
        raw = self.base()
        raw['initial'].append({'table': 'C', 'values': [9, 99]})  # dangling FK
        self.reject(raw)
        raw = self.base()
        raw['initial'][0]['values'] = [1]  # wrong width
        self.reject(raw)
        raw = self.base()
        raw['initial'][0]['values'] = [True, 'old']  # bool is not integer
        self.reject(raw)

    def test_cell_contract(self):
        raw = self.base()
        raw['records'][2]['after'][0] = True
        self.reject(raw)
        raw = self.base()
        raw['records'][2]['after'][0] = 10 ** 12 + 1
        self.reject(raw)
        raw = self.base()
        raw['records'][2]['after'][1] = 'x' * 257
        self.reject(raw)
        raw = self.base()
        raw['records'][2]['after'][1] = 3  # string column, integer value
        self.reject(raw)

    def test_record_frames(self):
        raw = self.base()
        raw['records'][0]['lsn'] = 1
        raw['records'][1]['lsn'] = 1  # not strictly increasing
        self.reject(raw)
        raw = self.base()
        raw['records'][0]['lsn'] = True  # bool rejected as lsn
        self.reject(raw)
        raw = self.base()
        raw['records'][0]['kind'] = 'prepare'
        self.reject(raw)
        raw = self.base()
        raw['records'][0]['extra'] = 1
        self.reject(raw)
        raw = self.base()
        del raw['records'][2]['table']
        self.reject(raw)
        raw = self.base()
        raw['records'][2]['before'] = None
        raw['records'][2]['after'] = None
        self.reject(raw)
        raw = self.base()
        raw['records'][2]['table'] = 'GHOST'
        self.reject(raw)

    def test_transaction_lifecycle(self):
        raw = self.base()
        raw['records'].append({'lsn': 18, 'txid': 'A', 'kind': 'begin'})
        self.reject(raw)
        raw = self.base()
        raw['records'].insert(0, {'lsn': 0, 'txid': 'Z', 'kind': 'commit'})
        self.reject(raw)
        raw = self.base()
        raw['records'].append({'lsn': 18, 'txid': 'A', 'kind': 'abort'})
        self.reject(raw)
        raw = self.base()
        raw['records'][2]['txid'] = 'NEVER-BEGUN'
        self.reject(raw)

    def test_null_only_when_nullable(self):
        raw = load_fixture('coverage-gallery.json')
        raw['initial'][0]['values'][0] = None  # PK column not nullable
        self.reject(raw)

    def test_duplicate_json_keys_and_nonfinite_numbers(self):
        bodies = (
            '{"dataset_id":"x","dataset_id":"y","tables":[],"initial":[],"records":[]}',
            '{"dataset_id":NaN,"tables":[],"initial":[],"records":[]}',
            '{"dataset_id":"x","tables":[],"initial":[],"records":[1e999]}',
        )
        for body in bodies:
            with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False,
                                             encoding='utf-8') as handle:
                handle.write(body)
                path = Path(handle.name)
            try:
                with self.assertRaises(ValueError):
                    read(path)
            finally:
                path.unlink()

    def test_static_validation_even_when_aborted(self):
        raw = self.base()
        raw['records'].append({'lsn': 18, 'txid': 'A', 'kind': 'begin'})
        raw['records'].append({'lsn': 19, 'txid': 'A', 'kind': 'change',
                               'table': 'P', 'before': None,
                               'after': ['not-an-int', 'x']})
        raw['records'].append({'lsn': 20, 'txid': 'A', 'kind': 'abort'})
        self.reject(raw)


class SemanticEdgeTest(unittest.TestCase):
    def _run(self, raw):
        return run_pipeline(raw)

    def test_type_aware_equality_in_before(self):
        # A boolean value in a string column fails static typing outright:
        # whole-input failure, never a dynamic replay verdict.
        raw = {
            'dataset_id': 'type-aware',
            'tables': [{'table': 'T', 'columns': [
                {'name': 'id', 'type': 'integer', 'nullable': False},
                {'name': 'note', 'type': 'string', 'nullable': False}],
                'primary_key': ['id'], 'foreign_keys': []}],
            'initial': [{'table': 'T', 'values': [1, 'true']}],
            'records': [
                {'lsn': 1, 'txid': 'x', 'kind': 'begin'},
                {'lsn': 2, 'txid': 'x', 'kind': 'change', 'table': 'T',
                 'before': [1, True], 'after': [1, 'false']},
                {'lsn': 3, 'txid': 'x', 'kind': 'commit'}]}
        with self.assertRaises(ValueError):
            self._run(raw)

        # The genuine type-aware replay check: a stored string "1" must not
        # match an integer cell even though Python would equate "1" != 1 too;
        # here two strings differ in case with no normalization.
        raw2 = {
            'dataset_id': 'type-aware-2',
            'tables': [{'table': 'T', 'columns': [
                {'name': 'id', 'type': 'integer', 'nullable': False},
                {'name': 'note', 'type': 'string', 'nullable': False}],
                'primary_key': ['id'], 'foreign_keys': []}],
            'initial': [{'table': 'T', 'values': [1, 'Abc']}],
            'records': [
                {'lsn': 1, 'txid': 'x', 'kind': 'begin'},
                {'lsn': 2, 'txid': 'x', 'kind': 'change', 'table': 'T',
                 'before': [1, 'abc'], 'after': [1, 'xyz']},
                {'lsn': 3, 'txid': 'x', 'kind': 'commit'}]}
        report = self._run(raw2)
        self.assertEqual(report['transactions'][0]['error']['code'],
                         'before_mismatch')

    def test_rows_equal_is_type_aware(self):
        self.assertTrue(cdc._rows_equal((1, 'x', True), (1, 'x', True)))
        self.assertFalse(cdc._rows_equal((1,), (True,)))
        self.assertFalse(cdc._rows_equal(('a',), ('A',)))
        self.assertFalse(cdc._rows_equal((0,), (False,)))
        self.assertTrue(cdc._rows_equal((None,), (None,)))

    def test_unicode_code_point_row_order(self):
        raw = {
            'dataset_id': 'ordering',
            'tables': [{'table': 'T', 'columns': [
                {'name': 'id', 'type': 'string', 'nullable': False}],
                'primary_key': ['id'], 'foreign_keys': []}],
            'initial': [],
            'records': []}
        lsn = 0
        for index, key in enumerate(('β', 'α', 'Z', 'a', '1'), start=1):
            lsn += 1
            raw['records'].append(
                {'lsn': lsn, 'txid': 'i%d' % index, 'kind': 'begin'})
            lsn += 1
            raw['records'].append(
                {'lsn': lsn, 'txid': 'i%d' % index, 'kind': 'change',
                 'table': 'T', 'before': None, 'after': [key]})
            lsn += 1
            raw['records'].append(
                {'lsn': lsn, 'txid': 'i%d' % index, 'kind': 'commit'})
        report = self._run(raw)
        rows = report['tables'][0]['rows']
        self.assertEqual(rows, [['1'], ['Z'], ['a'], ['α'], ['β']])

    def test_uncommitted_rows_are_invisible_to_other_transactions(self):
        raw = {
            'dataset_id': 'isolation',
            'tables': [{'table': 'T', 'columns': [
                {'name': 'id', 'type': 'integer', 'nullable': False}],
                'primary_key': ['id'], 'foreign_keys': []}],
            'initial': [],
            'records': [
                {'lsn': 1, 'txid': 'open-tx', 'kind': 'begin'},
                {'lsn': 2, 'txid': 'open-tx', 'kind': 'change', 'table': 'T',
                 'before': None, 'after': [7]},
                {'lsn': 3, 'txid': 'other', 'kind': 'begin'},
                {'lsn': 4, 'txid': 'other', 'kind': 'change', 'table': 'T',
                 'before': None, 'after': [7]},
                {'lsn': 5, 'txid': 'other', 'kind': 'commit'}]}
        report = self._run(raw)
        statuses = {e['txid']: e['status'] for e in report['transactions']}
        self.assertEqual(statuses['other'], 'accepted')
        self.assertEqual(statuses['open-tx'], 'open')
        self.assertEqual(report['tables'][0]['rows'], [[7]])

    def test_update_changing_primary_key_delete_first_semantics(self):
        raw = {
            'dataset_id': 'pk-move',
            'tables': [{'table': 'T', 'columns': [
                {'name': 'id', 'type': 'integer', 'nullable': False}],
                'primary_key': ['id'], 'foreign_keys': []}],
            'initial': [{'table': 'T', 'values': [1]},
                        {'table': 'T', 'values': [2]}],
            'records': [
                {'lsn': 1, 'txid': 'move', 'kind': 'begin'},
                {'lsn': 2, 'txid': 'move', 'kind': 'change', 'table': 'T',
                 'before': [1], 'after': [2]},
                {'lsn': 3, 'txid': 'move', 'kind': 'commit'}]}
        report = self._run(raw)
        self.assertEqual(report['transactions'][0]['error']['code'],
                         'primary_key_conflict')
        self.assertEqual(report['transactions'][0]['error']['primary_key'], [2])

    def test_delete_and_reinsert_same_key_in_one_transaction(self):
        raw = {
            'dataset_id': 'reinsert',
            'tables': [{'table': 'T', 'columns': [
                {'name': 'id', 'type': 'integer', 'nullable': False},
                {'name': 'v', 'type': 'string', 'nullable': True}],
                'primary_key': ['id'], 'foreign_keys': []}],
            'initial': [{'table': 'T', 'values': [1, 'a']}],
            'records': [
                {'lsn': 1, 'txid': 'r', 'kind': 'begin'},
                {'lsn': 2, 'txid': 'r', 'kind': 'change', 'table': 'T',
                 'before': [1, 'a'], 'after': None},
                {'lsn': 3, 'txid': 'r', 'kind': 'change', 'table': 'T',
                 'before': None, 'after': [1, 'b']},
                {'lsn': 4, 'txid': 'r', 'kind': 'commit'}]}
        report = self._run(raw)
        self.assertEqual(report['transactions'][0]['status'], 'accepted')
        self.assertEqual(report['tables'][0]['rows'], [[1, 'b']])

    def test_fk_violation_tie_break_order(self):
        raw = {
            'dataset_id': 'fk-tie',
            'tables': [
                {'table': 'P', 'columns': [
                    {'name': 'id', 'type': 'integer', 'nullable': False}],
                    'primary_key': ['id'], 'foreign_keys': []},
                {'table': 'A', 'columns': [
                    {'name': 'id', 'type': 'integer', 'nullable': False},
                    {'name': 'p_id', 'type': 'integer', 'nullable': False}],
                    'primary_key': ['id'],
                    'foreign_keys': [{'fk_id': 'z_fk', 'columns': ['p_id'],
                                      'ref_table': 'P'}]},
                {'table': 'B', 'columns': [
                    {'name': 'id', 'type': 'integer', 'nullable': False},
                    {'name': 'p_id', 'type': 'integer', 'nullable': False}],
                    'primary_key': ['id'],
                    'foreign_keys': [{'fk_id': 'a_fk', 'columns': ['p_id'],
                                      'ref_table': 'P'}]}],
            'initial': [{'table': 'P', 'values': [1]}],
            'records': [
                {'lsn': 1, 'txid': 'setup', 'kind': 'begin'},
                {'lsn': 2, 'txid': 'setup', 'kind': 'change', 'table': 'A',
                 'before': None, 'after': [5, 1]},
                {'lsn': 3, 'txid': 'setup', 'kind': 'change', 'table': 'B',
                 'before': None, 'after': [5, 1]},
                {'lsn': 4, 'txid': 'setup', 'kind': 'commit'},
                {'lsn': 5, 'txid': 'drop', 'kind': 'begin'},
                {'lsn': 6, 'txid': 'drop', 'kind': 'change', 'table': 'P',
                 'before': [1], 'after': None},
                {'lsn': 7, 'txid': 'drop', 'kind': 'commit'}]}
        report = self._run(raw)
        error = report['transactions'][1]['error']
        self.assertEqual(error['code'], 'foreign_key')
        self.assertEqual(error['table'], 'A')  # table name before fk_id ordering
        self.assertEqual(error['fk_id'], 'z_fk')
        self.assertEqual(error['primary_key'], [5])


class CommandLineTest(unittest.TestCase):
    def call(self, *args):
        env = dict(os.environ)
        env['PYTHONPATH'] = str(SRC)
        return subprocess.run([sys.executable, '-m', 'loan_schedule.cli',
                               *map(str, args)],
                              capture_output=True, text=True, env=env)

    def call_entry_point(self, *args):
        env = dict(os.environ)
        env['PYTHONPATH'] = str(SRC)
        return subprocess.run([sys.executable, '-c',
                               'from loan_schedule.cli import '
                               'cdc_materialize_main; '
                               'raise SystemExit(cdc_materialize_main())',
                               *map(str, args)],
                              capture_output=True, text=True, env=env)

    def test_module_and_entry_point_share_semantics(self):
        source = FIXTURES / 'cyclic-foreign-keys.json'
        with tempfile.TemporaryDirectory() as directory:
            out1 = Path(directory) / 'one.json'
            out2 = Path(directory) / 'two.json'
            first = self.call('cdc-materialize', '--source', source,
                              '--output', out1)
            second = self.call_entry_point('--source', source, '--output', out2)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(out1.read_bytes(), out2.read_bytes())

    def test_deterministic_rerun_and_sha(self):
        source = FIXTURES / 'linked-acceptance.json'
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'report.json'
            self.assertEqual(
                self.call('cdc-materialize', '--source', source,
                          '--output', out).returncode, 0)
            first = out.read_bytes()
            self.assertEqual(
                json.loads(first)['source_sha256'],
                hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertEqual(
                self.call('cdc-materialize', '--source', source,
                          '--output', out).returncode, 0)
            self.assertEqual(out.read_bytes(), first)

    def test_same_path_and_symlink_rejected(self):
        source = FIXTURES / 'linked-acceptance.json'
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / 'in.json'
            copied.write_bytes(source.read_bytes())
            result = self.call('cdc-materialize', '--source', copied,
                               '--output', copied)
            self.assertEqual(result.returncode, 2)
            link = Path(directory) / 'link.json'
            link.symlink_to(copied)
            result = self.call('cdc-materialize', '--source', copied,
                               '--output', link)
            self.assertEqual(result.returncode, 2)

    def test_failure_preserves_previous_report(self):
        source = FIXTURES / 'linked-acceptance.json'
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'report.json'
            out.write_text('prior-report')
            bad = Path(directory) / 'bad.json'
            bad.write_text('{"dataset_id":"x"}')
            result = self.call('cdc-materialize', '--source', bad,
                               '--output', out)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(out.read_text(), 'prior-report')

    def test_business_rejection_still_exits_zero(self):
        source = FIXTURES / 'linked-acceptance.json'
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'report.json'
            result = self.call('cdc-materialize', '--source', source,
                               '--output', out)
            self.assertEqual(result.returncode, 0)
            report = json.loads(out.read_text())
            self.assertTrue(any(t['status'] == 'rejected'
                                for t in report['transactions']))

    def test_input_is_not_modified(self):
        source = FIXTURES / 'linked-acceptance.json'
        before = source.read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'report.json'
            self.assertEqual(
                self.call('cdc-materialize', '--source', source,
                          '--output', out).returncode, 0)
        self.assertEqual(source.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
