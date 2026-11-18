"""Contract tests for the ``meter-tou`` time-of-use billing dataset."""
import copy
import hashlib
import json
import random
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock as mock
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'fixtures' / 'meter'
SRC = ROOT / 'src'
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from loan_schedule import meter_tou
from loan_schedule.common import read


def call(*args):
    env = {'PYTHONPATH': str(SRC)}
    return subprocess.run([sys.executable, '-m', 'loan_schedule.cli', *map(str, args)],
                          capture_output=True, text=True, env=env)


def sample(name='sample.json'):
    return json.loads((FIXTURES / name).read_text(encoding='utf-8'))


def epoch(year, month, day, hour=0, minute=0, second=0):
    return int(datetime(year, month, day, hour, minute, second,
                        tzinfo=timezone.utc).timestamp())


def run(spec, digest='0' * 64):
    normalized = meter_tou.validate_input(spec)
    report = meter_tou.build_report(normalized, digest)
    meter_tou.validate_result(normalized, report)
    return report


def one_day_zone(zone, window_start, window_end, intervals, tariffs):
    return {'dataset_id': 'case', 'timezone': zone,
            'window_start': window_start, 'window_end': window_end,
            'meters': sorted({row['meter_id'] for row in intervals}),
            'tariffs': tariffs, 'intervals': intervals}


FLAT = {'tariff_id': 'T1', 'start_minute': 0, 'end_minute': 1440,
        'rate_numerator': 1, 'rate_denominator': 1}


def write(path, value):
    path.write_text(json.dumps(value), encoding='utf-8')


class FallBackTest(unittest.TestCase):
    """A complete fall-back local day is 90000 physical seconds."""

    def setUp(self):
        self.window = (epoch(2025, 11, 2, 4), epoch(2025, 11, 3, 5))
        self.tariffs = [
            {'tariff_id': 'T1', 'start_minute': 0, 'end_minute': 60,
             'rate_numerator': 1, 'rate_denominator': 1},
            {'tariff_id': 'T3', 'start_minute': 60, 'end_minute': 120,
             'rate_numerator': 3, 'rate_denominator': 1},
            {'tariff_id': 'T2', 'start_minute': 120, 'end_minute': 1440,
             'rate_numerator': 1, 'rate_denominator': 1}]

    def spec(self):
        return one_day_zone(
            'America/New_York', *self.window,
            [{'interval_id': 'i1', 'meter_id': 'm1',
              'start': epoch(2025, 11, 2, 4), 'end': epoch(2025, 11, 3, 5),
              'energy_wh': 2500}], self.tariffs)

    def test_complete_day_and_repeated_hour(self):
        report = run(self.spec())
        day = report['days'][0]
        self.assertEqual(day['expected_seconds'], 90000)
        self.assertEqual(day['coverage_status'], 'complete')
        self.assertEqual(day['missing_seconds'], 0)
        self.assertEqual(day['cost_exact'], {'numerator': 2900, 'denominator': 1})
        self.assertEqual(day['cost_integer'], 2900)
        by_id = {row['tariff_id']: row for row in day['tariffs']}
        self.assertEqual(by_id['T3']['energy_wh_exact'],
                         {'numerator': 200, 'denominator': 1})
        self.assertEqual(by_id['T1']['energy_wh_exact'],
                         {'numerator': 100, 'denominator': 1})
        self.assertEqual(by_id['T2']['energy_wh_exact'],
                         {'numerator': 2200, 'denominator': 1})
        repeated = [row for row in report['segments']
                    if row['tariff_id'] == 'T3']
        self.assertEqual([row['utc_offset_seconds'] for row in repeated],
                         [-14400, -18000])
        self.assertEqual([row['seconds'] for row in repeated], [3600, 3600])
        self.assertEqual([row['local_date'] for row in repeated],
                         ['2025-11-02', '2025-11-02'])


class SpringForwardTest(unittest.TestCase):
    """A complete spring-forward local day is 82800 seconds; 02:00 does not exist."""

    def test_missing_local_hour_is_not_synthesized(self):
        window = (epoch(2025, 3, 9, 5), epoch(2025, 3, 10, 4))
        tariffs = [
            {'tariff_id': 'T1', 'start_minute': 0, 'end_minute': 120,
             'rate_numerator': 1, 'rate_denominator': 1},
            {'tariff_id': 'T3', 'start_minute': 120, 'end_minute': 180,
             'rate_numerator': 3, 'rate_denominator': 1},
            {'tariff_id': 'T2', 'start_minute': 180, 'end_minute': 1440,
             'rate_numerator': 1, 'rate_denominator': 1}]
        report = run(one_day_zone(
            'America/New_York', *window,
            [{'interval_id': 'i1', 'meter_id': 'm1', 'start': window[0],
              'end': window[1], 'energy_wh': 2300}], tariffs))
        day = report['days'][0]
        self.assertEqual(day['expected_seconds'], 82800)
        self.assertEqual(day['coverage_status'], 'complete')
        by_id = {row['tariff_id']: row for row in day['tariffs']}
        self.assertEqual(by_id['T3']['energy_wh_exact'],
                         {'numerator': 0, 'denominator': 1})
        self.assertEqual(by_id['T1']['energy_wh_exact'],
                         {'numerator': 200, 'denominator': 1})
        self.assertEqual(by_id['T2']['energy_wh_exact'],
                         {'numerator': 2100, 'denominator': 1})
        self.assertEqual(day['cost_exact'], {'numerator': 2300, 'denominator': 1})


class ClippingAndRoundingTest(unittest.TestCase):

    def test_window_clip_and_gap(self):
        start = epoch(2025, 1, 15)
        report = run({
            'dataset_id': 'd', 'timezone': 'UTC',
            'window_start': start, 'window_end': start + 7200,
            'meters': ['m1', 'm2', 'm3'], 'tariffs': [FLAT],
            'intervals': [
                {'interval_id': 'a', 'meter_id': 'm1',
                 'start': start - 3600, 'end': start + 3600, 'energy_wh': 2},
                # Wholly outside intervals are validated but emit no segment.
                {'interval_id': 'outside-before', 'meter_id': 'm1',
                 'start': start - 7200, 'end': start - 5400, 'energy_wh': 9},
                {'interval_id': 'outside-after', 'meter_id': 'm2',
                 'start': start + 7200, 'end': start + 7500, 'energy_wh': 9},
                {'interval_id': 'ends-at-window-end', 'meter_id': 'm2',
                 'start': start + 3600, 'end': start + 7200,
                 'energy_wh': 1}]})
        day = report['days'][0]
        self.assertEqual(day['energy_wh_exact'], {'numerator': 1, 'denominator': 1})
        self.assertEqual(day['covered_seconds'], 3600)
        self.assertEqual(day['missing_seconds'], 3600)
        self.assertEqual(day['coverage_status'], 'partial')
        absent = report['days'][2]
        self.assertEqual((absent['meter_id'], absent['coverage_status']),
                         ('m3', 'absent'))
        self.assertEqual(absent['energy_wh_exact'], {'numerator': 0, 'denominator': 1})
        self.assertEqual(report['meters'][2]['interval_count'], 0)
        # Outside intervals produce no segments; a half-open window excludes
        # an interval that only starts at the window end.
        self.assertEqual([row['interval_id'] for row in report['segments']],
                         ['a', 'ends-at-window-end'])
        self.assertEqual(report['segments'][1]['start'], start + 3600)
        self.assertEqual(report['segments'][1]['end'], start + 7200)

    def test_two_half_cost_intervals_round_once_per_day(self):
        start = epoch(2025, 1, 15)
        report = run({
            'dataset_id': 'd', 'timezone': 'UTC',
            'window_start': start, 'window_end': start + 7200,
            'meters': ['m1'],
            'tariffs': [{'tariff_id': 'A', 'start_minute': 0,
                         'end_minute': 1440, 'rate_numerator': 1,
                         'rate_denominator': 2}],
            'intervals': [
                {'interval_id': 'a', 'meter_id': 'm1', 'start': start,
                 'end': start + 1800, 'energy_wh': 1},
                {'interval_id': 'b', 'meter_id': 'm1', 'start': start + 3600,
                 'end': start + 5400, 'energy_wh': 1}]})
        day = report['days'][0]
        self.assertEqual(day['cost_exact'], {'numerator': 1, 'denominator': 1})
        self.assertEqual(day['cost_integer'], 1)
        self.assertEqual(report['totals']['cost_integer'], 1)

    def test_energy_is_prorated_by_original_duration(self):
        start = epoch(2025, 1, 15)
        report = run({
            'dataset_id': 'd', 'timezone': 'UTC',
            'window_start': start - 3600, 'window_end': start + 3600,
            'meters': ['m1'], 'tariffs': [FLAT],
            'intervals': [{'interval_id': 'a', 'meter_id': 'm1',
                           'start': start - 7200, 'end': start + 7200,
                           'energy_wh': 4}]})
        self.assertEqual(report['totals']['energy_wh_exact'],
                         {'numerator': 2, 'denominator': 1})
        # The 23:00Z window is split by local midnight into two 1h segments.
        self.assertEqual([row['seconds'] for row in report['segments']],
                         [3600, 3600])
        self.assertEqual([row['local_date'] for row in report['segments']],
                         ['2025-01-14', '2025-01-15'])
        self.assertEqual(report['days'][0]['energy_wh_exact'],
                         {'numerator': 1, 'denominator': 1})
        self.assertEqual(report['days'][1]['energy_wh_exact'],
                         {'numerator': 1, 'denominator': 1})


class StructuringTest(unittest.TestCase):

    def test_non_minute_endpoints_and_midnight_split(self):
        window_start = epoch(2025, 3, 9, 23)
        tariffs = [
            {'tariff_id': 'P', 'start_minute': 0, 'end_minute': 30,
             'rate_numerator': 3, 'rate_denominator': 2},
            {'tariff_id': 'Q', 'start_minute': 30, 'end_minute': 1440,
             'rate_numerator': 1, 'rate_denominator': 1}]
        report = run(one_day_zone(
            'Europe/Berlin', window_start, window_start + 7200,
            [{'interval_id': 'x', 'meter_id': 'm1',
              'start': window_start + 1000, 'end': window_start + 3000,
              'energy_wh': 7}], tariffs))
        segments = report['segments']
        self.assertEqual([row['start'] for row in segments],
                         [window_start + 1000, window_start + 1800])
        self.assertEqual([row['local_date'] for row in segments],
                         ['2025-03-10', '2025-03-10'])
        self.assertEqual([row['tariff_id'] for row in segments], ['P', 'Q'])
        self.assertEqual([row['seconds'] for row in segments], [800, 1200])
        self.assertEqual(segments[0]['energy_wh_exact'],
                         {'numerator': 14, 'denominator': 5})
        self.assertEqual(segments[1]['energy_wh_exact'],
                         {'numerator': 21, 'denominator': 5})
        self.assertEqual(segments[0]['cost_exact'],
                         {'numerator': 21, 'denominator': 5})
        self.assertEqual(segments[1]['cost_exact'],
                         {'numerator': 21, 'denominator': 5})
        # 2025-03-09 23:00Z is already local 2025-03-10 00:00 (+01:00), so the
        # window holds a single local day of 7200 seconds.
        self.assertEqual([row['local_date'] for row in report['days']],
                         ['2025-03-10'])
        self.assertEqual(report['days'][0]['expected_seconds'], 7200)

    def test_adjacent_segments_merge_but_intervals_never_do(self):
        start = epoch(2025, 1, 15)
        spec = {
            'dataset_id': 'd', 'timezone': 'UTC',
            'window_start': start, 'window_end': start + 3600,
            'meters': ['m1'], 'tariffs': [FLAT],
            'intervals': [
                {'interval_id': 'a', 'meter_id': 'm1', 'start': start,
                 'end': start + 900, 'energy_wh': 2},
                {'interval_id': 'b', 'meter_id': 'm1', 'start': start + 900,
                 'end': start + 1800, 'energy_wh': 2}]}
        report = run(spec)
        self.assertEqual(len(report['segments']), 2)
        self.assertEqual([row['interval_id'] for row in report['segments']],
                         ['a', 'b'])

    def test_order_independence_and_byte_identity(self):
        first = sample()
        second = copy.deepcopy(first)
        for key in ('meters', 'tariffs', 'intervals'):
            second[key] = list(reversed(second[key]))
        second['intervals'] = [dict(row, energy_wh=row['energy_wh'])
                               for row in second['intervals']]
        a = run(first, 'a' * 64)
        b = run(second, 'a' * 64)
        for key in ('segments', 'days', 'meters', 'totals'):
            self.assertEqual(a[key], b[key])
        # Byte identity of the published file for the same source bytes.
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            target = folder / 'report.json'
            for payload in (sample(), sample()):
                write(folder / 'input.json', payload)
                result = call('meter-tou', '--source', folder / 'input.json',
                              '--output', target)
                self.assertEqual(result.returncode, 0, result.stderr)
                data = target.read_bytes()
                self.assertIn(b'2025-11-02', data)
            self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(),
                             hashlib.sha256(target.read_bytes()).hexdigest())

    def test_corrupt_result_is_rejected_before_publication(self):
        spec = meter_tou.validate_input(sample())
        report = meter_tou.build_report(spec, 'a' * 64)
        meter_tou.validate_result(spec, report)
        cases = []
        altered = copy.deepcopy(report)
        altered['days'][0]['covered_seconds'] += 1
        cases.append(altered)
        altered = copy.deepcopy(report)
        altered['segments'][0]['seconds'] += 1
        cases.append(altered)
        altered = copy.deepcopy(report)
        altered['totals']['cost_exact'] = {'numerator': 1, 'denominator': 2}
        cases.append(altered)
        altered = copy.deepcopy(report)
        altered['segments'].reverse()
        cases.append(altered)
        for altered in cases:
            with self.assertRaises(ValueError):
                meter_tou.validate_result(spec, altered)

    def test_day_and_tariff_order_is_stable(self):
        report = run(sample())
        keys = [(row['meter_id'], row['local_date']) for row in report['days']]
        self.assertEqual(keys, sorted(keys))
        tariff_ids = [row['tariff_id'] for row in report['tariffs']]
        self.assertEqual(tariff_ids, sorted(tariff_ids))
        for row in report['days']:
            self.assertEqual([item['tariff_id'] for item in row['tariffs']],
                             sorted(tariff_ids))
        self.assertEqual([row['meter_id'] for row in report['meters']],
                         sorted(row['meter_id'] for row in report['meters']))


class ValidationTest(unittest.TestCase):

    def base(self):
        start = epoch(2025, 1, 15)
        return {'dataset_id': 'd', 'timezone': 'UTC',
                'window_start': start, 'window_end': start + 3600,
                'meters': ['m1'], 'tariffs': [FLAT], 'intervals': []}

    def rejects(self, spec, message):
        with self.assertRaises(ValueError) as caught:
            meter_tou.validate_input(spec)
        self.assertIn(message, str(caught.exception))

    def test_timezone_must_be_known_listed_zone(self):
        spec = self.base()
        spec['timezone'] = 'Asia/Tokyo'
        self.rejects(spec, 'unsupported timezone')
        spec['timezone'] = 'utc'
        self.rejects(spec, 'unsupported timezone')
        spec['timezone'] = None
        self.rejects(spec, 'unsupported timezone')
        # The listed zone still loads from the system database.
        spec['timezone'] = 'UTC'
        meter_tou.validate_input(spec)

    def test_window_bounds(self):
        spec = self.base()
        spec['window_start'] = meter_tou.MIN_TIMESTAMP - 1
        self.rejects(spec, 'window_start must be within')
        spec = self.base()
        spec['window_end'] = spec['window_start'] + 31 * 86400 + 1
        self.rejects(spec, 'must not exceed')
        spec = self.base()
        spec['window_end'] = spec['window_start']
        self.rejects(spec, 'must be before')
        spec = self.base()
        spec['window_end'] = True
        self.rejects(spec, 'booleans are rejected')

    def test_structure_rejections(self):
        spec = self.base()
        spec['extra'] = 1
        self.rejects(spec, 'unknown field')
        spec = self.base()
        del spec['meters']
        self.rejects(spec, 'missing field')
        spec = self.base()
        spec['meters'] = ['m1', 'm1']
        self.rejects(spec, 'duplicate meter_id')
        spec = self.base()
        spec['tariffs'] = [dict(FLAT, end_minute=1000)]
        self.rejects(spec, 'must cover every local minute')
        spec = self.base()
        spec['tariffs'] = [FLAT, dict(FLAT, tariff_id='T2',
                                      start_minute=0, end_minute=10)]
        self.rejects(spec, 'must cover every local minute')
        spec = self.base()
        spec['tariffs'] = [dict(FLAT, start_minute=10, end_minute=10)]
        self.rejects(spec, 'start_minute must be before end_minute')
        spec = self.base()
        spec['tariffs'] = [dict(FLAT, rate_denominator=0)]
        self.rejects(spec, 'rate_denominator must be within')
        spec = self.base()
        spec['tariffs'] = [dict(FLAT, rate_numerator=True)]
        self.rejects(spec, 'booleans are rejected')

    def test_interval_rejections_include_out_of_window_rows(self):
        spec = self.base()
        start = spec['window_start']
        spec['intervals'] = [
            {'interval_id': 'a', 'meter_id': 'zz',
             'start': start, 'end': start + 10, 'energy_wh': 1}]
        self.rejects(spec, 'unknown meter_id')
        spec = self.base()
        spec['intervals'] = [
            {'interval_id': 'a', 'meter_id': 'm1',
             'start': start, 'end': start + 10, 'energy_wh': 1},
            {'interval_id': 'b', 'meter_id': 'm1',
             'start': start + 5, 'end': start + 20, 'energy_wh': 1}]
        self.rejects(spec, 'overlap')
        spec = self.base()
        spec['intervals'] = [
            {'interval_id': 'a', 'meter_id': 'm1',
             'start': start, 'end': start + 10, 'energy_wh': 1},
            {'interval_id': 'a', 'meter_id': 'm1',
             'start': start + 10, 'end': start + 20, 'energy_wh': 1}]
        self.rejects(spec, 'duplicate interval_id')
        spec = self.base()
        spec['intervals'] = [
            {'interval_id': 'a', 'meter_id': 'm1',
             'start': start - 100, 'end': start - 90, 'energy_wh': 1},
            {'interval_id': 'b', 'meter_id': 'm1',
             'start': start - 95, 'end': start - 80, 'energy_wh': 1}]
        self.rejects(spec, 'overlap')
        spec = self.base()
        spec['intervals'] = [
            {'interval_id': 'a', 'meter_id': 'm1',
             'start': start, 'end': start + 10, 'energy_wh': 1},
            {'interval_id': 'b', 'meter_id': 'm1',
             'start': start + 11, 'end': start + 21, 'energy_wh': -1}]
        self.rejects(spec, 'energy_wh must be within')
        spec = self.base()
        spec['intervals'] = [{'interval_id': 'a', 'meter_id': 'm1',
                              'start': start, 'end': start + 10,
                              'energy_wh': True}]
        self.rejects(spec, 'booleans are rejected')
        spec = self.base()
        spec['intervals'] = [{'interval_id': 'a', 'meter_id': 'm1',
                              'start': start, 'end': start,
                              'energy_wh': 1}]
        self.rejects(spec, 'start must be before end')
        spec = self.base()
        spec['intervals'] = [{'interval_id': 'a', 'meter_id': 'm1',
                              'start': start, 'end': start + 10,
                              'energy_wh': 1.5}]
        self.rejects(spec, 'energy_wh must be an integer')

    def test_adjacent_and_different_meter_overlaps_within_window(self):
        spec = self.base()
        start = spec['window_start']
        spec['meters'] = ['m1', 'm2']
        spec['intervals'] = [
            {'interval_id': 'a', 'meter_id': 'm1',
             'start': start, 'end': start + 10, 'energy_wh': 1},
            {'interval_id': 'b', 'meter_id': 'm1',
             'start': start + 10, 'end': start + 20, 'energy_wh': 1},
            {'interval_id': 'c', 'meter_id': 'm2',
             'start': start + 5, 'end': start + 15, 'energy_wh': 1}]
        meter_tou.validate_input(spec)

    def test_duplicate_json_keys_rejected_by_reader(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / 'dup.json'
            path.write_text('{"dataset_id":"d","dataset_id":"d"}',
                            encoding='utf-8')
            with self.assertRaises(ValueError):
                read(path)

    def test_unknown_zone_and_missing_zone_data(self):
        import zoneinfo
        spec = self.base()
        spec['timezone'] = 'Nowhere/Land'
        self.rejects(spec, 'unsupported timezone')
        with mock.patch.object(meter_tou, 'ZoneInfo',
                               side_effect=zoneinfo.ZoneInfoNotFoundError):
            with self.assertRaises(ValueError) as caught:
                meter_tou.validate_input(self.base())
            self.assertIn('timezone data unavailable', str(caught.exception))


class PublicationTest(unittest.TestCase):

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.source = self.folder / 'input.json'
        self.output = self.folder / 'report.json'
        write(self.source, sample())

    def tearDown(self):
        shutil.rmtree(self.folder)

    def test_success_and_atomic_rerun(self):
        result = call('meter-tou', '--source', self.source,
                      '--output', self.output)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary['command'], 'meter-tou')
        self.assertEqual(summary['dataset_id'], 'meter-demo')
        report = json.loads(self.output.read_text(encoding='utf-8'))
        self.assertEqual(report['source_sha256'],
                         hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.assertEqual(report['schema_version'], 1)
        first = self.output.read_bytes()
        call('meter-tou', '--source', self.source, '--output', self.output)
        self.assertEqual(first, self.output.read_bytes())

    def test_refuses_same_path_and_symlink(self):
        self.assertEqual(call('meter-tou', '--source', self.source,
                              '--output', self.source).returncode, 2)
        link = self.folder / 'link.json'
        link.symlink_to(self.output)
        self.assertEqual(call('meter-tou', '--source', self.source,
                              '--output', link).returncode, 2)

    def test_failure_preserves_old_report(self):
        call('meter-tou', '--source', self.source, '--output', self.output)
        good = self.output.read_bytes()
        broken = sample()
        broken['timezone'] = 'Mars/Olympus'
        write(self.source, broken)
        result = call('meter-tou', '--source', self.source,
                      '--output', self.output)
        self.assertEqual(result.returncode, 2)
        self.assertIn('unsupported timezone', result.stderr)
        self.assertEqual(self.output.read_bytes(), good)
        # A calculation failure must also leave the previous report untouched.
        broken = sample()
        broken['intervals'][0]['energy_wh'] = 10 ** 13
        write(self.source, broken)
        self.assertEqual(call('meter-tou', '--source', self.source,
                              '--output', self.output).returncode, 2)
        self.assertEqual(self.output.read_bytes(), good)

    def test_source_size_limit(self):
        padded = '{"dataset_id":"' + 'x' * 9_000_000 + '"}'
        self.source.write_text(padded, encoding='utf-8')
        result = call('meter-tou', '--source', self.source,
                      '--output', self.output)
        self.assertEqual(result.returncode, 2)
        self.assertIn('exceeds 8 MB', result.stderr)


class SecondBySecondOracleTest(unittest.TestCase):
    """Independent per-second enumeration agrees on energy and coverage."""

    def test_random_small_windows(self):
        from collections import defaultdict
        from fractions import Fraction
        from zoneinfo import ZoneInfo
        generator = random.Random(20251102)
        zones = ['UTC', 'Asia/Shanghai', 'America/New_York', 'Europe/Berlin']
        anchors = [epoch(2025, 3, 9, 12), epoch(2025, 11, 2, 12),
                   epoch(2001, 1, 1, 12), epoch(2080, 7, 1, 12)]
        for _ in range(40):
            zone = generator.choice(zones)
            tz = ZoneInfo(zone)
            start = generator.choice(anchors) + generator.randrange(-7200, 7200)
            end = start + generator.randrange(1, 400)
            cut = generator.randrange(1, 1440)
            tariffs = [
                {'tariff_id': 'x', 'start_minute': 0, 'end_minute': cut,
                 'rate_numerator': 1, 'rate_denominator': 1},
                {'tariff_id': 'y', 'start_minute': cut, 'end_minute': 1440,
                 'rate_numerator': 2, 'rate_denominator': 3}]
            intervals = []
            counter = 0
            for meter_id in ('a', 'b'):
                points = sorted(generator.sample(range(start - 300, end + 300), 6))
                for lower, upper in zip(points[::2], points[1::2]):
                    if upper > lower:
                        intervals.append({
                            'interval_id': 'i%d' % counter, 'meter_id': meter_id,
                            'start': lower, 'end': upper,
                            'energy_wh': generator.randrange(0, 100)})
                        counter += 1
            spec = {'dataset_id': 't', 'timezone': zone,
                    'window_start': start, 'window_end': end,
                    'meters': ['a', 'b'], 'tariffs': tariffs,
                    'intervals': intervals}
            report = run(spec)
            per_second = defaultdict(Fraction)
            covered = defaultdict(int)
            minutes = meter_tou.validate_input(spec)['minute_tariff']
            for row in intervals:
                low = max(row['start'], start)
                high = min(row['end'], end)
                step = Fraction(row['energy_wh'],
                                row['end'] - row['start'])
                for second in range(low, high):
                    local = datetime.fromtimestamp(second, tz)
                    tariff_id = minutes[local.hour * 60 + local.minute]['tariff_id']
                    key = (row['meter_id'], local.date().isoformat(), tariff_id)
                    per_second[key] += step
                    covered[(row['meter_id'],
                             local.date().isoformat())] += 1
            reported = defaultdict(Fraction)
            for day in report['days']:
                for item in day['tariffs']:
                    value = Fraction(item['energy_wh_exact']['numerator'],
                                     item['energy_wh_exact']['denominator'])
                    reported[(day['meter_id'], day['local_date'],
                              item['tariff_id'])] += value
                self.assertEqual(day['covered_seconds'],
                                 covered[(day['meter_id'], day['local_date'])],
                                 (zone, day['meter_id'], day['local_date']))
            keys = set(per_second) | set(reported)
            for key in keys:
                self.assertEqual(per_second.get(key, Fraction(0)),
                                 reported.get(key, Fraction(0)), key)


class LegacyEntryTest(unittest.TestCase):
    """Existing entry points keep working alongside the new command."""

    def test_static_schedule_unchanged(self):
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / 'plan.json'
            result = call('--source', ROOT / 'fixtures' / 'input.json',
                          '--output', target)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(target.read_text(encoding='utf-8'))
            self.assertEqual(report['schema_version'], 1)
            self.assertEqual(report['result']['total_principal'], 1000)

    def test_contract_award_unchanged(self):
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / 'awards.json'
            result = call('contract-award', '--source',
                          ROOT / 'fixtures' / 'award' / 'procurement.json',
                          '--output', target)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(target.read_text(encoding='utf-8'))
            self.assertEqual(report['objective']['awarded_total_amount'], 44)

    def test_new_command_does_not_consume_static_arguments(self):
        result = call('--source', ROOT / 'fixtures' / 'input.json',
                      '--output', Path(tempfile.gettempdir()) / 'nope.json',
                      '--timezone', 'UTC')
        self.assertEqual(result.returncode, 2)


class FixtureTest(unittest.TestCase):

    def test_all_fixtures_validate(self):
        for path in sorted(FIXTURES.glob('*.json')):
            report = run(json.loads(path.read_text(encoding='utf-8')))
            self.assertEqual(report['schema_version'], 1)

    def test_sample_report_shape(self):
        report = run(sample())
        self.assertEqual(set(report), {'schema_version', 'dataset_id',
                                       'source_sha256', 'timezone', 'window',
                                       'tariffs', 'totals', 'meters', 'days',
                                       'segments'})
        self.assertEqual(report['window'], {'start': 1762056000,
                                            'end': 1762146000,
                                            'seconds': 90000})
        statuses = {(row['meter_id'], row['local_date']): row['coverage_status']
                    for row in report['days']}
        self.assertEqual(statuses[('north-1', '2025-11-02')], 'complete')
        self.assertEqual(statuses[('south-2', '2025-11-02')], 'partial')
        for day in report['days']:
            self.assertLessEqual(day['covered_seconds'],
                                 day['expected_seconds'])

    def test_sparse_fixture_reports_absent_meter(self):
        report = run(sample('sparse.json'))
        days = {(row['meter_id'], row['local_date']): row
                for row in report['days']}
        self.assertEqual(days[('shed-b', '2025-01-01')]['coverage_status'],
                         'absent')
        self.assertEqual(days[('shed-b', '2025-01-01')]['cost_integer'], 0)
        self.assertEqual(days[('shed-a', '2025-01-01')]['covered_seconds'], 3601)


if __name__ == '__main__':
    unittest.main()
