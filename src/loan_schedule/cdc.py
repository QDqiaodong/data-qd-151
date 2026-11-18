"""CDC transaction-log materialization (``cdc-materialize`` product).

An interleaved, multi-table change-data-capture log with explicit transaction
boundaries is replayed into the final committed-visible snapshot.  Every
transaction gets an audit verdict with acceptance or rejection evidence.

Semantics summary
-----------------
* Records are consumed strictly in array order; ``lsn`` is strictly increasing
  and is never re-sorted.  Transactions may interleave: each open transaction
  stages its ordered changes privately, so uncommitted and aborted rows can
  never enter the visible relation.
* When a ``commit`` arrives the transaction is replayed from scratch against
  the latest accepted snapshot on a private working copy.  Row conflicts
  (``missing_row`` / ``before_mismatch`` / ``primary_key_conflict``) reject the
  whole transaction; the working copy is discarded so no partial row leaks.
* Foreign keys are checked only after every change of the transaction has
  replayed, over the *whole* candidate relation (including unchanged child
  tables), so cyclic and self references work inside one transaction while a
  dangling reference rejects it.  Any null FK component exempts that reference.
* Equality is type-aware: ``True`` never equals ``1`` and strings keep their
  raw Unicode code points without normalization.

Only the Python standard library is used; nothing accesses the network.
"""
from .common import ident

SCHEMA_VERSION = 1
TOP_KEYS = ('dataset_id', 'tables', 'initial', 'records')
TABLE_KEYS = ('table', 'columns', 'primary_key', 'foreign_keys')
COLUMN_KEYS = ('name', 'type', 'nullable')
FK_KEYS = ('fk_id', 'columns', 'ref_table')
INITIAL_KEYS = ('table', 'values')
FRAME_KEYS = ('lsn', 'txid', 'kind')
CHANGE_KEYS = ('lsn', 'txid', 'kind', 'table', 'before', 'after')
MAX_TABLES = 16
MAX_COLUMNS = 16
MAX_PRIMARY_KEY = 4
MAX_FOREIGN_KEYS = 16
MAX_INITIAL_ROWS = 2000
MAX_RECORDS = 10000
INTEGER_MIN = -10 ** 12
INTEGER_MAX = 10 ** 12
MAX_STRING_LENGTH = 256
LSN_MIN = 1
LSN_MAX = 10 ** 12
TYPES = ('integer', 'string', 'boolean')
KINDS = ('begin', 'commit', 'abort', 'change')

ROW_CONFLICT_CODES = ('missing_row', 'before_mismatch', 'primary_key_conflict')
STATUSES = ('accepted', 'rejected', 'aborted', 'open')
ERROR_KEYS = ('code', 'change_lsn', 'table', 'primary_key', 'fk_id')


# --------------------------------------------------------------------------- #
# Strict structural validation
# --------------------------------------------------------------------------- #

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


def _bounded_array(value, low, high, where):
    if not isinstance(value, list) or not low <= len(value) <= high:
        raise ValueError('%s must be an array of %d..%d items' % (where, low, high))
    return value


def _lsn(value, where):
    # ``type(...) is int`` rejects booleans, which are int subclasses in Python.
    if type(value) is not int or not LSN_MIN <= value <= LSN_MAX:
        raise ValueError('%s: lsn must be an integer in %d..%d (booleans rejected)'
                         % (where, LSN_MIN, LSN_MAX))
    return value


def _cell(value, column, where):
    """Validate one cell against its declared column and return the raw value."""
    if value is None:
        if not column['nullable']:
            raise ValueError('%s: null is not legal for non-nullable column %s'
                             % (where, column['name']))
        return None
    declared = column['type']
    if declared == 'integer':
        if type(value) is not int:  # excludes bool
            raise ValueError('%s: expected integer, got %s'
                             % (where, type(value).__name__))
        if not INTEGER_MIN <= value <= INTEGER_MAX:
            raise ValueError('%s: integer outside %d..%d'
                             % (where, INTEGER_MIN, INTEGER_MAX))
    elif declared == 'boolean':
        if type(value) is not bool:
            raise ValueError('%s: expected boolean true/false' % where)
    else:  # string
        if not isinstance(value, str):
            raise ValueError('%s: expected string' % where)
        if len(value) > MAX_STRING_LENGTH:
            raise ValueError('%s: string longer than %d code points'
                             % (where, MAX_STRING_LENGTH))
    return value


def _row(value, table, where):
    """Validate a complete positional row and return it as a tuple."""
    columns = table['columns']
    if not isinstance(value, list) or len(value) != len(columns):
        raise ValueError('%s: row must be an array of exactly %d values'
                         % (where, len(columns)))
    return tuple(_cell(item, columns[index], '%s[%d]' % (where, index))
                 for index, item in enumerate(value))


def _validate_tables(raw_tables):
    tables = []
    names = set()
    for index, raw_table in enumerate(raw_tables):
        where = 'tables[%d]' % index
        _object(raw_table, TABLE_KEYS, where)
        name = ident(raw_table['table'])
        if name in names:
            raise ValueError('duplicate table name: ' + name)
        names.add(name)

        raw_columns = _bounded_array(raw_table['columns'], 1, MAX_COLUMNS,
                                     where + '.columns')
        columns = []
        column_names = set()
        for c_index, raw_column in enumerate(raw_columns):
            c_where = '%s.columns[%d]' % (where, c_index)
            _object(raw_column, COLUMN_KEYS, c_where)
            column_name = ident(raw_column['name'])
            if column_name in column_names:
                raise ValueError('duplicate column name: ' + column_name)
            column_names.add(column_name)
            declared_type = raw_column['type']
            if declared_type not in TYPES:
                raise ValueError('%s: type must be one of %s' % (c_where, ','.join(TYPES)))
            nullable = raw_column['nullable']
            if type(nullable) is not bool:
                raise ValueError(c_where + ': nullable must be a boolean')
            columns.append({'name': column_name, 'type': declared_type,
                            'nullable': nullable})
        column_index = {column['name']: i for i, column in enumerate(columns)}

        raw_pk = _bounded_array(raw_table['primary_key'], 1, MAX_PRIMARY_KEY,
                                where + '.primary_key')
        primary_key = []
        for pk_name in raw_pk:
            pk_name = ident(pk_name)
            if pk_name in primary_key:
                raise ValueError('%s: duplicate primary key column %s' % (where, pk_name))
            if pk_name not in column_index:
                raise ValueError('%s: unknown primary key column %s' % (where, pk_name))
            if columns[column_index[pk_name]]['nullable']:
                raise ValueError('%s: primary key column %s must be nullable=false'
                                 % (where, pk_name))
            primary_key.append(pk_name)

        raw_fks = _bounded_array(raw_table['foreign_keys'], 0, MAX_FOREIGN_KEYS,
                                 where + '.foreign_keys')
        foreign_keys = []
        fk_ids = set()
        for f_index, raw_fk in enumerate(raw_fks):
            f_where = '%s.foreign_keys[%d]' % (where, f_index)
            _object(raw_fk, FK_KEYS, f_where)
            fk_id = ident(raw_fk['fk_id'])
            if fk_id in fk_ids:
                raise ValueError('%s: duplicate fk_id %s' % (where, fk_id))
            fk_ids.add(fk_id)
            ref_table = ident(raw_fk['ref_table'])
            fk_columns = []
            for fk_column in raw_fk['columns']:
                fk_column = ident(fk_column)
                if fk_column in fk_columns:
                    raise ValueError('%s: duplicate FK column %s' % (f_where, fk_column))
                if fk_column not in column_index:
                    raise ValueError('%s: unknown FK column %s' % (f_where, fk_column))
                fk_columns.append(fk_column)
            if not fk_columns:
                raise ValueError(f_where + ': columns must be a non-empty array')
            foreign_keys.append({'fk_id': fk_id, 'columns': fk_columns,
                                 'ref_table': ref_table})

        tables.append({'table': name, 'columns': columns,
                       'column_index': column_index,
                       'primary_key': primary_key,
                       'pk_index': [column_index[pk] for pk in primary_key],
                       'foreign_keys': foreign_keys})
    return tables


def _cross_validate_foreign_keys(tables):
    by_name = {table['table']: table for table in tables}
    for table in tables:
        for fk in table['foreign_keys']:
            ref = by_name.get(fk['ref_table'])
            if ref is None:
                raise ValueError('table %s FK %s references unknown table %s'
                                 % (table['table'], fk['fk_id'], fk['ref_table']))
            if len(fk['columns']) != len(ref['primary_key']):
                raise ValueError(
                    'table %s FK %s: column count must match the full primary key '
                    'of %s (%d columns); candidate keys other than the declared '
                    'primary key are not accepted'
                    % (table['table'], fk['fk_id'], ref['table'],
                       len(ref['primary_key'])))
            for position, fk_column in enumerate(fk['columns']):
                local_type = table['columns'][table['column_index'][fk_column]]['type']
                ref_type = ref['columns'][ref['pk_index'][position]]['type']
                if local_type != ref_type:
                    raise ValueError(
                        'table %s FK %s: position %d type %s does not match '
                        'referenced primary key type %s'
                        % (table['table'], fk['fk_id'], position, local_type, ref_type))


def _validate_initial(data, tables):
    by_name = {table['table']: table for table in tables}
    raw_initial = data['initial']
    if not isinstance(raw_initial, list) or len(raw_initial) > MAX_INITIAL_ROWS:
        raise ValueError('initial must contain 0..%d items' % MAX_INITIAL_ROWS)
    loaded = {table['table']: [] for table in tables}
    for index, item in enumerate(raw_initial):
        where = 'initial[%d]' % index
        _object(item, INITIAL_KEYS, where)
        table_name = ident(item['table'])
        table = by_name.get(table_name)
        if table is None:
            raise ValueError('%s: unknown table %s' % (where, table_name))
        loaded[table_name].append(_row(item['values'], table, where + '.values'))

    # Duplicate primary keys are rejected after every row has been loaded.
    for table in tables:
        seen = set()
        for row in loaded[table['table']]:
            key = tuple(row[position] for position in table['pk_index'])
            if key in seen:
                raise ValueError('table %s: duplicate initial primary key %r'
                                 % (table['table'], list(key)))
            seen.add(key)

    # Joint foreign-key pass over every table at once so declaration order and
    # cyclic/self references cannot cause order-dependent rejection.
    relations = {}
    for table in tables:
        relations[table['table']] = {
            tuple(row[position] for position in table['pk_index']): row
            for row in loaded[table['table']]}
    initial_violations = _check_all_foreign_keys(tables, relations, 'initial')
    if initial_violations:
        table_name, fk_id, child_key = initial_violations[0]
        raise ValueError('table %s: initial row %r violates foreign key %s'
                         % (table_name, list(child_key), fk_id))
    return loaded


def _validate_records(data, tables):
    by_name = {table['table']: table for table in tables}
    raw_records = data['records']
    if not isinstance(raw_records, list) or len(raw_records) > MAX_RECORDS:
        raise ValueError('records must contain 0..%d items' % MAX_RECORDS)
    records = []
    previous_lsn = None
    tx_state = {}  # txid -> 'open' / 'ended'
    for index, raw_record in enumerate(raw_records):
        where = 'records[%d]' % index
        if not isinstance(raw_record, dict):
            raise ValueError(where + ' must be an object')
        kind = raw_record.get('kind')
        allowed = CHANGE_KEYS if kind == 'change' else FRAME_KEYS
        unknown = sorted(set(raw_record) - set(allowed))
        if unknown:
            raise ValueError('%s: unknown field %s' % (where, unknown[0]))
        missing = [key for key in allowed if key not in raw_record]
        if missing:
            raise ValueError('%s: missing field %s' % (where, missing[0]))
        if kind not in KINDS:
            raise ValueError('%s: unknown kind %r' % (where, kind))
        lsn = _lsn(raw_record['lsn'], where)
        if previous_lsn is not None and lsn <= previous_lsn:
            raise ValueError('%s: lsn must be strictly increasing' % where)
        previous_lsn = lsn
        txid = ident(raw_record['txid'])

        if kind == 'change':
            if tx_state.get(txid) != 'open':
                raise ValueError('%s: change has no currently open transaction %s'
                                 % (where, txid))
            table_name = ident(raw_record['table'])
            table = by_name.get(table_name)
            if table is None:
                raise ValueError('%s: unknown table %s' % (where, table_name))
            before = raw_record['before']
            after = raw_record['after']
            if before is None and after is None:
                raise ValueError(where + ': before and after cannot both be null')
            # Static structure is validated even for aborted/open transactions;
            # dynamic before/key validity is never checked at parse time.
            before_row = (_row(before, table, where + '.before')
                          if before is not None else None)
            after_row = (_row(after, table, where + '.after')
                         if after is not None else None)
            records.append({'lsn': lsn, 'txid': txid, 'kind': kind,
                            'table': table_name, 'before': before_row,
                            'after': after_row})
        else:
            if kind == 'begin':
                if txid in tx_state:
                    raise ValueError('%s: txid %s began more than once' % (where, txid))
                tx_state[txid] = 'open'
            else:
                if tx_state.get(txid) != 'open':
                    raise ValueError('%s: %s has no currently open transaction %s'
                                     % (where, kind, txid))
                tx_state[txid] = 'ended'
            records.append({'lsn': lsn, 'txid': txid, 'kind': kind})
    return records


def validate_input(data):
    """Validate the whole strict contract and return a normalized spec.

    Any structural or frame violation raises ``ValueError``; the caller treats
    that as whole-input failure (exit 2, previous report preserved).
    """
    _object(data, TOP_KEYS, 'input')
    dataset_id = ident(data['dataset_id'])
    raw_tables = _bounded_array(data['tables'], 1, MAX_TABLES, 'tables')
    tables = _validate_tables(raw_tables)
    _cross_validate_foreign_keys(tables)
    initial = _validate_initial(data, tables)
    records = _validate_records(data, tables)
    return {'dataset_id': dataset_id, 'tables': tables,
            'initial': initial, 'records': records}


# --------------------------------------------------------------------------- #
# Materialization engine
# --------------------------------------------------------------------------- #

def _primary_key(table, row):
    return tuple(row[position] for position in table['pk_index'])


def _typed_sort_key(key):
    """Order key parts by typed rank: false < true < integers < strings."""
    def rank(part):
        if part is None:
            return (0, 0)
        if type(part) is bool:
            return (1, int(part))
        if type(part) is int:
            return (2, part)
        return (3, part)  # Unicode code-point comparison on the raw string
    return tuple(rank(part) for part in key)


def _rows_equal(left, right):
    """Type-aware full-row equality: True != 1, strings are unnormalized."""
    if len(left) != len(right):
        return False
    for a, b in zip(left, right):
        if type(a) is not type(b) or a != b:
            return False
    return True


def _check_all_foreign_keys(tables, relations, phase):
    """Scan every candidate relation; return violations in deterministic order.

    Each violation is ``(table_name, fk_id, child_primary_key_tuple)``.  A null
    component exempts that reference.  The full-relation scan is what makes
    unchanged child tables and cyclic/self references observable.
    """
    by_name = {table['table']: table for table in tables}
    violations = []
    for table in tables:
        fk_specs = table['foreign_keys']
        if not fk_specs:
            continue
        for key, row in relations[table['table']].items():
            for fk in fk_specs:
                components = [row[table['column_index'][column]]
                              for column in fk['columns']]
                if any(component is None for component in components):
                    continue  # null-component exemption
                ref = by_name[fk['ref_table']]
                target = tuple(components[position]
                               for position in range(len(components)))
                if target not in relations[ref['table']]:
                    violations.append((table['table'], fk['fk_id'], key))
    violations.sort(key=lambda item: (item[0], item[1],
                                      _typed_sort_key(item[2])))
    return violations


def _replay_transaction(table_by_name, tables, relations, changes):
    """Replay one transaction on a private working copy.

    Returns ``(error, working_relations)``.  On success ``error`` is None and
    the caller adopts the working copy; on failure the working copy is simply
    discarded, leaving the accepted relation untouched.
    """
    working = {name: dict(rows) for name, rows in relations.items()}
    for change in changes:
        table = table_by_name[change['table']]
        relation = working[change['table']]
        before = change['before']
        after = change['after']
        lsn = change['lsn']

        if before is None:  # insert
            target_key = _primary_key(table, after)
            if target_key in relation:
                return _row_error('primary_key_conflict', lsn, change['table'],
                                  target_key), working
            relation[target_key] = after
            continue

        before_key = _primary_key(table, before)
        current = relation.get(before_key)
        # Locate and verify before before touching any after-state, even when
        # one change could fail in several ways.
        if current is None:
            return _row_error('missing_row', lsn, change['table'],
                              before_key), working
        if not _rows_equal(current, before):
            return _row_error('before_mismatch', lsn, change['table'],
                              before_key), working

        if after is None:  # delete
            del relation[before_key]
        else:  # update: remove the old row first, then check the new PK
            del relation[before_key]
            after_key = _primary_key(table, after)
            if after_key in relation:
                return _row_error('primary_key_conflict', lsn, change['table'],
                                  after_key), working
            relation[after_key] = after

    # All changes replayed: only now are foreign keys adjudicated, over the
    # whole candidate relation (all tables, unchanged ones included).
    violations = _check_all_foreign_keys(tables, working, 'commit')
    if violations:
        table_name, fk_id, child_key = violations[0]
        return {'code': 'foreign_key', 'change_lsn': None,
                'table': table_name, 'primary_key': list(child_key),
                'fk_id': fk_id}, working
    return None, working


def _row_error(code, lsn, table_name, key):
    return {'code': code, 'change_lsn': lsn, 'table': table_name,
            'primary_key': list(key), 'fk_id': None}


def materialize(spec):
    """Consume the staged log and return final relations plus the full audit."""
    tables = spec['tables']
    table_by_name = {table['table']: table for table in tables}
    relations = {
        table['table']: {
            tuple(row[position] for position in table['pk_index']): row
            for row in spec['initial'][table['table']]}
        for table in tables}

    staged = {}       # txid -> ordered list of change records
    begin_lsn = {}    # txid -> begin lsn
    audit_order = []  # txids in begin order
    audits = {}       # txid -> audit entry

    for record in spec['records']:
        txid = record['txid']
        kind = record['kind']
        if kind == 'begin':
            staged[txid] = []
            begin_lsn[txid] = record['lsn']
            audit_order.append(txid)
            audits[txid] = {'txid': txid, 'begin_lsn': record['lsn'],
                            'end_lsn': None, 'status': 'open',
                            'change_lsns': [], 'error': None}
        elif kind == 'change':
            staged[txid].append(record)
            audits[txid]['change_lsns'].append(record['lsn'])
        elif kind == 'abort':
            audits[txid]['status'] = 'aborted'
            audits[txid]['end_lsn'] = record['lsn']
            staged.pop(txid, None)
        else:  # commit
            error, working = _replay_transaction(
                table_by_name, tables, relations, staged[txid])
            entry = audits[txid]
            entry['end_lsn'] = record['lsn']
            if error is None:
                relations = working  # adopt the private candidate wholesale
                entry['status'] = 'accepted'
            else:
                entry['status'] = 'rejected'
                entry['error'] = error
            staged.pop(txid, None)

    transactions = [audits[txid] for txid in
                    sorted(audit_order, key=lambda one: begin_lsn[one])]
    return {'relations': relations, 'transactions': transactions}


# --------------------------------------------------------------------------- #
# Report assembly
# --------------------------------------------------------------------------- #

def _column_report(column):
    return {'name': column['name'], 'type': column['type'],
            'nullable': column['nullable']}


def _fk_report(fk):
    return {'fk_id': fk['fk_id'], 'columns': list(fk['columns']),
            'ref_table': fk['ref_table']}


def build_report(spec, digest, material):
    table_reports = []
    for table in sorted(spec['tables'], key=lambda item: item['table']):
        relation = material['relations'][table['table']]
        ordered_rows = [list(row) for row in sorted(
            relation.values(),
            key=lambda row: _typed_sort_key(
                tuple(row[position] for position in table['pk_index'])))]
        table_reports.append({
            'table': table['table'],
            'columns': [_column_report(column) for column in table['columns']],
            'primary_key': list(table['primary_key']),
            'foreign_keys': [_fk_report(fk) for fk in
                             sorted(table['foreign_keys'],
                                    key=lambda fk: fk['fk_id'])],
            'rows': ordered_rows})
    return {'schema_version': SCHEMA_VERSION,
            'dataset_id': spec['dataset_id'],
            'source_sha256': digest,
            'tables': table_reports,
            'transactions': material['transactions']}


# --------------------------------------------------------------------------- #
# Independent publication-time verification
# --------------------------------------------------------------------------- #

def _oracle_replay(spec):
    """Second, deliberately independent replay used only for verification.

    It re-derives every verdict and the final rows from the validated spec
    without calling :func:`materialize` or :func:`_replay_transaction`, using
    plain list staging and locally defined helpers, so an engine bug cannot be
    self-consistent.
    """
    catalog = {t['table']: t for t in spec['tables']}
    state = {}
    for t in spec['tables']:
        state[t['table']] = {}
        for row in spec['initial'][t['table']]:
            key = tuple(row[i] for i in t['pk_index'])
            state[t['table']][key] = row

    def same_type_value(a, b):
        return type(a) is type(b) and a == b

    def same_row(a, b):
        return len(a) == len(b) and all(same_type_value(x, y)
                                        for x, y in zip(a, b))

    def key_of(table_name, row):
        t = catalog[table_name]
        return tuple(row[i] for i in t['pk_index'])

    def foreign_key_violations(candidate):
        found = []
        for t in spec['tables']:
            for key, row in candidate[t['table']].items():
                for fk in t['foreign_keys']:
                    vals = [row[t['column_index'][c]] for c in fk['columns']]
                    if any(v is None for v in vals):
                        continue
                    target_table = catalog[fk['ref_table']]
                    if tuple(vals) not in candidate[target_table['table']]:
                        found.append((t['table'], fk['fk_id'], key))
        found.sort(key=lambda triple: (triple[0], triple[1],
                                       _typed_sort_key(triple[2])))
        return found

    opens = {}
    begins = {}
    order = []
    book = {}
    for record in spec['records']:
        txid = record['txid']
        if record['kind'] == 'begin':
            opens[txid] = []
            begins[txid] = record['lsn']
            order.append(txid)
            book[txid] = {'txid': txid, 'begin_lsn': record['lsn'],
                          'end_lsn': None, 'status': 'open',
                          'change_lsns': [], 'error': None}
        elif record['kind'] == 'change':
            opens[txid].append(record)
            book[txid]['change_lsns'].append(record['lsn'])
        elif record['kind'] == 'abort':
            book[txid]['status'] = 'aborted'
            book[txid]['end_lsn'] = record['lsn']
            del opens[txid]
        else:
            candidate = {name: dict(rows) for name, rows in state.items()}
            failure = None
            for change in opens[txid]:
                t = catalog[change['table']]
                rel = candidate[change['table']]
                b, a = change['before'], change['after']
                if b is None:
                    nk = key_of(change['table'], a)
                    if nk in rel:
                        failure = _row_error('primary_key_conflict',
                                             change['lsn'], change['table'], nk)
                        break
                    rel[nk] = a
                    continue
                bk = key_of(change['table'], b)
                if bk not in rel:
                    failure = _row_error('missing_row', change['lsn'],
                                         change['table'], bk)
                    break
                if not same_row(rel[bk], b):
                    failure = _row_error('before_mismatch', change['lsn'],
                                         change['table'], bk)
                    break
                del rel[bk]
                if a is not None:
                    ak = key_of(change['table'], a)
                    if ak in rel:
                        failure = _row_error('primary_key_conflict',
                                             change['lsn'], change['table'], ak)
                        break
                    rel[ak] = a
            if failure is None:
                dangling = foreign_key_violations(candidate)
                if dangling:
                    tbl, fk_id, child_key = dangling[0]
                    failure = {'code': 'foreign_key', 'change_lsn': None,
                               'table': tbl, 'primary_key': list(child_key),
                               'fk_id': fk_id}
            entry = book[txid]
            entry['end_lsn'] = record['lsn']
            if failure is None:
                state = candidate
                entry['status'] = 'accepted'
            else:
                entry['status'] = 'rejected'
                entry['error'] = failure
            del opens[txid]

    expected_transactions = [book[tx] for tx in
                             sorted(order, key=lambda one: begins[one])]
    expected_rows = {}
    for t in spec['tables']:
        expected_rows[t['table']] = [
            list(row) for row in sorted(
                state[t['table']].values(),
                key=lambda row: tuple(row[i] for i in t['pk_index']))]
    return expected_rows, expected_transactions


def validate_result(spec, digest, report):
    """Verify the whole report before publication; raise on any disagreement."""
    if not isinstance(report, dict) or set(report) != {
            'schema_version', 'dataset_id', 'source_sha256',
            'tables', 'transactions'}:
        raise ValueError('result check: report frame mismatch')
    if report['schema_version'] != SCHEMA_VERSION:
        raise ValueError('result check: schema_version mismatch')
    if report['dataset_id'] != spec['dataset_id']:
        raise ValueError('result check: dataset_id mismatch')
    if report['source_sha256'] != digest or not isinstance(digest, str) \
            or len(digest) != 64:
        raise ValueError('result check: source_sha256 mismatch')

    catalog = {t['table']: t for t in spec['tables']}
    if [item['table'] for item in report['tables']] != \
            sorted(catalog):
        raise ValueError('result check: table set/order mismatch')
    for item in report['tables']:
        t = catalog[item['table']]
        if [c['name'] for c in item['columns']] != \
                [c['name'] for c in t['columns']] or \
                any(set(c) != set(COLUMN_KEYS) or
                    c['type'] != t['columns'][i]['type'] or
                    c['nullable'] != t['columns'][i]['nullable']
                    for i, c in enumerate(item['columns'])):
            raise ValueError('result check: column declaration mismatch on '
                             + t['table'])
        if item['primary_key'] != t['primary_key']:
            raise ValueError('result check: primary key order mismatch on '
                             + t['table'])
        if [fk['fk_id'] for fk in item['foreign_keys']] != \
                sorted(fk['fk_id'] for fk in t['foreign_keys']) or \
                any(set(fk) != set(FK_KEYS) for fk in item['foreign_keys']):
            raise ValueError('result check: foreign key declaration mismatch on '
                             + t['table'])
        # Row width, per-column type/nullability and primary-key uniqueness.
        seen = set()
        width = len(t['columns'])
        for row in item['rows']:
            if not isinstance(row, list) or len(row) != width:
                raise ValueError('result check: row width mismatch on ' + t['table'])
            for index, value in enumerate(row):
                _cell(value, t['columns'][index],
                      'result check %s[%d]' % (t['table'], index))
            key = tuple(row[i] for i in t['pk_index'])
            if key in seen:
                raise ValueError('result check: duplicate published key on '
                                 + t['table'])
            seen.add(key)
        ordered = [tuple(row[i] for i in t['pk_index']) for row in item['rows']]
        if ordered != sorted(ordered, key=_typed_sort_key):
            raise ValueError('result check: rows not sorted by primary key on '
                             + t['table'])

    # Full final foreign-key adjudication over the published rows.
    published = {item['table']: {
        tuple(row[i] for i in catalog[item['table']]['pk_index']): tuple(row)
        for row in item['rows']} for item in report['tables']}
    if _check_all_foreign_keys(spec['tables'], published, 'verify'):
        raise ValueError('result check: published relation has dangling FK')

    # Audit frame cross-check against the raw validated records.
    records = spec['records']
    by_tx = {}
    for record in records:
        by_tx.setdefault(record['txid'], []).append(record)
    if [entry['txid'] for entry in report['transactions']] != \
            [tx for tx, _ in sorted(
                ((tx, rs[0]['lsn']) for tx, rs in by_tx.items()),
                key=lambda pair: pair[1])]:
        raise ValueError('result check: transaction order/set mismatch')
    last_lsn = {record['txid']: record['lsn'] for record in records}
    for entry in report['transactions']:
        if set(entry) != {'txid', 'begin_lsn', 'end_lsn', 'status',
                          'change_lsns', 'error'}:
            raise ValueError('result check: audit frame mismatch')
        if entry['status'] not in STATUSES:
            raise ValueError('result check: invalid status')
        raw = by_tx[entry['txid']]
        begin = [r for r in raw if r['kind'] == 'begin']
        if len(begin) != 1 or entry['begin_lsn'] != begin[0]['lsn']:
            raise ValueError('result check: begin_lsn mismatch')
        changes = [r for r in raw if r['kind'] == 'change']
        if entry['change_lsns'] != [r['lsn'] for r in changes]:
            raise ValueError('result check: change_lsns mismatch')
        ends = [r for r in raw if r['kind'] in ('commit', 'abort')]
        if entry['status'] == 'open':
            if ends or entry['end_lsn'] is not None or entry['error'] is not None:
                raise ValueError('result check: open transaction frame mismatch')
        else:
            if len(ends) != 1 or entry['end_lsn'] != ends[0]['lsn']:
                raise ValueError('result check: end_lsn mismatch')
        if entry['status'] in ('accepted', 'aborted') and entry['error'] is not None:
            raise ValueError('result check: non-rejected error must be null')
        if entry['status'] == 'aborted' and ends[0]['kind'] != 'abort':
            raise ValueError('result check: aborted status mismatch')
        if entry['status'] in ('accepted', 'rejected') and \
                ends[0]['kind'] != 'commit':
            raise ValueError('result check: commit status mismatch')
        error = entry['error']
        if entry['status'] == 'rejected':
            if not isinstance(error, dict) or set(error) != set(ERROR_KEYS):
                raise ValueError('result check: error frame mismatch')
            if error['code'] in ROW_CONFLICT_CODES:
                match = [r for r in changes if r['lsn'] == error['change_lsn']]
                if len(match) != 1 or match[0]['table'] != error['table']:
                    raise ValueError('result check: error locator not in source log')
                change = match[0]
                # missing_row/before_mismatch locate the before-image key; an
                # insert/update conflict locates the conflicting target key.
                if error['code'] == 'primary_key_conflict':
                    expected_pk = _primary_key(catalog[error['table']],
                                               change['after'])
                else:
                    expected_pk = _primary_key(catalog[error['table']],
                                               change['before'])
                if list(expected_pk) != error['primary_key'] or \
                        error['fk_id'] is not None:
                    raise ValueError('result check: primary key locator mismatch')
            elif error['code'] == 'foreign_key':
                t = catalog.get(error['table'])
                if t is None or error['change_lsn'] is not None or \
                        not any(fk['fk_id'] == error['fk_id']
                                for fk in t['foreign_keys']) or \
                        len(error['primary_key']) != len(t['primary_key']):
                    raise ValueError('result check: FK error locator mismatch')
            else:
                raise ValueError('result check: unknown error code')
        elif error is not None:
            raise ValueError('result check: error must be null outside rejected')

    # The independent replay must reproduce every verdict and every row.
    expected_rows, expected_transactions = _oracle_replay(spec)
    if report['transactions'] != expected_transactions:
        raise ValueError('result check: independent audit replay disagrees')
    for item in report['tables']:
        if item['rows'] != expected_rows[item['table']]:
            raise ValueError('result check: independent row replay disagrees on '
                             + item['table'])
    return True
