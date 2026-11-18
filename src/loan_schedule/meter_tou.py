"""Time-of-use meter energy and local-day billing dataset.

Irregular UTC observation intervals are prorated by elapsed seconds, clipped to
the reporting window, and split at local-day boundaries, tariff boundaries and
UTC-offset transitions.  Local days are physical (DST aware), so a complete
local day is not assumed to be 86400 seconds.  Missing observation time is
reported as missing, never as an observed zero-energy fact.

Only the Python standard library and the system IANA time-zone database are
used; the module never accesses the network.
"""
from bisect import bisect_left, bisect_right
from calendar import timegm
from datetime import datetime, timedelta
from fractions import Fraction
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .common import ident

SCHEMA_VERSION = 1
TOP_KEYS = ('dataset_id', 'timezone', 'window_start', 'window_end',
            'meters', 'tariffs', 'intervals')
TARIFF_KEYS = ('tariff_id', 'start_minute', 'end_minute',
               'rate_numerator', 'rate_denominator')
INTERVAL_KEYS = ('interval_id', 'meter_id', 'start', 'end', 'energy_wh')

ALLOWED_ZONES = ('UTC', 'Asia/Shanghai', 'America/New_York', 'Europe/Berlin')
MIN_TIMESTAMP = 946684800           # 2000-01-01T00:00:00Z
MAX_TIMESTAMP = 4102444800          # 2100-01-01T00:00:00Z
MAX_SPAN_SECONDS = 31 * 86400
MAX_METERS = 200
MAX_TARIFFS = 96
MAX_INTERVALS = 20000
MAX_ENERGY_WH = 10 ** 12
MAX_RATE_COMPONENT = 10 ** 6
SECONDS_PER_DAY = 86400


def _object(value, keys, where):
    if not isinstance(value, dict):
        raise ValueError(where + ' must be an object')
    unknown = sorted(set(value) - set(keys))
    if unknown:
        raise ValueError('%s: unknown field %s' % (where, unknown[0]))
    missing = [key for key in keys if key not in value]
    if missing:
        raise ValueError('%s: missing field %s' % (where, missing[0]))
    return value


def _identifier(value, where):
    try:
        return ident(value)
    except ValueError as error:
        raise ValueError('%s: %s' % (where, error)) from None


def _integer(value, minimum, maximum, where):
    if type(value) is not int:
        raise ValueError(where + ' must be an integer (booleans are rejected)')
    if not minimum <= value <= maximum:
        raise ValueError('%s must be within %d..%d' % (where, minimum, maximum))
    return value


def _zone(name):
    """Load a system IANA zone; there is deliberately no UTC fallback."""
    if not isinstance(name, str) or name not in ALLOWED_ZONES:
        raise ValueError('unsupported timezone: %r' % (name,))
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        raise ValueError('timezone data unavailable for %s' % name) from None
    except Exception as error:  # corrupt or unreadable system zone data
        raise ValueError('timezone data unavailable for %s: %s'
                         % (name, error)) from None


def validate_input(data):
    """Validate the strict object and return a normalized, sorted spec."""
    _object(data, TOP_KEYS, 'input')
    dataset_id = _identifier(data['dataset_id'], 'dataset_id')
    timezone_name = data['timezone']
    tz = _zone(timezone_name)

    window_start = _integer(data['window_start'], MIN_TIMESTAMP,
                            MAX_TIMESTAMP, 'window_start')
    window_end = _integer(data['window_end'], MIN_TIMESTAMP,
                          MAX_TIMESTAMP, 'window_end')
    if window_start >= window_end:
        raise ValueError('window_start must be before window_end')
    if window_end - window_start > MAX_SPAN_SECONDS:
        raise ValueError('report window must not exceed %d seconds'
                         % MAX_SPAN_SECONDS)

    raw_meters = data['meters']
    if not isinstance(raw_meters, list) or not 1 <= len(raw_meters) <= MAX_METERS:
        raise ValueError('meters must contain 1..%d identifiers' % MAX_METERS)
    meters = []
    meter_ids = set()
    for index, value in enumerate(raw_meters):
        meter_id = _identifier(value, 'meters[%d]' % index)
        if meter_id in meter_ids:
            raise ValueError('duplicate meter_id: ' + meter_id)
        meter_ids.add(meter_id)
        meters.append(meter_id)
    meters.sort()

    raw_tariffs = data['tariffs']
    if not isinstance(raw_tariffs, list) or not 1 <= len(raw_tariffs) <= MAX_TARIFFS:
        raise ValueError('tariffs must contain 1..%d items' % MAX_TARIFFS)
    tariffs = []
    tariff_ids = set()
    for index, item in enumerate(raw_tariffs):
        where = 'tariffs[%d]' % index
        _object(item, TARIFF_KEYS, where)
        tariff_id = _identifier(item['tariff_id'], where + '.tariff_id')
        if tariff_id in tariff_ids:
            raise ValueError('duplicate tariff_id: ' + tariff_id)
        tariff_ids.add(tariff_id)
        start_minute = _integer(item['start_minute'], 0, 1440,
                                where + '.start_minute')
        end_minute = _integer(item['end_minute'], 0, 1440,
                              where + '.end_minute')
        if start_minute >= end_minute:
            raise ValueError('%s: start_minute must be before end_minute' % where)
        numerator = _integer(item['rate_numerator'], 0, MAX_RATE_COMPONENT,
                             where + '.rate_numerator')
        denominator = _integer(item['rate_denominator'], 1, MAX_RATE_COMPONENT,
                               where + '.rate_denominator')
        tariffs.append({
            'tariff_id': tariff_id,
            'start_minute': start_minute,
            'end_minute': end_minute,
            'rate_numerator': numerator,
            'rate_denominator': denominator,
            'rate': Fraction(numerator, denominator),
        })
    tariffs.sort(key=lambda row: row['start_minute'])
    expected = 0
    for row in tariffs:
        if row['start_minute'] != expected:
            raise ValueError('tariffs must cover every local minute exactly once '
                             'from 0 to 1440')
        expected = row['end_minute']
    if expected != 1440:
        raise ValueError('tariffs must cover every local minute exactly once '
                         'from 0 to 1440')

    minute_tariff = [None] * 1440
    for row in tariffs:
        for minute in range(row['start_minute'], row['end_minute']):
            minute_tariff[minute] = row

    raw_intervals = data['intervals']
    if not isinstance(raw_intervals, list) or len(raw_intervals) > MAX_INTERVALS:
        raise ValueError('intervals must contain 0..%d items' % MAX_INTERVALS)
    intervals = []
    interval_ids = set()
    for index, item in enumerate(raw_intervals):
        where = 'intervals[%d]' % index
        _object(item, INTERVAL_KEYS, where)
        interval_id = _identifier(item['interval_id'], where + '.interval_id')
        if interval_id in interval_ids:
            raise ValueError('duplicate interval_id: ' + interval_id)
        interval_ids.add(interval_id)
        meter_id = _identifier(item['meter_id'], where + '.meter_id')
        if meter_id not in meter_ids:
            raise ValueError('%s: unknown meter_id %s' % (where, meter_id))
        start = _integer(item['start'], MIN_TIMESTAMP, MAX_TIMESTAMP,
                         where + '.start')
        end = _integer(item['end'], MIN_TIMESTAMP, MAX_TIMESTAMP,
                       where + '.end')
        if start >= end:
            raise ValueError('%s: start must be before end' % where)
        if end - start > MAX_SPAN_SECONDS:
            raise ValueError('%s: interval must not exceed %d seconds'
                             % (where, MAX_SPAN_SECONDS))
        energy_wh = _integer(item['energy_wh'], 0, MAX_ENERGY_WH,
                             where + '.energy_wh')
        intervals.append({'interval_id': interval_id, 'meter_id': meter_id,
                          'start': start, 'end': end, 'energy_wh': energy_wh})

    # Every interval is validated before any window filtering, so an interval
    # outside the window cannot hide a contract error.
    intervals.sort(key=lambda row: (row['meter_id'], row['start'],
                                    row['end'], row['interval_id']))
    previous = None
    maximum_end = 0
    for row in intervals:
        if previous is not None and previous['meter_id'] == row['meter_id']:
            if row['start'] < maximum_end:
                raise ValueError('intervals for meter %s overlap in UTC'
                                 % row['meter_id'])
            maximum_end = max(maximum_end, row['end'])
        else:
            maximum_end = row['end']
        previous = row

    return {
        'dataset_id': dataset_id,
        'timezone': timezone_name,
        'tz': tz,
        'window_start': window_start,
        'window_end': window_end,
        'meters': meters,
        'tariffs': tariffs,
        'tariffs_by_id': sorted(tariffs, key=lambda row: row['tariff_id']),
        'minute_tariff': minute_tariff,
        'intervals': intervals,
    }


def _utcoffset(tz, timestamp):
    return int(datetime.fromtimestamp(timestamp, tz).utcoffset().total_seconds())


def _transitions(tz, start, end):
    """Return every UTC second in (start, end) at which the offset changes."""
    step = 3600
    points = []
    moment = start
    offset = _utcoffset(tz, moment)
    while moment < end:
        nxt = min(moment + step, end)
        if _utcoffset(tz, nxt) != offset:
            low, high = moment, nxt
            while low + 1 < high:
                middle = (low + high) // 2
                if _utcoffset(tz, middle) == offset:
                    low = middle
                else:
                    high = middle
            points.append(high)
            moment = high
            offset = _utcoffset(tz, moment)
        else:
            moment = nxt
    return points


def _day_start(tz, day, cache):
    """First UTC second whose local date is ``day`` (DST robust)."""
    if day in cache:
        return cache[day]
    naive = timegm((day.year, day.month, day.day, 0, 0, 0))
    low = naive - 2 * SECONDS_PER_DAY
    high = naive + 2 * SECONDS_PER_DAY

    def local_date(moment):
        return datetime.fromtimestamp(moment, tz).date()

    while local_date(low) >= day:
        low -= SECONDS_PER_DAY
    while local_date(high) < day:
        high += SECONDS_PER_DAY
    while low + 1 < high:
        middle = (low + high) // 2
        if local_date(middle) >= day:
            high = middle
        else:
            low = middle
    cache[day] = high
    return high


def _boundary_lists(tz, window_start, window_end, offsets, tariffs):
    """Pre-compute local wall-clock boundaries mapped through each offset."""
    first = datetime.fromtimestamp(window_start, tz).date()
    last = datetime.fromtimestamp(window_end - 1, tz).date()
    starts = sorted({row['start_minute'] for row in tariffs if row['start_minute']})
    naive_values = set()
    day = first - timedelta(days=2)
    final = last + timedelta(days=2)
    while day <= final:
        midnight = timegm((day.year, day.month, day.day, 0, 0, 0))
        naive_values.add(midnight)
        for minute in starts:
            naive_values.add(midnight + minute * 60)
        day += timedelta(days=1)
    return {offset: sorted(value - offset for value in naive_values)
            for offset in offsets}


def _half_up(value):
    """Round a nonnegative exact fraction half-up to an integer."""
    if value < 0:
        raise ValueError('cannot round a negative cost')
    return (2 * value.numerator + value.denominator) // (2 * value.denominator)


def _fraction(value):
    return {'numerator': value.numerator, 'denominator': value.denominator}


def _segments_for(spec, transitions, boundaries):
    """Build every maximal contiguous segment, independent of input order."""
    tz = spec['tz']
    window_start = spec['window_start']
    window_end = spec['window_end']
    minute_tariff = spec['minute_tariff']
    segments = []
    for row in spec['intervals']:
        clip_start = max(row['start'], window_start)
        clip_end = min(row['end'], window_end)
        if clip_start >= clip_end:
            continue
        duration = row['end'] - row['start']
        wh_per_second = Fraction(row['energy_wh'], duration)
        cuts = [clip_start]
        cuts.extend(point for point in transitions
                    if clip_start < point < clip_end)
        cuts.append(clip_end)
        parts = []
        for lower, upper in zip(cuts, cuts[1:]):
            offset = _utcoffset(tz, lower)
            table = boundaries[offset]
            left = bisect_right(table, lower)
            right = bisect_left(table, upper)
            edges = [lower, *table[left:right], upper]
            for start, end in zip(edges, edges[1:]):
                if end <= start:
                    continue
                local = datetime.fromtimestamp(start, tz)
                tariff = minute_tariff[local.hour * 60 + local.minute]
                if tariff is None:
                    raise ValueError('local minute is not covered by any tariff')
                energy = wh_per_second * (end - start)
                parts.append({
                    'interval_id': row['interval_id'],
                    'meter_id': row['meter_id'],
                    'start': start,
                    'end': end,
                    'local_date': local.date().isoformat(),
                    'utc_offset_seconds': offset,
                    'tariff_id': tariff['tariff_id'],
                    'seconds': end - start,
                    'energy': energy,
                    'cost': energy * tariff['rate'],
                })
        merged = []
        for part in parts:
            if merged:
                previous = merged[-1]
                same_key = (previous['local_date'] == part['local_date'] and
                            previous['utc_offset_seconds'] ==
                            part['utc_offset_seconds'] and
                            previous['tariff_id'] == part['tariff_id'])
                if previous['end'] == part['start'] and same_key:
                    previous['end'] = part['end']
                    previous['seconds'] += part['seconds']
                    previous['energy'] += part['energy']
                    previous['cost'] += part['cost']
                    continue
            merged.append(part)
        segments.extend(merged)
    segments.sort(key=lambda row: (row['meter_id'], row['start'],
                                   row['end'], row['interval_id']))
    return segments


def _day_rows(spec):
    tz = spec['tz']
    first = datetime.fromtimestamp(spec['window_start'], tz).date()
    last = datetime.fromtimestamp(spec['window_end'] - 1, tz).date()
    days = []
    day = first
    while day <= last:
        days.append(day)
        day += timedelta(days=1)
    return days


def _expected_seconds(spec, day, cache):
    start = _day_start(spec['tz'], day, cache)
    end = _day_start(spec['tz'], day + timedelta(days=1), cache)
    return max(0, min(end, spec['window_end']) -
               max(start, spec['window_start']))


def _aggregate(spec, segments):
    """Aggregate exact daily and per-meter facts from validated segments."""
    days = _day_rows(spec)
    cache = {}
    expected = {day: _expected_seconds(spec, day, cache) for day in days}
    day_buckets = {}
    tariff_buckets = {}
    segment_counts = {}
    for meter_id in spec['meters']:
        for day in days:
            key = (meter_id, day.isoformat())
            day_buckets[key] = {'seconds': 0, 'energy': Fraction(0),
                                'cost': Fraction(0)}
            tariff_buckets[key] = {}
            segment_counts[key] = 0
    for segment in segments:
        key = (segment['meter_id'], segment['local_date'])
        bucket = day_buckets[key]
        segment_counts[key] += 1
        bucket['seconds'] += segment['seconds']
        bucket['energy'] += segment['energy']
        bucket['cost'] += segment['cost']
        tariff_buckets[key].setdefault(segment['tariff_id'], [Fraction(0),
                                                              Fraction(0)])
        tariff_buckets[key][segment['tariff_id']][0] += segment['energy']
        tariff_buckets[key][segment['tariff_id']][1] += segment['cost']

    day_rows = []
    meter_totals = {meter_id: {'expected_seconds': 0, 'covered_seconds': 0,
                               'missing_seconds': 0, 'energy': Fraction(0),
                               'cost': Fraction(0), 'cost_integer': 0,
                               'segment_count': 0, 'day_count': 0}
                    for meter_id in spec['meters']}
    for meter_id in spec['meters']:
        for day in days:
            key = (meter_id, day.isoformat())
            bucket = day_buckets[key]
            expected_seconds = expected[day]
            covered = bucket['seconds']
            missing = expected_seconds - covered
            if missing < 0:
                raise ValueError('covered seconds exceed expected seconds for '
                                 '%s on %s' % (meter_id, day.isoformat()))
            if covered == expected_seconds:
                status = 'complete'
            elif covered == 0:
                status = 'absent'
            else:
                status = 'partial'
            integer_cost = _half_up(bucket['cost'])
            tariff_rows = []
            for tariff in spec['tariffs_by_id']:
                values = tariff_buckets[key].get(tariff['tariff_id'],
                                                 [Fraction(0), Fraction(0)])
                tariff_rows.append({'tariff_id': tariff['tariff_id'],
                                    'energy_wh_exact': _fraction(values[0]),
                                    'cost_exact': _fraction(values[1])})
            row = {
                'meter_id': meter_id,
                'local_date': day.isoformat(),
                'expected_seconds': expected_seconds,
                'covered_seconds': covered,
                'missing_seconds': missing,
                'coverage_status': status,
                'energy_wh_exact': _fraction(bucket['energy']),
                'cost_exact': _fraction(bucket['cost']),
                'cost_integer': integer_cost,
                'tariffs': tariff_rows,
            }
            day_rows.append(row)
            total = meter_totals[meter_id]
            total['expected_seconds'] += expected_seconds
            total['covered_seconds'] += covered
            total['missing_seconds'] += missing
            total['energy'] += bucket['energy']
            total['cost'] += bucket['cost']
            total['cost_integer'] += integer_cost
            total['day_count'] += 1
            total['segment_count'] += segment_counts[key]

    meter_rows = []
    interval_counts = {}
    for row in spec['intervals']:
        interval_counts[row['meter_id']] = interval_counts.get(row['meter_id'], 0) + 1
    totals = {'expected_seconds': 0, 'covered_seconds': 0,
              'missing_seconds': 0, 'energy': Fraction(0), 'cost': Fraction(0),
              'cost_integer': 0, 'segment_count': len(segments),
              'day_count': len(day_rows), 'meter_count': len(spec['meters'])}
    for meter_id in spec['meters']:
        total = meter_totals[meter_id]
        meter_rows.append({
            'meter_id': meter_id,
            'interval_count': interval_counts.get(meter_id, 0),
            'day_count': total['day_count'],
            'segment_count': total['segment_count'],
            'expected_seconds': total['expected_seconds'],
            'covered_seconds': total['covered_seconds'],
            'missing_seconds': total['missing_seconds'],
            'energy_wh_exact': _fraction(total['energy']),
            'cost_exact': _fraction(total['cost']),
            'cost_integer': total['cost_integer'],
        })
        for field in ('expected_seconds', 'covered_seconds', 'missing_seconds',
                      'energy', 'cost', 'cost_integer'):
            totals[field] += total[field]
    return day_rows, meter_rows, totals


def build_report(spec, digest):
    """Compute the deterministic report from a validated spec."""
    transitions = _transitions(spec['tz'], spec['window_start'],
                               spec['window_end'])
    offsets = {_utcoffset(spec['tz'], spec['window_start']),
               _utcoffset(spec['tz'], spec['window_end'] - 1)}
    for point in transitions:
        offsets.add(_utcoffset(spec['tz'], point))
    boundaries = _boundary_lists(spec['tz'], spec['window_start'],
                                 spec['window_end'], offsets, spec['tariffs'])
    segments = _segments_for(spec, transitions, boundaries)
    day_rows, meter_rows, totals = _aggregate(spec, segments)
    return {
        'schema_version': SCHEMA_VERSION,
        'dataset_id': spec['dataset_id'],
        'source_sha256': digest,
        'timezone': spec['timezone'],
        'window': {'start': spec['window_start'], 'end': spec['window_end'],
                   'seconds': spec['window_end'] - spec['window_start']},
        'tariffs': [{'tariff_id': row['tariff_id'],
                     'start_minute': row['start_minute'],
                     'end_minute': row['end_minute'],
                     'rate_numerator': row['rate_numerator'],
                     'rate_denominator': row['rate_denominator']}
                    for row in spec['tariffs_by_id']],
        'totals': {'expected_seconds': totals['expected_seconds'],
                   'covered_seconds': totals['covered_seconds'],
                   'missing_seconds': totals['missing_seconds'],
                   'meter_count': totals['meter_count'],
                   'day_count': totals['day_count'],
                   'segment_count': totals['segment_count'],
                   'energy_wh_exact': _fraction(totals['energy']),
                   'cost_exact': _fraction(totals['cost']),
                   'cost_integer': totals['cost_integer']},
        'meters': meter_rows,
        'days': day_rows,
        'segments': [{
            'interval_id': row['interval_id'],
            'meter_id': row['meter_id'],
            'start': row['start'],
            'end': row['end'],
            'local_date': row['local_date'],
            'utc_offset_seconds': row['utc_offset_seconds'],
            'tariff_id': row['tariff_id'],
            'seconds': row['seconds'],
            'energy_wh_exact': _fraction(row['energy']),
            'cost_exact': _fraction(row['cost']),
        } for row in segments],
    }


def _read_fraction(value, where):
    if not isinstance(value, dict) or set(value) != {'numerator', 'denominator'}:
        raise ValueError(where + ' must be a fraction object')
    numerator = value['numerator']
    denominator = value['denominator']
    if type(numerator) is not int or type(denominator) is not int:
        raise ValueError(where + ' must contain integers')
    if denominator <= 0 or numerator < 0:
        raise ValueError(where + ' must be a nonnegative reduced fraction')
    fraction = Fraction(numerator, denominator)
    if fraction.numerator != numerator or fraction.denominator != denominator:
        raise ValueError(where + ' is not reduced')
    return fraction


def validate_result(spec, report):
    """Re-derive the published facts and check every invariant before writing."""
    if report.get('schema_version') != SCHEMA_VERSION:
        raise ValueError('result check: unexpected schema_version')
    if report.get('dataset_id') != spec['dataset_id']:
        raise ValueError('result check: dataset_id mismatch')
    if report.get('timezone') != spec['timezone']:
        raise ValueError('result check: timezone mismatch')
    digest = report.get('source_sha256')
    if not isinstance(digest, str) or len(digest) != 64 or any(
            character not in '0123456789abcdef' for character in digest):
        raise ValueError('result check: source_sha256 must be 64 hex digits')
    if set(report) != {'schema_version', 'dataset_id', 'source_sha256',
                       'timezone', 'window', 'tariffs', 'totals', 'meters',
                       'days', 'segments'}:
        raise ValueError('result check: unexpected top-level fields')
    if report.get('window') != {'start': spec['window_start'],
                                'end': spec['window_end'],
                                'seconds': spec['window_end'] -
                                spec['window_start']}:
        raise ValueError('result check: window mismatch')
    expected_tariffs = [{
        'tariff_id': row['tariff_id'],
        'start_minute': row['start_minute'],
        'end_minute': row['end_minute'],
        'rate_numerator': row['rate_numerator'],
        'rate_denominator': row['rate_denominator'],
    } for row in spec['tariffs_by_id']]
    if report.get('tariffs') != expected_tariffs:
        raise ValueError('result check: tariff configuration differs from input')

    tz = spec['tz']
    minute_tariff = spec['minute_tariff']
    tariff_rate = {row['tariff_id']: row['rate'] for row in spec['tariffs']}
    intervals = {row['interval_id']: row for row in spec['intervals']}
    segments = report.get('segments')
    if not isinstance(segments, list):
        raise ValueError('result check: segments must be an array')

    expected_order = ('meter_id', 'start', 'end', 'interval_id')
    previous_order = None
    energies = {interval_id: Fraction(0) for interval_id in intervals}
    seconds = {interval_id: 0 for interval_id in intervals}
    per_interval = {}
    for index, segment in enumerate(segments):
        where = 'segments[%d]' % index
        if not isinstance(segment, dict):
            raise ValueError(where + ' must be an object')
        required = {'interval_id', 'meter_id', 'start', 'end', 'local_date',
                    'utc_offset_seconds', 'tariff_id', 'seconds',
                    'energy_wh_exact', 'cost_exact'}
        if set(segment) != required:
            raise ValueError(where + ' has unexpected fields')
        interval_id = segment['interval_id']
        if interval_id not in intervals:
            raise ValueError(where + ': unknown interval_id')
        source = intervals[interval_id]
        if segment['meter_id'] != source['meter_id']:
            raise ValueError(where + ': meter_id differs from its interval')
        start = segment['start']
        end = segment['end']
        if type(start) is not int or type(end) is not int:
            raise ValueError(where + ': UTC endpoints must be integers')
        if not (spec['window_start'] <= start < end <= spec['window_end']):
            raise ValueError(where + ': segment is outside the report window')
        if not (source['start'] <= start < end <= source['end']):
            raise ValueError(where + ': segment is outside its source interval')
        if segment['seconds'] != end - start:
            raise ValueError(where + ': seconds must equal end - start')
        order = tuple(segment[name] for name in expected_order)
        if previous_order is not None and order < previous_order:
            raise ValueError('result check: segments are not sorted')
        previous_order = order
        local = datetime.fromtimestamp(start, tz)
        if segment['local_date'] != local.date().isoformat():
            raise ValueError(where + ': local_date does not match its start')
        offset = _utcoffset(tz, start)
        if segment['utc_offset_seconds'] != offset:
            raise ValueError(where + ': utc_offset_seconds differs from ZoneInfo')
        tariff = minute_tariff[local.hour * 60 + local.minute]
        if tariff is None or segment['tariff_id'] != tariff['tariff_id']:
            raise ValueError(where + ': tariff_id does not match local start')
        # A maximal segment holds one local day, offset and tariff throughout,
        # so the moment just before its end must agree with its start.
        final = datetime.fromtimestamp(end - 1, tz)
        if final.date().isoformat() != segment['local_date']:
            raise ValueError(where + ': segment crosses a local-day boundary')
        if _utcoffset(tz, end - 1) != offset:
            raise ValueError(where + ': segment crosses an offset transition')
        final_tariff = minute_tariff[final.hour * 60 + final.minute]
        if final_tariff is None or final_tariff['tariff_id'] != tariff['tariff_id']:
            raise ValueError(where + ': segment crosses a tariff boundary')
        energy = _read_fraction(segment['energy_wh_exact'],
                                where + '.energy_wh_exact')
        cost = _read_fraction(segment['cost_exact'], where + '.cost_exact')
        uniform = Fraction(source['energy_wh'] * (end - start),
                           source['end'] - source['start'])
        if energy != uniform:
            raise ValueError(where + ': energy is not prorated by UTC seconds')
        if cost != energy * tariff_rate[segment['tariff_id']]:
            raise ValueError(where + ': cost is not energy times the tariff rate')
        per_interval.setdefault(interval_id, []).append(segment)
        energies[interval_id] += energy
        seconds[interval_id] += end - start

    # Every original interval's clipped energy must equal its segment sum.
    for interval_id, source in intervals.items():
        clipped = max(0, min(source['end'], spec['window_end']) -
                      max(source['start'], spec['window_start']))
        if seconds[interval_id] != clipped:
            raise ValueError('result check: interval %s segment seconds do not '
                             'equal its clipped seconds' % interval_id)
        expected_energy = Fraction(source['energy_wh'] * clipped,
                                   source['end'] - source['start'])
        if energies[interval_id] != expected_energy:
            raise ValueError('result check: interval %s segment energy does not '
                             'equal its clipped energy' % interval_id)
        rows = sorted(per_interval.get(interval_id, []),
                      key=lambda row: (row['start'], row['end']))
        for before, after in zip(rows, rows[1:]):
            if (before['end'] == after['start'] and
                    before['local_date'] == after['local_date'] and
                    before['utc_offset_seconds'] ==
                    after['utc_offset_seconds'] and
                    before['tariff_id'] == after['tariff_id']):
                raise ValueError('result check: interval %s has adjacent '
                                 'segments that were not merged' % interval_id)

    # Days, tariffs and meter totals must be exact sums of the segments.
    day_rows, meter_rows, totals = _aggregate(spec, [
        {**segment,
         'energy': _read_fraction(segment['energy_wh_exact'], 'segment energy'),
         'cost': _read_fraction(segment['cost_exact'], 'segment cost')}
        for segment in segments])
    if report.get('days') != day_rows:
        raise ValueError('result check: daily summaries differ from segment sums')
    if report.get('meters') != meter_rows:
        raise ValueError('result check: meter totals differ from segment sums')
    expected_totals = {
        'expected_seconds': totals['expected_seconds'],
        'covered_seconds': totals['covered_seconds'],
        'missing_seconds': totals['missing_seconds'],
        'meter_count': totals['meter_count'],
        'day_count': totals['day_count'],
        'segment_count': totals['segment_count'],
        'energy_wh_exact': _fraction(totals['energy']),
        'cost_exact': _fraction(totals['cost']),
        'cost_integer': totals['cost_integer'],
    }
    if report.get('totals') != expected_totals:
        raise ValueError('result check: report totals differ from daily sums')
    if not 0 <= expected_totals['covered_seconds'] <= \
            expected_totals['expected_seconds']:
        raise ValueError('result check: total covered_seconds exceeds expected')
    if expected_totals['cost_integer'] != sum(row['cost_integer']
                                              for row in day_rows):
        raise ValueError('result check: total integer cost is not the day sum')

    # Explicit coverage and tariff completeness checks.
    for row in day_rows:
        if not 0 <= row['covered_seconds'] <= row['expected_seconds']:
            raise ValueError('result check: covered_seconds exceeds '
                             'expected_seconds')
        if row['missing_seconds'] != (row['expected_seconds'] -
                                      row['covered_seconds']):
            raise ValueError('result check: missing_seconds is inconsistent')
        if [tariff['tariff_id'] for tariff in row['tariffs']] != \
                [tariff['tariff_id'] for tariff in spec['tariffs_by_id']]:
            raise ValueError('result check: daily tariff rows are incomplete')
        energy_sum = sum(_read_fraction(row['tariffs'][index]['energy_wh_exact'],
                                        'daily tariff energy')
                         for index in range(len(row['tariffs'])))
        cost_sum = sum(_read_fraction(row['tariffs'][index]['cost_exact'],
                                      'daily tariff cost')
                       for index in range(len(row['tariffs'])))
        if energy_sum != _read_fraction(row['energy_wh_exact'],
                                        'daily energy'):
            raise ValueError('result check: daily tariff energy does not sum')
        if cost_sum != _read_fraction(row['cost_exact'], 'daily cost'):
            raise ValueError('result check: daily tariff cost does not sum')
        if row['cost_integer'] != _half_up(cost_sum):
            raise ValueError('result check: daily integer cost is not half-up')
