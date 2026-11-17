"""Auditable lot-award dataset for public procurement batches.

Award selection is an exact lexicographic optimisation over the qualified bid
graph.  A lot may take part only through a bid whose supplier covers every
certificate the lot requires; each lot is awarded at most once and a supplier
never exceeds its declared capacity.  Among all feasible assignments the
winner maximises the number of awarded lots, then minimises the total awarded
amount, and finally has the lexicographically smallest sorted
``(lot_id, supplier_id, bid_id)`` sequence.  Empty lots are never represented
as zero-priced awards.

The optimisation is a min-cost max-flow on the lot/supplier graph.  Primary
objectives are folded into one arbitrary-precision integer cost: the amount is
scaled by ``2**E`` (more than the largest possible lexicographic offset) and
each qualified bid edge carries an offset that prefers earlier
``(lot_id, supplier_id, bid_id)`` positions.  Because every candidate solution
has the same cardinality when amounts tie, the offset difference between two
solutions is below ``2**E`` and cannot outweigh one minor amount unit.
"""
from heapq import heappop, heappush
from .common import ident

TOP_KEYS = ('procurement_id', 'lots', 'suppliers', 'bids')
LOT_KEYS = ('lot_id', 'required_certificates')
SUPPLIER_KEYS = ('supplier_id', 'capacity', 'certificates')
BID_KEYS = ('bid_id', 'lot_id', 'supplier_id', 'amount')
MAX_LOTS = 200
MAX_SUPPLIERS = 100
MAX_BIDS = 5000
AMOUNT_MAX = 10 ** 12
SCHEMA_VERSION = 1
OBJECTIVE_RULE = ('maximize awarded lots, then minimize total awarded amount, '
                  'then lexicographically smallest sorted '
                  '(lot_id, supplier_id, bid_id) sequence')


def _integer(value, minimum, maximum, field):
    if type(value) is not int:
        raise ValueError('%s must be an integer (booleans are rejected)' % field)
    if not minimum <= value <= maximum:
        raise ValueError('%s must be within %d..%d' % (field, minimum, maximum))
    return value


def _object(value, keys, where):
    if not isinstance(value, dict):
        raise ValueError(where + ' must be an object')
    unknown = sorted(set(value) - set(keys))
    if unknown:
        raise ValueError(where + ': unknown field ' + unknown[0])
    missing = [key for key in keys if key not in value]
    if missing:
        raise ValueError(where + ': missing field ' + missing[0])
    return value


def _identifier(value, where):
    try:
        return ident(value)
    except ValueError as error:
        raise ValueError(where + ': ' + str(error)) from None


def _certificates(value, where):
    if not isinstance(value, list):
        raise ValueError(where + ' must be an array')
    seen = set()
    result = []
    for index, item in enumerate(value):
        certificate = _identifier(item, '%s[%d]' % (where, index))
        if certificate in seen:
            raise ValueError('%s: duplicate certificate %s' % (where, certificate))
        seen.add(certificate)
        result.append(certificate)
    return sorted(result)


def validate_input(data):
    """Validate the strict procurement object and return a normalized spec.

    ``lots``, ``suppliers`` and ``bids`` are sorted by their contract sort keys
    so that every later step, and therefore the published dataset, is
    independent of the input array order.
    """
    _object(data, TOP_KEYS, 'input')
    procurement_id = _identifier(data['procurement_id'], 'procurement_id')

    raw_lots = data['lots']
    if not isinstance(raw_lots, list) or not 1 <= len(raw_lots) <= MAX_LOTS:
        raise ValueError('lots must contain 1..%d items' % MAX_LOTS)
    lots = []
    lot_ids = set()
    for index, item in enumerate(raw_lots):
        where = 'lots[%d]' % index
        _object(item, LOT_KEYS, where)
        lot_id = _identifier(item['lot_id'], where + '.lot_id')
        if lot_id in lot_ids:
            raise ValueError('duplicate lot_id: ' + lot_id)
        lot_ids.add(lot_id)
        required = _certificates(item['required_certificates'],
                                 where + '.required_certificates')
        lots.append({'lot_id': lot_id, 'required_certificates': required})

    raw_suppliers = data['suppliers']
    if not isinstance(raw_suppliers, list) or not 1 <= len(raw_suppliers) <= MAX_SUPPLIERS:
        raise ValueError('suppliers must contain 1..%d items' % MAX_SUPPLIERS)
    suppliers = []
    supplier_ids = set()
    for index, item in enumerate(raw_suppliers):
        where = 'suppliers[%d]' % index
        _object(item, SUPPLIER_KEYS, where)
        supplier_id = _identifier(item['supplier_id'], where + '.supplier_id')
        if supplier_id in supplier_ids:
            raise ValueError('duplicate supplier_id: ' + supplier_id)
        supplier_ids.add(supplier_id)
        capacity = _integer(item['capacity'], 0, len(lots), where + '.capacity')
        certificates = _certificates(item['certificates'], where + '.certificates')
        suppliers.append({'supplier_id': supplier_id, 'capacity': capacity,
                          'certificates': certificates})

    raw_bids = data['bids']
    if not isinstance(raw_bids, list) or len(raw_bids) > MAX_BIDS:
        raise ValueError('bids must contain 0..%d items' % MAX_BIDS)
    bids = []
    bid_ids = set()
    pairs = set()
    for index, item in enumerate(raw_bids):
        where = 'bids[%d]' % index
        _object(item, BID_KEYS, where)
        bid_id = _identifier(item['bid_id'], where + '.bid_id')
        if bid_id in bid_ids:
            raise ValueError('duplicate bid_id: ' + bid_id)
        bid_ids.add(bid_id)
        lot_id = _identifier(item['lot_id'], where + '.lot_id')
        supplier_id = _identifier(item['supplier_id'], where + '.supplier_id')
        amount = _integer(item['amount'], 1, AMOUNT_MAX, where + '.amount')
        if lot_id not in lot_ids:
            raise ValueError('%s: unknown lot_id %s' % (where, lot_id))
        if supplier_id not in supplier_ids:
            raise ValueError('%s: unknown supplier_id %s' % (where, supplier_id))
        if (supplier_id, lot_id) in pairs:
            raise ValueError('%s: duplicate bid for supplier %s and lot %s'
                             % (where, supplier_id, lot_id))
        pairs.add((supplier_id, lot_id))
        bids.append({'bid_id': bid_id, 'lot_id': lot_id,
                     'supplier_id': supplier_id, 'amount': amount})

    lots.sort(key=lambda row: row['lot_id'])
    suppliers.sort(key=lambda row: row['supplier_id'])
    bids.sort(key=lambda row: (row['lot_id'], row['supplier_id'], row['bid_id']))
    return {'procurement_id': procurement_id, 'lots': lots,
            'suppliers': suppliers, 'bids': bids}


def _candidate_rows(spec):
    """Group every input bid by lot with its certificate assessment.

    Unqualified bids are retained so the published dataset can explain why a
    supplier could not compete; they never become award edges.
    """
    certificates = {row['supplier_id']: set(row['certificates'])
                    for row in spec['suppliers']}
    required = {row['lot_id']: set(row['required_certificates'])
                for row in spec['lots']}
    by_lot = {row['lot_id']: [] for row in spec['lots']}
    for bid in spec['bids']:
        missing = sorted(required[bid['lot_id']] - certificates[bid['supplier_id']])
        by_lot[bid['lot_id']].append({'bid': bid,
                                      'missing_certificates': missing,
                                      'certificate_qualified': not missing})
    for rows in by_lot.values():
        rows.sort(key=lambda row: (row['bid']['supplier_id'], row['bid']['bid_id']))
    return by_lot


class _MinCostFlow:
    """Successive shortest paths with Dijkstra potentials (integer costs)."""

    def __init__(self, size):
        self.size = size
        self.graph = [[] for _ in range(size)]
        self.flow = 0
        self.cost = 0

    def add(self, source, target, capacity, cost):
        forward = [target, capacity, cost, len(self.graph[target])]
        backward = [source, 0, -cost, len(self.graph[source])]
        self.graph[source].append(forward)
        self.graph[target].append(backward)
        return forward

    def run(self, source, sink):
        size = self.size
        graph = self.graph
        potential = [0] * size
        while True:
            distance = [None] * size
            previous_node = [-1] * size
            previous_edge = [-1] * size
            distance[source] = 0
            heap = [(0, source)]
            while heap:
                reached, node = heappop(heap)
                if reached != distance[node]:
                    continue
                for index, edge in enumerate(graph[node]):
                    target, capacity, cost, _ = edge
                    if capacity <= 0:
                        continue
                    candidate = reached + cost + potential[node] - potential[target]
                    if distance[target] is None or candidate < distance[target]:
                        distance[target] = candidate
                        previous_node[target] = node
                        previous_edge[target] = index
                        heappush(heap, (candidate, target))
            if distance[sink] is None:
                break
            for node in range(size):
                if distance[node] is not None:
                    potential[node] += distance[node]
            amount = None
            node = sink
            while node != source:
                parent = previous_node[node]
                if parent < 0:
                    raise ValueError('award graph lost its augmenting path')
                edge = graph[parent][previous_edge[node]]
                amount = edge[1] if amount is None else min(amount, edge[1])
                node = parent
            node = sink
            path_cost = 0
            while node != source:
                parent = previous_node[node]
                edge = graph[parent][previous_edge[node]]
                path_cost += edge[2]
                edge[1] -= amount
                graph[node][edge[3]][1] += amount
                node = parent
            self.flow += amount
            self.cost += amount * path_cost
        return self.flow, self.cost


def optimize(spec):
    """Return the exact lexicographic optimum for the qualified bid graph."""
    candidates = _candidate_rows(spec)
    edges = []
    for lot in spec['lots']:
        for row in candidates[lot['lot_id']]:
            if row['certificate_qualified']:
                edges.append(row['bid'])
    count = len(edges)

    supplier_node = {row['supplier_id']: index + 1
                     for index, row in enumerate(spec['suppliers'])}
    lot_offset = 1 + len(spec['suppliers'])
    lot_node = {row['lot_id']: lot_offset + index
                for index, row in enumerate(spec['lots'])}
    sink = lot_offset + len(spec['lots'])
    engine = _MinCostFlow(sink + 1)
    for row in spec['suppliers']:
        engine.add(0, supplier_node[row['supplier_id']], row['capacity'], 0)
    for row in spec['lots']:
        engine.add(lot_node[row['lot_id']], sink, 1, 0)

    scale = 1 << count
    references = []
    for position, bid in enumerate(edges):
        weight = (1 << (count - 1 - position)) if count else 0
        cost = bid['amount'] * scale + (scale - 1 - weight)
        references.append((bid, engine.add(supplier_node[bid['supplier_id']],
                                           lot_node[bid['lot_id']], 1, cost)))
    flow, _ = engine.run(0, sink)

    awards = {}
    for bid, edge in references:
        if edge[1] == 0:  # unit capacity fully consumed by the optimum flow
            if bid['lot_id'] in awards:
                raise ValueError('award optimisation assigned a lot twice')
            awards[bid['lot_id']] = bid
    if len(awards) != flow:
        raise ValueError('award optimisation produced %d awards for flow %d'
                         % (len(awards), flow))
    return {'awards': awards, 'awarded_lot_count': len(awards),
            'awarded_total_amount': sum(row['amount'] for row in awards.values()),
            'qualified_bid_count': count}


def _unawarded_reason(rows):
    if not rows:
        return 'no_bid'
    if not any(row['certificate_qualified'] for row in rows):
        return 'no_qualified_bid'
    return 'capacity_competition'


def build_report(spec, result, digest):
    """Assemble the deterministic, fully explainable awards dataset."""
    candidates = _candidate_rows(spec)
    awards = result['awards']

    lots = []
    for lot in spec['lots']:
        lot_id = lot['lot_id']
        selected = awards.get(lot_id)
        rows = candidates[lot_id]
        candidates_out = []
        for row in rows:
            bid = row['bid']
            candidates_out.append({
                'bid_id': bid['bid_id'],
                'supplier_id': bid['supplier_id'],
                'amount': bid['amount'],
                'certificate_qualified': row['certificate_qualified'],
                'missing_certificates': list(row['missing_certificates']),
                'selected': selected is not None and bid['bid_id'] == selected['bid_id'],
            })
        lots.append({
            'lot_id': lot_id,
            'required_certificates': list(lot['required_certificates']),
            'awarded': selected is not None,
            'awarded_bid_id': selected['bid_id'] if selected else None,
            'awarded_supplier_id': selected['supplier_id'] if selected else None,
            'awarded_amount': selected['amount'] if selected else None,
            'unawarded_reason': None if selected else _unawarded_reason(rows),
            'candidates': candidates_out,
        })

    suppliers = []
    for supplier in spec['suppliers']:
        supplier_id = supplier['supplier_id']
        awarded_lots = sorted(lot_id for lot_id, bid in awards.items()
                              if bid['supplier_id'] == supplier_id)
        awarded_amount = sum(awards[lot_id]['amount'] for lot_id in awarded_lots)
        suppliers.append({
            'supplier_id': supplier_id,
            'capacity': supplier['capacity'],
            'awarded_lot_count': len(awarded_lots),
            'awarded_lot_ids': awarded_lots,
            'awarded_total_amount': awarded_amount,
            'remaining_capacity': supplier['capacity'] - len(awarded_lots),
        })

    awarded_count = result['awarded_lot_count']
    return {
        'schema_version': SCHEMA_VERSION,
        'procurement_id': spec['procurement_id'],
        'source_sha256': digest,
        'objective': {
            'rule': OBJECTIVE_RULE,
            'lot_count': len(spec['lots']),
            'supplier_count': len(spec['suppliers']),
            'bid_count': len(spec['bids']),
            'qualified_bid_count': result['qualified_bid_count'],
            'awarded_lot_count': awarded_count,
            'awarded_total_amount': result['awarded_total_amount'],
            'unawarded_lot_count': len(spec['lots']) - awarded_count,
        },
        'lots': lots,
        'suppliers': suppliers,
    }


def validate_result(spec, report):
    """Independently re-derive and check every published row.

    Runs before publication.  Any inconsistency raises ``ValueError`` so the
    previous output file survives untouched.
    """
    if report.get('schema_version') != SCHEMA_VERSION:
        raise ValueError('result check: unexpected schema_version')
    if report.get('procurement_id') != spec['procurement_id']:
        raise ValueError('result check: procurement_id mismatch')
    digest = report.get('source_sha256')
    if not isinstance(digest, str) or len(digest) != 64 or any(
            character not in '0123456789abcdef' for character in digest):
        raise ValueError('result check: source_sha256 must be 64 hex digits')

    candidates = _candidate_rows(spec)
    reports = report.get('lots')
    if not isinstance(reports, list) or len(reports) != len(spec['lots']):
        raise ValueError('result check: lot rows do not match the input')
    supplier_counts = {}
    supplier_amounts = {}
    total_amount = 0
    awarded_count = 0
    expected_suppliers = {row['supplier_id']: row for row in spec['suppliers']}
    for lot, row in zip(spec['lots'], reports):
        lot_id = lot['lot_id']
        if row.get('lot_id') != lot_id:
            raise ValueError('result check: lot rows are not sorted by lot_id')
        if row.get('required_certificates') != lot['required_certificates']:
            raise ValueError('result check: lot %s required certificates differ' % lot_id)
        expected = candidates[lot_id]
        actual = row.get('candidates')
        if not isinstance(actual, list) or len(actual) != len(expected):
            raise ValueError('result check: lot %s candidate rows differ' % lot_id)
        selected = []
        for candidate, source in zip(actual, expected):
            bid = source['bid']
            if candidate.get('bid_id') != bid['bid_id'] or \
                    candidate.get('supplier_id') != bid['supplier_id'] or \
                    candidate.get('amount') != bid['amount']:
                raise ValueError('result check: lot %s candidate %s differs from its bid'
                                 % (lot_id, bid['bid_id']))
            if candidate.get('certificate_qualified') != source['certificate_qualified'] or \
                    candidate.get('missing_certificates') != source['missing_certificates']:
                raise ValueError('result check: lot %s candidate %s certificate status differs'
                                 % (lot_id, bid['bid_id']))
            if candidate.get('selected'):
                if not source['certificate_qualified']:
                    raise ValueError('result check: lot %s selected an unqualified bid' % lot_id)
                selected.append(bid)
        if len(selected) > 1:
            raise ValueError('result check: lot %s has more than one selected bid' % lot_id)
        if row.get('awarded') is True:
            if len(selected) != 1:
                raise ValueError('result check: awarded lot %s lacks exactly one selected bid'
                                 % lot_id)
            bid = selected[0]
            if row.get('awarded_bid_id') != bid['bid_id'] or \
                    row.get('awarded_supplier_id') != bid['supplier_id'] or \
                    row.get('awarded_amount') != bid['amount']:
                raise ValueError('result check: lot %s award summary differs from its bid'
                                 % lot_id)
            if row.get('unawarded_reason') is not None:
                raise ValueError('result check: awarded lot %s has an unawarded reason' % lot_id)
            awarded_count += 1
            total_amount += bid['amount']
            supplier_id = bid['supplier_id']
            supplier_counts[supplier_id] = supplier_counts.get(supplier_id, 0) + 1
            supplier_amounts[supplier_id] = supplier_amounts.get(supplier_id, 0) + bid['amount']
        elif row.get('awarded') is False:
            if selected:
                raise ValueError('result check: unawarded lot %s has a selected bid' % lot_id)
            if row.get('awarded_bid_id') is not None or \
                    row.get('awarded_supplier_id') is not None or \
                    row.get('awarded_amount') is not None:
                raise ValueError('result check: unawarded lot %s carries an award' % lot_id)
            if row.get('unawarded_reason') != _unawarded_reason(expected):
                raise ValueError('result check: lot %s unawarded reason differs' % lot_id)
        else:
            raise ValueError('result check: lot %s awarded flag must be boolean' % lot_id)

    supplier_rows = report.get('suppliers')
    if not isinstance(supplier_rows, list) or len(supplier_rows) != len(spec['suppliers']):
        raise ValueError('result check: supplier rows do not match the input')
    awarded_lots = {}
    for lot, row in zip(spec['lots'], reports):
        if row.get('awarded') is True:
            awarded_lots.setdefault(row['awarded_supplier_id'], []).append(lot['lot_id'])
    for supplier_id in awarded_lots:
        awarded_lots[supplier_id].sort()
    for source, row in zip(spec['suppliers'], supplier_rows):
        supplier_id = source['supplier_id']
        if row.get('supplier_id') != supplier_id:
            raise ValueError('result check: supplier rows are not sorted by supplier_id')
        if row.get('capacity') != expected_suppliers[supplier_id]['capacity']:
            raise ValueError('result check: supplier %s capacity differs' % supplier_id)
        count = supplier_counts.get(supplier_id, 0)
        if row.get('awarded_lot_count') != count:
            raise ValueError('result check: supplier %s awarded count differs' % supplier_id)
        if count > source['capacity']:
            raise ValueError('result check: supplier %s exceeds capacity %d'
                             % (supplier_id, source['capacity']))
        if row.get('remaining_capacity') != source['capacity'] - count:
            raise ValueError('result check: supplier %s remaining capacity differs' % supplier_id)
        if row.get('awarded_total_amount') != supplier_amounts.get(supplier_id, 0):
            raise ValueError('result check: supplier %s awarded amount differs' % supplier_id)
        if row.get('awarded_lot_ids') != awarded_lots.get(supplier_id, []):
            raise ValueError('result check: supplier %s awarded lots differ' % supplier_id)

    objective = report.get('objective')
    if not isinstance(objective, dict):
        raise ValueError('result check: objective summary missing')
    if objective.get('awarded_lot_count') != awarded_count or \
            objective.get('awarded_total_amount') != total_amount:
        raise ValueError('result check: objective summary disagrees with the lot rows')
    if objective.get('unawarded_lot_count') != len(spec['lots']) - awarded_count:
        raise ValueError('result check: unawarded count differs')
    if objective.get('lot_count') != len(spec['lots']) or \
            objective.get('supplier_count') != len(spec['suppliers']) or \
            objective.get('bid_count') != len(spec['bids']):
        raise ValueError('result check: objective input counts differ')
    qualified = sum(1 for rows in candidates.values()
                    for row in rows if row['certificate_qualified'])
    if objective.get('qualified_bid_count') != qualified:
        raise ValueError('result check: qualified bid count differs')
