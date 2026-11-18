"""Relational-table candidate-key profiling (``table-keys`` product).

Given a strict JSON relation ``{dataset_id, columns, rows}`` the product finds,
over the full column-subset lattice, every inclusion-minimal unique column
group (candidate key) and every inclusion-maximal non-unique column group,
each with the lexicographically smallest pair of source rows that still
collide on it.

Type-aware equality
--------------------
Cells are compared by type *and* value.  To make this explicit every accepted
cell is tagged with a type marker, so ``True``, ``1`` and ``"1"`` never compare
equal, while two ``null`` values do and integer ``0`` equals ``-0``.  Strings
keep their raw Unicode code points (no case folding, trimming or other
normalisation) and the string ``"null"`` is distinct from JSON ``null``.

Why the search cannot miss a boundary
--------------------------------------
Columns that carry identical value vectors are interchangeable, so they are
folded into equivalence classes first (the answer over the original columns is
reconstructed exactly from the class answer).  For the class lattice one of two
exhaustive, non-sampling searches is used:

* when a triangular agreement-mask table fits the memory budget, *every* row
  pair contributes its exact agreement mask; a column group is non-unique iff
  it is contained in some pair's agreement mask, so the inclusion-maximal
  agreement masks are precisely the maximal non-keys;
* otherwise an incremental partition DFS visits column sets and keeps the exact
  partition of rows induced by each set, recording a set only when no remaining
  column can still split a colliding block.  Each distinct column set is
  processed at most once, and every maximal agreement set is a leaf, so no
  boundary can be omitted.

The minimal keys follow from the duality that the minimal hitting sets of the
complements of the maximal non-keys are exactly the inclusion-minimal keys.
The whole result is independently re-checked before publication.

Only the Python standard library is used; nothing accesses the network.
"""
from array import array

from .common import ident

SCHEMA_VERSION = 1
TOP_KEYS = ('dataset_id', 'columns', 'rows')
ROW_KEYS = ('row_id', 'values')
MAX_COLUMNS = 24
MAX_ROWS = 20000
INTEGER_MIN = -10 ** 12
INTEGER_MAX = 10 ** 12
MAX_STRING_LENGTH = 256
# Four-byte unsigned masks limit the triangular agreement table; keep the whole
# process comfortably inside the 512 MiB acceptance environment.
STORE_BYTE_BUDGET = 200_000_000

# Type tags: equality requires the same tag, so bool/int/str/null never mix.
_TAG_NULL = 0
_TAG_BOOL = 1
_TAG_INT = 2
_TAG_STR = 3


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


def _cell(value, where):
    """Validate one cell and return its type-tagged comparable form."""
    if value is None:
        return (_TAG_NULL, 0)
    if isinstance(value, bool):
        return (_TAG_BOOL, 1 if value else 0)
    if isinstance(value, int):  # bool already handled; floats rejected
        if not INTEGER_MIN <= value <= INTEGER_MAX:
            raise ValueError('%s: integer outside %d..%d'
                             % (where, INTEGER_MIN, INTEGER_MAX))
        return (_TAG_INT, value)
    if isinstance(value, str):
        if len(value) > MAX_STRING_LENGTH:
            raise ValueError('%s: string longer than %d characters'
                             % (where, MAX_STRING_LENGTH))
        return (_TAG_STR, value)
    raise ValueError(
        '%s: cell must be null, boolean, integer or string (floats, lists and '
        'objects are rejected)' % where)


def validate_input(data):
    """Validate the strict relation and return a normalized, ordered spec.

    Columns are returned in canonical Unicode code-point order and rows in
    row_id order; each row's values are reordered to match the canonical
    columns.  Row order and input column order therefore never change the
    business result.
    """
    _object(data, TOP_KEYS, 'input')
    try:
        dataset_id = ident(data['dataset_id'])
    except ValueError as error:
        raise ValueError('dataset_id: ' + str(error)) from None

    raw_columns = data['columns']
    if not isinstance(raw_columns, list) or not 1 <= len(raw_columns) <= MAX_COLUMNS:
        raise ValueError('columns must contain 1..%d identifiers' % MAX_COLUMNS)
    column_order = []
    column_ids = set()
    for index, value in enumerate(raw_columns):
        where = 'columns[%d]' % index
        name = ident(value)
        if name in column_ids:
            raise ValueError('duplicate column name: ' + name)
        column_ids.add(name)
        column_order.append((name, index))
    column_order.sort(key=lambda pair: pair[0])
    canonical_names = [name for name, _ in column_order]
    canonical_permutation = [original for _, original in column_order]

    raw_rows = data['rows']
    if not isinstance(raw_rows, list) or len(raw_rows) > MAX_ROWS:
        raise ValueError('rows must contain 0..%d items' % MAX_ROWS)
    row_ids = set()
    normalized_rows = []
    expected_width = len(canonical_names)
    for index, item in enumerate(raw_rows):
        where = 'rows[%d]' % index
        _object(item, ROW_KEYS, where)
        try:
            row_id = ident(item['row_id'])
        except ValueError as error:
            raise ValueError('%s.row_id: %s' % (where, error)) from None
        if row_id in row_ids:
            raise ValueError('duplicate row_id: ' + row_id)
        row_ids.add(row_id)
        values = item['values']
        if not isinstance(values, list) or len(values) != expected_width:
            raise ValueError('%s.values must be an array of exactly %d items'
                             % (where, expected_width))
        tagged = [_cell(values[position], '%s.values[%d]' % (where, position))
                  for position in range(expected_width)]
        # reorder to canonical column order
        canonical_cells = [tagged[position] for position in canonical_permutation]
        normalized_rows.append((row_id, canonical_cells))

    normalized_rows.sort(key=lambda pair: pair[0])
    return {
        'dataset_id': dataset_id,
        'columns': canonical_names,
        'rows': normalized_rows,
    }


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #

def _build_classes(columns):
    """Group canonical columns with identical tagged value vectors."""
    signature_to_members = {}
    for j, vector in enumerate(columns):
        signature_to_members.setdefault(tuple(vector), []).append(j)
    signatures, members = [], []
    for signature, group in signature_to_members.items():
        signatures.append(signature)
        members.append(group)
    return signatures, members


def _code_classes(signatures, row_count):
    """Dictionary-encode every class column into small integer codes."""
    coded = []
    max_distinct = 1
    for signature in signatures:
        mapping = {}
        out = [0] * row_count
        next_code = 0
        for i, value in enumerate(signature):
            code = mapping.get(value)
            if code is None:
                code = next_code
                next_code += 1
                mapping[value] = code
            out[i] = code
        coded.append(out)
        max_distinct = max(max_distinct, next_code)
    return coded, max_distinct


def _full_rows_unique(coded, class_count, row_count):
    seen = set()
    for r in range(row_count):
        key = tuple(coded[c][r] for c in range(class_count))
        if key in seen:
            return False
        seen.add(key)
    return True


def _maximal_masks(masks):
    maximal = []
    for mask in sorted(masks, key=lambda x: (-x.bit_count(), x)):
        if all(not (mask != other and (mask & other) == mask) for other in maximal):
            maximal.append(mask)
    return maximal


def _pair_path(coded, class_count, row_count):
    """Enumerate every row pair's exact agreement mask (memory-bounded)."""
    total_pairs = row_count * (row_count - 1) // 2
    store = array('I', [0]) * total_pairs
    for c in range(class_count):
        column = coded[c]
        bit = 1 << c
        groups = {}
        for r in range(row_count):
            groups.setdefault(column[r], array('I')).append(r)
        for group in groups.values():
            if len(group) < 2:
                continue
            for pi in range(len(group)):
                a = group[pi]
                base = total_pairs - (row_count - a) * (row_count - a - 1) // 2
                for pj in range(pi + 1, len(group)):
                    b = group[pj]
                    store[base + (b - a - 1)] |= bit

    first_pair = {}
    index = 0
    for a in range(row_count - 1):
        for b in range(a + 1, row_count):
            mask = store[index]
            index += 1
            if mask not in first_pair:  # mask 0 is the empty agreement set
                first_pair[mask] = (a, b)  # index order equals row_id order
    maximal = _maximal_masks(list(first_pair))
    witnesses = {}
    for mask in maximal:
        best = None
        for agreement, pair in first_pair.items():
            if (mask & ~agreement) == 0 and (best is None or pair < best):
                best = pair
        witnesses[mask] = best
    return maximal, witnesses


def _dfs_path(coded, class_count, row_count, max_distinct):
    """Incremental partition DFS used when the pair table would be too large."""
    leaves = []
    witnesses = {}
    done = set()
    group_of = [0] * row_count
    sizes = [row_count]
    marks = [0] * max_distinct
    stamp = [0]

    def dfs(prefix):
        if prefix in done:
            return
        done.add(prefix)
        big_groups = [g for g, size in enumerate(sizes) if size >= 2]
        candidates = []
        for c in range(class_count):
            if prefix & (1 << c):
                continue
            column = coded[c]
            found = False
            for g in big_groups:
                stamp[0] += 1
                token = stamp[0]
                for r in range(row_count):
                    if group_of[r] != g:
                        continue
                    value = column[r]
                    if marks[value] == token:
                        found = True
                        break
                    marks[value] = token
                if found:
                    break
            if found:
                candidates.append(c)
        if not candidates:
            if big_groups:
                leaves.append(prefix)
                best = None
                for g in big_groups:
                    members = [r for r in range(row_count) if group_of[r] == g]
                    pair = (members[0], members[1])
                    if best is None or pair < best:
                        best = pair
                witnesses[prefix] = best
            return
        for c in candidates:
            column = coded[c]
            saved_labels = group_of[:]
            saved_sizes = sizes[:]
            next_label = len(sizes)
            label_of = {}
            first_label = set()
            for r in range(row_count):
                g = saved_labels[r]
                key = (g, column[r])
                label = label_of.get(key)
                if label is None:
                    if g not in first_label:
                        label = g
                        first_label.add(g)
                    else:
                        label = next_label
                        next_label += 1
                    label_of[key] = label
                group_of[r] = label
            new_sizes = [0] * next_label
            for r in range(row_count):
                new_sizes[group_of[r]] += 1
            sizes[:] = new_sizes
            dfs(prefix | (1 << c))
            group_of[:] = saved_labels
            sizes[:] = saved_sizes

    dfs(0)
    if row_count >= 2:
        leaves.append(0)  # empty agreement set always exists for >= 2 rows
    maximal = _maximal_masks(leaves)
    witnesses = {mask: witnesses[mask] for mask in maximal}
    return maximal, witnesses


def _minimal_hitting_sets(hyperedges, class_count):
    """All inclusion-minimal transversals of a family of bitmask hyperedges."""
    results = []

    def dfs(chosen, edges):
        if not edges:
            results.append(chosen)
            return
        edge = edges[0]
        bit = 1
        for v in range(class_count):
            if edge & bit:
                dfs(chosen | bit, [other for other in edges if not (other & bit)])
            bit <<= 1

    dfs(0, list(hyperedges))
    family = list(hyperedges)
    minimal = []
    for mask in set(results):
        is_minimal = True
        for v in range(class_count):
            bit = 1 << v
            if mask & bit and all((mask ^ bit) & edge for edge in family):
                is_minimal = False
                break
        if is_minimal:
            minimal.append(mask)
    return minimal


def _search_boundaries(coded, class_count, row_count, max_distinct, prefer):
    """Run one exhaustive maximal-agreement search.

    ``prefer`` selects the algorithm: ``'pair'`` for the triangular agreement
    table or ``'dfs'`` for the incremental partition search.  Both are
    exhaustive and return identical masks; callers use the alternate path as
    an independent cross-check.
    """
    store_bytes = (row_count * (row_count - 1) // 2) * 4
    pair_fits = store_bytes <= STORE_BYTE_BUDGET
    if prefer == 'pair' and pair_fits:
        return _pair_path(coded, class_count, row_count), 'pair'
    if prefer == 'dfs':
        return _dfs_path(coded, class_count, row_count, max_distinct), 'dfs'
    # Fall back to whichever path the memory budget permits.
    if pair_fits:
        return _pair_path(coded, class_count, row_count), 'pair'
    return _dfs_path(coded, class_count, row_count, max_distinct), 'dfs'


def discover_boundaries(spec):
    """Return class-level boundaries plus the mapping back to real columns."""
    row_count = len(spec['rows'])
    # columns as per-canonical-column value vectors, rows already id-ordered
    columns = [[row[1][j] for row in spec['rows']]
               for j in range(len(spec['columns']))]
    signatures, members = _build_classes(columns)
    class_count = len(signatures)
    coded, max_distinct = _code_classes(signatures, row_count)

    if row_count <= 1:
        # The empty column group is the unique boundary; no non-keys.
        return {'members': members, 'class_count': class_count,
                'row_count': row_count, 'class_keys': [0],
                'class_nonkeys': [], 'class_witnesses': {}, 'path': 'trivial'}

    if not _full_rows_unique(coded, class_count, row_count):
        full = (1 << class_count) - 1
        first_seen = {}
        witness = None
        for r in range(row_count):
            key = tuple(coded[c][r] for c in range(class_count))
            if key in first_seen:
                pair = (first_seen[key], r)
                if witness is None or pair < witness:
                    witness = pair
            else:
                first_seen[key] = r
        return {'members': members, 'class_count': class_count,
                'row_count': row_count, 'class_keys': [],
                'class_nonkeys': [full], 'class_witnesses': {full: witness},
                'path': 'duplicate'}

    (maximal, witnesses), path = _search_boundaries(
        coded, class_count, row_count, max_distinct, 'pair')
    all_classes = (1 << class_count) - 1
    edges = [all_classes ^ mask for mask in maximal]
    keys = _minimal_hitting_sets(edges, class_count)
    return {'members': members, 'class_count': class_count,
            'row_count': row_count, 'class_keys': keys,
            'class_nonkeys': maximal, 'class_witnesses': witnesses,
            'path': path}


def _expand_key(class_mask, members, class_count):
    """A class key expands to one real-column key per equivalent-class choice."""
    choices = [members[c] for c in range(class_count) if class_mask & (1 << c)]
    results = []

    def grow(position, mask):
        if position == len(choices):
            results.append(mask)
            return
        for column in choices[position]:
            grow(position + 1, mask | (1 << column))

    grow(0, 0)
    return results


def _expand_full_class_set(class_mask, members, class_count):
    """Included class contributes every member column; excluded contributes none."""
    full = 0
    for c in range(class_count):
        if class_mask & (1 << c):
            for column in members[c]:
                full |= 1 << column
    return full


def build_report(spec, digest, found):
    """Assemble the deterministic report with sorted group lists."""
    names = spec['columns']
    members = found['members']
    class_count = found['class_count']
    row_ids = [row[0] for row in spec['rows']]

    key_masks = set()
    for class_mask in found['class_keys']:
        for expanded in _expand_key(class_mask, members, class_count):
            key_masks.add(expanded)
    minimal_keys = [[names[j] for j in range(len(names)) if mask & (1 << j)]
                    for mask in key_masks]
    minimal_keys.sort(key=lambda group: (len(group), group))

    maximal_nonkeys = []
    for class_mask in found['class_nonkeys']:
        full = _expand_full_class_set(class_mask, members, class_count)
        group = [names[j] for j in range(len(names)) if full & (1 << j)]
        left, right = found['class_witnesses'][class_mask]
        maximal_nonkeys.append({
            'columns': group,
            'witness': [row_ids[left], row_ids[right]],
        })
    maximal_nonkeys.sort(key=lambda item: (len(item['columns']),
                                           item['columns']))

    return {
        'schema_version': SCHEMA_VERSION,
        'dataset_id': spec['dataset_id'],
        'source_sha256': digest,
        'row_count': found['row_count'],
        'columns': list(names),
        'minimal_keys': minimal_keys,
        'maximal_nonkeys': maximal_nonkeys,
    }


# --------------------------------------------------------------------------- #
# Independent publication-time verification
# --------------------------------------------------------------------------- #

MAX_COVERAGE_ENUM = 1 << 20  # exhaustive subset scan only for small lattices
DIRECT_KEY_CHECK_LIMIT = 64


def validate_result(spec, report, used_path):
    """Independently verify every published boundary before publication.

    ``used_path`` names the exhaustive search (``'pair'`` or ``'dfs'``) that
    produced the report; verification runs the *other* path so an
    implementation bug in either one cannot silently agree.

    Witnesses are also re-derived directly from the raw rows, key/non-key
    properties are checked by independent bitmask reasoning, and for small
    class lattices every legal subset is checked for complementary coverage.
    Raises ``ValueError`` on any inconsistency so the previous report survives.
    """
    if report.get('schema_version') != SCHEMA_VERSION:
        raise ValueError('result check: unexpected schema_version')
    if report.get('dataset_id') != spec['dataset_id']:
        raise ValueError('result check: dataset_id mismatch')
    digest = report.get('source_sha256')
    if not isinstance(digest, str) or len(digest) != 64 or any(
            ch not in '0123456789abcdef' for ch in digest):
        raise ValueError('result check: source_sha256 must be 64 hex digits')
    names = spec['columns']
    if report.get('columns') != names:
        raise ValueError('result check: canonical column order mismatch')
    rows = spec['rows']
    row_count = len(rows)
    if report.get('row_count') != row_count:
        raise ValueError('result check: row_count mismatch')

    # Re-derive equivalence classes and coding independently from the rows.
    column_vectors = [[row[1][j] for row in rows] for j in range(len(names))]
    signatures, members = _build_classes(column_vectors)
    class_count = len(signatures)
    coded, max_distinct = _code_classes(signatures, row_count)
    class_of_column = {}
    for c, group in enumerate(members):
        for column in group:
            class_of_column[column] = c
    all_classes = (1 << class_count) - 1

    def columns_to_class_mask(group):
        columns = [names.index(name) for name in group]
        class_mask = 0
        for column in columns:
            class_mask |= 1 << class_of_column[column]
        return class_mask, columns

    report_key_masks = set()
    for group in report['minimal_keys']:
        if not isinstance(group, list) or group != sorted(group):
            raise ValueError('result check: key columns must be sorted')
        mask, columns = columns_to_class_mask(group)
        # a real key is minimal only when it picks exactly one member per class
        if mask.bit_count() != len(columns):
            raise ValueError('result check: key includes redundant equivalent column')
        report_key_masks.add(mask)

    report_nonkey_masks = set()
    report_witnesses = {}
    for item in report['maximal_nonkeys']:
        if set(item) != {'columns', 'witness'}:
            raise ValueError('result check: non-key item has wrong fields')
        group = item['columns']
        if not isinstance(group, list) or group != sorted(group):
            raise ValueError('result check: non-key columns must be sorted')
        mask, columns = columns_to_class_mask(group)
        # maximal non-keys are whole-class groups: every member must be present
        for c in range(class_count):
            if mask & (1 << c):
                for column in members[c]:
                    if column not in columns:
                        raise ValueError('result check: non-key splits equivalent columns')
        report_nonkey_masks.add(mask)
        report_witnesses[mask] = tuple(item['witness'])

    # Independent exhaustive recomputation via the alternate search path.
    if row_count <= 1:
        ref_keys, ref_nonkeys, ref_witnesses = {0}, set(), {}
    elif not _full_rows_unique(coded, class_count, row_count):
        full = all_classes
        first_seen = {}
        witness = None
        for r in range(row_count):
            key = tuple(coded[c][r] for c in range(class_count))
            if key in first_seen:
                pair = (first_seen[key], r)
                if witness is None or pair < witness:
                    witness = pair
            else:
                first_seen[key] = r
        ref_keys, ref_nonkeys, ref_witnesses = set(), {full}, {full: witness}
    else:
        # The alternate exhaustive path gives a genuinely independent result.
        alternate = 'dfs' if used_path == 'pair' else 'pair'
        (ref_nonkey_masks, ref_witnesses), _ = _search_boundaries(
            coded, class_count, row_count, max_distinct, alternate)
        ref_nonkeys = set(ref_nonkey_masks)
        edges = [all_classes ^ mask for mask in ref_nonkey_masks]
        ref_keys = set(_minimal_hitting_sets(edges, class_count))

    if report_key_masks != ref_keys:
        raise ValueError('result check: minimal keys disagree with independent search')
    if report_nonkey_masks != ref_nonkeys:
        raise ValueError('result check: maximal non-keys disagree with independent search')

    # Structural properties expressed purely over class masks.
    for key in ref_keys:
        # unique: not contained in any maximal non-key (hits every complement)
        if any((key & ~nk) == 0 for nk in ref_nonkeys):
            raise ValueError('result check: reported key is contained in a non-key')
        # minimal: removing any chosen class makes it fit inside some non-key
        for c in range(class_count):
            bit = 1 << c
            if key & bit:
                reduced = key ^ bit
                if not any((reduced & ~nk) == 0 for nk in ref_nonkeys):
                    raise ValueError('result check: key is not inclusion-minimal')
    for nk in ref_nonkeys:
        # maximal: adding any absent class breaks every containing non-key
        for c in range(class_count):
            bit = 1 << c
            if not (nk & bit):
                extended = nk | bit
                if any((extended & ~other) == 0 for other in ref_nonkeys):
                    raise ValueError('result check: non-key is not inclusion-maximal')

    # Direct witness verification against the raw, type-tagged rows.
    id_to_row = {row[0]: row[1] for row in rows}
    ordered_ids = [row[0] for row in rows]
    for mask, witness in report_witnesses.items():
        if len(witness) != 2 or witness[0] >= witness[1]:
            raise ValueError('result check: witness must be [a, b] with a < b')
        a, b = witness
        if a not in id_to_row or b not in id_to_row:
            raise ValueError('result check: witness row_id not found')
        class_columns = [column for c in range(class_count) if mask & (1 << c)
                         for column in members[c]]
        if any(id_to_row[a][j] != id_to_row[b][j] for j in class_columns):
            raise ValueError('result check: witness does not collide on the group')
        # lexicographically smallest colliding pair, found by an independent scan
        smallest = None
        for x in range(row_count):
            for y in range(x + 1, row_count):
                rid_a, rid_b = ordered_ids[x], ordered_ids[y]
                if all(id_to_row[rid_a][j] == id_to_row[rid_b][j]
                       for j in class_columns):
                    smallest = (rid_a, rid_b)
                    break
            if smallest:
                break
        if tuple(witness) != smallest:
            raise ValueError('result check: witness is not the smallest pair')

    # For a bounded number of keys confirm uniqueness directly on the rows.
    if len(ref_keys) <= DIRECT_KEY_CHECK_LIMIT:
        for key in ref_keys:
            indices = [column for c in range(class_count) if key & (1 << c)
                       for column in members[c]]
            seen = set()
            for _, cells in rows:
                value = tuple(cells[j] for j in indices)
                if value in seen:
                    raise ValueError('result check: direct key uniqueness failed')
                seen.add(value)

    # Exhaustive complementary coverage over the class lattice when it is small.
    if (1 << class_count) <= MAX_COVERAGE_ENUM:
        for subset in range(1 << class_count):
            on_key_side = any((key & ~subset) == 0 for key in ref_keys)
            on_nonkey_side = any((subset & ~nk) == 0 for nk in ref_nonkeys)
            if on_key_side == on_nonkey_side:
                raise ValueError('result check: boundaries do not partition the subsets')
    return True
