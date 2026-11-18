"""Contract tests for the ``table-keys`` relational candidate-key product.

The production boundary search and uniqueness helpers are never used by the
oracle in this file: the oracle enumerates the full column-subset lattice and
tests each subset directly with set hashing.  Random tables of up to eight
columns are checked for both complete boundaries and the smallest collision
witness.
"""
import json
import os
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'fixtures' / 'table_keys'
SRC = ROOT / 'src'
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from loan_schedule import table_keys
from loan_schedule.common import read


def call(*args):
    env = dict(os.environ)
    env['PYTHONPATH'] = str(SRC)
    return subprocess.run([sys.executable, '-m', 'loan_schedule.cli', *map(str, args)],
                          capture_output=True, text=True, env=env)


# --------------------------------------------------------------------------- #
# Independent oracle: full subset enumeration, no production search calls
# --------------------------------------------------------------------------- #

def tag(value):
    """Independent type-aware marker: Python otherwise conflates True and 1."""
    if value is None:
        return ('null',)
    if isinstance(value, bool):
        return ('bool', value)
    if isinstance(value, int):
        return ('int', value)
    return ('str', value)


def oracle(columns, rows):
    """Brute-force the entire subset lattice over type-tagged cells."""
    n = len(columns)
    m = len(rows)
    tagged = [[tag(value) for value in row] for row in rows]

    def unique(indices):
        seen = set()
        for cells in tagged:
            key = tuple(cells[j] for j in indices)
            if key in seen:
                return False
            seen.add(key)
        return True

    unique_masks, nonkey_masks = [], []
    for mask in range(1 << n):
        indices = [j for j in range(n) if mask & (1 << j)]
        (unique_masks if unique(indices) else nonkey_masks).append(mask)

    minimal_keys = [
        mask for mask in unique_masks
        if not any(other != mask and (mask & other) == other for other in unique_masks)
    ]
    maximal_nonkeys = [
        mask for mask in nonkey_masks
        if not any(other != mask and (mask & other) == mask for other in nonkey_masks)
    ]

    witnesses = {}
    for mask in maximal_nonkeys:
        indices = [j for j in range(n) if mask & (1 << j)]
        best = None
        for x in range(m):
            for y in range(x + 1, m):
                if all(tagged[x][j] == tagged[y][j] for j in indices):
                    pair = (x, y)  # rows are already row_id ordered
                    if best is None or pair < best:
                        best = pair
        witnesses[mask] = best
    return minimal_keys, maximal_nonkeys, witnesses


def run_pipeline(data):
    spec = table_keys.validate_input(data)
    found = table_keys.discover_boundaries(spec)
    report = table_keys.build_report(spec, '0' * 64, found)
    table_keys.validate_result(spec, report, found['path'])
    return spec, found, report


def report_masks(report, names):
    index = {name: j for j, name in enumerate(names)}
    keys = [sum(1 << index[name] for name in group)
            for group in report['minimal_keys']]
    nonkeys = {}
    for item in report['maximal_nonkeys']:
        mask = sum(1 << index[name] for name in item['columns'])
        nonkeys[mask] = tuple(item['witness'])
    return keys, nonkeys


def equivalent_classes(columns, rows):
    """Column indices sharing an identical type-tagged value vector."""
    vectors = {j: tuple(tag(row[j]) for row in rows)
               for j in range(len(columns))}
    classes = {}
    for j, vector in vectors.items():
        classes.setdefault(vector, []).append(j)
    return list(classes.values())


def expand_class_keys(class_key_masks, class_members, n):
    """Expand one mask per equivalence class into all member-choice masks."""
    expanded = set()
    for mask in class_key_masks:
        choices = [class_members[c]
                   for c in range(len(class_members)) if mask & (1 << c)]

        def grow(position, acc):
            if position == len(choices):
                expanded.add(acc)
                return
            for column in choices[position]:
                grow(position + 1, acc | (1 << column))

        grow(0, 0)
    return expanded


def expand_whole_class_masks(class_masks, class_members):
    """An included class contributes every member; excluded contributes none."""
    expanded = set()
    for mask in class_masks:
        full = 0
        for c in range(len(class_members)):
            if mask & (1 << c):
                for column in class_members[c]:
                    full |= 1 << column
        expanded.add(full)
    return expanded


class ParityAcceptanceTest(unittest.TestCase):
    """The four-row parity relation from the acceptance statement."""

    def setUp(self):
        self.data = {
            'dataset_id': 'parity', 'columns': ['A', 'B', 'C'],
            'rows': [
                {'row_id': 'r1', 'values': [0, 0, 0]},
                {'row_id': 'r2', 'values': [0, 1, 1]},
                {'row_id': 'r3', 'values': [1, 0, 1]},
                {'row_id': 'r4', 'values': [1, 1, 0]}]}
        self.spec, self.found, self.report = run_pipeline(self.data)

    def test_boundaries(self):
        keys, nonkeys = report_masks(self.report, ['A', 'B', 'C'])
        self.assertEqual(sorted(keys), [0b011, 0b101, 0b110])
        self.assertEqual(sorted(nonkeys), [0b001, 0b010, 0b100])

    def test_witnesses(self):
        expected = {0b001: ('r1', 'r2'), 0b010: ('r1', 'r3'),
                    0b100: ('r1', 'r4')}
        _, nonkeys = report_masks(self.report, ['A', 'B', 'C'])
        for mask, pair in expected.items():
            self.assertEqual(nonkeys[mask], pair)

    def test_matches_independent_oracle(self):
        rows = [cells for _, cells in self.spec['rows']]
        class_members = equivalent_classes(['A', 'B', 'C'], rows)
        k = len(class_members)
        class_rows = [[rows[r][group[0]] for group in class_members]
                      for r in range(len(rows))]
        o_class_keys, o_class_nonkeys, o_class_wit = oracle(
            ['g%d' % c for c in range(k)], class_rows)
        o_keys = expand_class_keys(o_class_keys, class_members, 3)
        o_nonkeys = expand_whole_class_masks(o_class_nonkeys, class_members)
        keys, nonkeys = report_masks(self.report, self.spec['columns'])
        self.assertEqual(set(keys), o_keys)
        self.assertEqual(set(nonkeys), o_nonkeys)
        id_order = [rid for rid, _ in self.spec['rows']]
        for class_mask, (x, y) in o_class_wit.items():
            full = 0
            for c in range(k):
                if class_mask & (1 << c):
                    for column in class_members[c]:
                        full |= 1 << column
            self.assertEqual(nonkeys[full], (id_order[x], id_order[y]))


class SupplementaryKeysTest(unittest.TestCase):
    """A is unique alone; B and C together form the other key."""

    def test_keys_are_A_and_BC(self):
        data = {'dataset_id': 'd', 'columns': ['A', 'B', 'C'],
                'rows': [
                    {'row_id': 'r0', 'values': [0, 0, 0]},
                    {'row_id': 'r1', 'values': [1, 0, 1]},
                    {'row_id': 'r2', 'values': [2, 1, 0]},
                    {'row_id': 'r3', 'values': [3, 1, 1]}]}
        _, _, report = run_pipeline(data)
        self.assertEqual(sorted(report['minimal_keys']),
                         [['A'], ['B', 'C']])
        self.assertEqual(sorted(item['columns']
                                for item in report['maximal_nonkeys']),
                         [['B'], ['C']])


class BoundaryShapeTest(unittest.TestCase):

    def profile(self, columns, rows):
        data = {'dataset_id': 'd', 'columns': columns,
                'rows': [{'row_id': 'r%d' % i, 'values': list(values)}
                         for i, values in enumerate(rows)]}
        return run_pipeline(data)[2]

    def test_zero_rows(self):
        report = self.profile(['A', 'B'], [])
        self.assertEqual(report['minimal_keys'], [[]])
        self.assertEqual(report['maximal_nonkeys'], [])
        self.assertEqual(report['row_count'], 0)

    def test_single_row(self):
        report = self.profile(['A', 'B'], [(1, 'x')])
        self.assertEqual(report['minimal_keys'], [[]])
        self.assertEqual(report['maximal_nonkeys'], [])

    def test_full_duplicate_rows(self):
        report = self.profile(['A', 'B'], [(1, 2), (1, 2), (3, 4)])
        self.assertEqual(report['minimal_keys'], [])
        self.assertEqual(len(report['maximal_nonkeys']), 1)
        item = report['maximal_nonkeys'][0]
        self.assertEqual(item['columns'], ['A', 'B'])
        self.assertEqual(item['witness'], ['r0', 'r1'])

    def test_constant_and_equivalent_columns(self):
        report = self.profile(['A', 'B', 'C'],
                              [(1, 5, 5), (2, 6, 6), (3, 7, 7)])
        # B and C are interchangeable; each single column is a minimal key
        self.assertEqual(sorted(report['minimal_keys']),
                         [['A'], ['B'], ['C']])
        # Every non-empty group is a key, so the empty set is the sole maximal
        # non-key (rows still collide when no columns are selected).
        self.assertEqual(len(report['maximal_nonkeys']), 1)
        self.assertEqual(report['maximal_nonkeys'][0]['columns'], [])

    def test_single_all_null_column(self):
        report = self.profile(['A'], [(None,), (None,), (None,)])
        self.assertEqual(report['minimal_keys'], [])
        self.assertEqual(report['maximal_nonkeys'][0]['columns'], ['A'])
        self.assertEqual(report['maximal_nonkeys'][0]['witness'],
                         ['r0', 'r1'])

    def test_type_distinction(self):
        # true, 1, "1", null are four distinct values; the second true collides
        report = self.profile(['X'],
                              [(True,), (1,), ("1",), (None,), (True,)])
        self.assertEqual(report['minimal_keys'], [])
        item = report['maximal_nonkeys'][0]
        self.assertEqual(item['witness'], ['r0', 'r4'])

    def test_zero_equals_minus_zero_but_string_null_differs(self):
        report = self.profile(['X'], [(0,), (-0,)])
        self.assertEqual(report['minimal_keys'], [])  # 0 == -0, so collision
        report2 = self.profile(['X'], [('null',), (None,)])
        self.assertEqual(report2['minimal_keys'], [['X']])  # string vs null

    def test_bools_are_not_integers(self):
        report = self.profile(['X'], [(True,), (1,)])
        self.assertEqual(report['minimal_keys'], [['X']])


class OracleFuzzTest(unittest.TestCase):
    """Random tables of up to eight columns vs the exhaustive oracle."""

    def _assert_matches_oracle(self, columns, cells):
        # Oracle over the equivalence-class lattice, then expand to columns.
        class_members = equivalent_classes(columns, cells)
        k = len(class_members)
        # One representative raw column per equivalence class.
        class_rows = [[cells[r][group[0]] for group in class_members]
                      for r in range(len(cells))]
        o_class_keys, o_class_nonkeys, o_class_wit = oracle(
            ['g%d' % c for c in range(k)], class_rows)
        n = len(columns)
        o_keys = expand_class_keys(o_class_keys, class_members, n)
        o_nonkeys = expand_whole_class_masks(o_class_nonkeys, class_members)

        data = {'dataset_id': 'fuzz', 'columns': columns,
                'rows': [{'row_id': 'r%02d' % i, 'values': row}
                         for i, row in enumerate(cells)]}
        spec, _, report = run_pipeline(data)
        keys, nonkeys = report_masks(report, columns)
        self.assertEqual(set(keys), o_keys)
        self.assertEqual(set(nonkeys), o_nonkeys)
        id_order = [rid for rid, _ in spec['rows']]
        # Witnesses are attached to the expanded non-key masks.
        for class_mask, (x, y) in o_class_wit.items():
            full = 0
            for c in range(k):
                if class_mask & (1 << c):
                    for column in class_members[c]:
                        full |= 1 << column
            self.assertEqual(nonkeys[full], (id_order[x], id_order[y]))

    def test_random_tables(self):
        random.seed(20261118)
        values = [None, True, False, 0, 1, -1, '1', 'null', '', 'x', 2]
        for _ in range(120):
            n = random.randint(1, 8)
            m = random.randint(0, 12)
            columns = ['c%02d' % j for j in range(n)]
            cells = [[random.choice(values) for _ in range(n)]
                     for _ in range(m)]
            self._assert_matches_oracle(columns, cells)

    def test_random_tables_on_forced_dfs_path(self):
        random.seed(99)
        values = [None, True, 0, 's', -3]
        original_budget = table_keys.STORE_BYTE_BUDGET
        try:
            table_keys.STORE_BYTE_BUDGET = 0
            for _ in range(40):
                n = random.randint(1, 7)
                m = random.randint(2, 10)
                columns = ['c%02d' % j for j in range(n)]
                cells = [[random.choice(values) for _ in range(n)]
                         for _ in range(m)]
                self._assert_matches_oracle(columns, cells)
        finally:
            table_keys.STORE_BYTE_BUDGET = original_budget


class OrderInvarianceTest(unittest.TestCase):

    def test_row_and_column_permutation_bytes_identical(self):
        base = {'dataset_id': 'd', 'columns': ['A', 'B', 'C'], 'rows': [
            {'row_id': 'r1', 'values': [0, 0, 0]},
            {'row_id': 'r2', 'values': [0, 1, 1]},
            {'row_id': 'r3', 'values': [1, 0, 1]},
            {'row_id': 'r4', 'values': [1, 1, 0]}]}
        permuted = {'dataset_id': 'd', 'columns': ['C', 'A', 'B'], 'rows': [
            {'row_id': 'r4', 'values': [0, 1, 1]},
            {'row_id': 'r1', 'values': [0, 0, 0]},
            {'row_id': 'r3', 'values': [1, 1, 0]},
            {'row_id': 'r2', 'values': [1, 0, 1]}]}
        _, _, r1 = run_pipeline(base)
        _, _, r2 = run_pipeline(permuted)
        self.assertEqual(json.dumps(r1, sort_keys=True, ensure_ascii=False),
                         json.dumps(r2, sort_keys=True, ensure_ascii=False))


class StrictValidationTest(unittest.TestCase):

    def reject(self, data, needle=''):
        with self.assertRaises(ValueError) as context:
            table_keys.validate_input(data)
        if needle:
            self.assertIn(needle, str(context.exception))

    def test_rejects_float_nested_unknown_and_missing(self):
        good_rows = []
        self.reject({'dataset_id': 'd', 'columns': ['A'],
                     'rows': [{'row_id': 'r', 'values': [1.5]}]})
        self.reject({'dataset_id': 'd', 'columns': ['A'],
                     'rows': [{'row_id': 'r', 'values': [[1]]}]})
        self.reject({'dataset_id': 'd', 'columns': ['A'],
                     'rows': [], 'extra': 1})
        self.reject({'dataset_id': 'd', 'columns': ['A']})
        self.reject({'dataset_id': 'd', 'columns': ['A'],
                     'rows': [{'row_id': 'r', 'values': [1], 'z': 2}]})

    def test_rejects_duplicate_and_bounds(self):
        self.reject({'dataset_id': 'd', 'columns': ['A', 'A'], 'rows': []},
                    'duplicate column')
        self.reject({'dataset_id': 'd', 'columns': ['A'], 'rows': [
            {'row_id': 'r', 'values': [1]},
            {'row_id': 'r', 'values': [2]}]}, 'duplicate row_id')
        self.reject({'dataset_id': 'd', 'columns': ['A', 'B'], 'rows': [
            {'row_id': 'r', 'values': [1]}]}, 'exactly 2')
        self.reject({'dataset_id': 'd', 'columns': ['A'], 'rows': [
            {'row_id': 'r', 'values': [10 ** 12 + 1]}]}, 'integer outside')
        self.reject({'dataset_id': 'd', 'columns': ['A'], 'rows': [
            {'row_id': 'r', 'values': ['x' * 257]}]}, 'string longer')
        self.reject({'dataset_id': 'd',
                     'columns': ['c%d' % i for i in range(25)], 'rows': []},
                    '1..24')

    def test_duplicate_json_key_and_nonfinite_rejected_by_reader(self):
        with tempfile.TemporaryDirectory() as td:
            dup = Path(td) / 'dup.json'
            dup.write_text(
                '{"dataset_id":"d","dataset_id":"e","columns":["A"],"rows":[]}',
                encoding='utf-8')
            with self.assertRaises(ValueError):
                read(dup)
            nan = Path(td) / 'nan.json'
            nan.write_text(
                '{"dataset_id":"d","columns":["A"],"rows":['
                '{"row_id":"r","values":[NaN]}]}', encoding='utf-8')
            with self.assertRaises(ValueError):
                read(nan)


class CommandLineTest(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.source = Path(self.temp.name) / 'in.json'
        self.output = Path(self.temp.name) / 'out.json'
        self.source.write_text(json.dumps({
            'dataset_id': 'parity', 'columns': ['A', 'B', 'C'],
            'rows': [
                {'row_id': 'r1', 'values': [0, 0, 0]},
                {'row_id': 'r2', 'values': [0, 1, 1]},
                {'row_id': 'r3', 'values': [1, 0, 1]},
                {'row_id': 'r4', 'values': [1, 1, 0]}]}), encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def test_module_invocation_and_determinism(self):
        first = call('table-keys', '--source', self.source, '--output', self.output)
        self.assertEqual(first.returncode, 0, first.stderr)
        before = self.output.read_bytes()
        second = call('table-keys', '--source', self.source, '--output', self.output)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.output.read_bytes(), before)

    def test_same_path_and_symlink_rejected(self):
        same = call('table-keys', '--source', self.source, '--output', self.source)
        self.assertEqual(same.returncode, 2)
        link = Path(self.temp.name) / 'link.json'
        link.symlink_to(self.source)
        linked = call('table-keys', '--source', self.source, '--output', link)
        self.assertEqual(linked.returncode, 2)

    def test_failure_preserves_old_report(self):
        call('table-keys', '--source', self.source, '--output', self.output)
        original = self.output.read_bytes()
        bad = Path(self.temp.name) / 'bad.json'
        bad.write_text('{"dataset_id":"d","columns":["A"],"rows":'
                       '[{"row_id":"r","values":[1.5]}]}', encoding='utf-8')
        failed = call('table-keys', '--source', bad, '--output', self.output)
        self.assertEqual(failed.returncode, 2)
        self.assertTrue(failed.stderr.strip())
        self.assertEqual(self.output.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
