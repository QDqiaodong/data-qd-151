"""SQLite persistence and atomic publication for the stress dataset.

The build always targets a throwaway database in the destination directory:
schema creation, every fact insert, the SQLite constraint checks and the
reconciliation views all complete inside one transaction before the connection
is closed.  Only then is the finished file renamed over the destination, so a
parse, calculation or SQL failure never overwrites a previously published
dataset.
"""
from pathlib import Path
import os
import sqlite3
import tempfile

FORMAT = 'data-qd-151-stress/1'
SCHEMA_VERSION = 1
TRANCHES = ('senior', 'junior')

SCHEMA = '''
CREATE TABLE dataset_meta (
    id INTEGER PRIMARY KEY CHECK (id=1),
    format TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    source_sha256 TEXT NOT NULL CHECK (length(source_sha256)=64),
    pool_id TEXT NOT NULL
) STRICT;
CREATE TABLE pool_config (
    id INTEGER PRIMARY KEY CHECK (id=1) REFERENCES dataset_meta(id),
    pool_id TEXT NOT NULL,
    horizon INTEGER NOT NULL CHECK (horizon BETWEEN 1 AND 48),
    initial_principal INTEGER NOT NULL CHECK (initial_principal BETWEEN 1 AND 1000000000000),
    reserve_target INTEGER NOT NULL CHECK (reserve_target >= 0)
) STRICT;
CREATE TABLE loan_contracts (
    loan_id TEXT PRIMARY KEY CHECK (length(loan_id) BETWEEN 1 AND 128),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    principal INTEGER NOT NULL CHECK (principal BETWEEN 1 AND 1000000000000),
    periods INTEGER NOT NULL CHECK (periods BETWEEN 1 AND 10000),
    rate_numerator INTEGER NOT NULL CHECK (rate_numerator BETWEEN 0 AND 1000000),
    rate_denominator INTEGER NOT NULL CHECK (rate_denominator BETWEEN 1 AND 1000000000),
    CHECK (periods <= principal),
    CHECK (rate_numerator <= rate_denominator)
) STRICT;
CREATE TABLE exit_terms (
    loan_id TEXT PRIMARY KEY REFERENCES loan_contracts(loan_id),
    kind TEXT NOT NULL CHECK (kind IN ('prepay','default')),
    exit_period INTEGER NOT NULL CHECK (exit_period BETWEEN 1 AND 10000),
    recovery_numerator INTEGER,
    recovery_denominator INTEGER,
    recovery_lag INTEGER,
    CHECK (kind='prepay' OR (recovery_numerator IS NOT NULL
        AND recovery_denominator IS NOT NULL AND recovery_lag IS NOT NULL)),
    CHECK (recovery_numerator IS NULL OR recovery_numerator BETWEEN 0 AND 1000000000),
    CHECK (recovery_denominator IS NULL OR recovery_denominator BETWEEN 1 AND 1000000000),
    CHECK (recovery_numerator IS NULL OR recovery_numerator <= recovery_denominator),
    CHECK (recovery_lag IS NULL OR recovery_lag BETWEEN 1 AND 48)
) STRICT;
CREATE TABLE tranche_terms (
    tranche TEXT PRIMARY KEY CHECK (tranche IN ('senior','junior')),
    rank_order INTEGER NOT NULL UNIQUE CHECK (rank_order BETWEEN 0 AND 1),
    principal INTEGER NOT NULL CHECK (principal >= 0),
    rate_numerator INTEGER NOT NULL CHECK (rate_numerator BETWEEN 0 AND 1000000),
    rate_denominator INTEGER NOT NULL CHECK (rate_denominator BETWEEN 1 AND 1000000000),
    CHECK (rate_numerator <= rate_denominator)
) STRICT;
CREATE TABLE cleanup_terms (
    id INTEGER PRIMARY KEY CHECK (id=1),
    threshold_numerator INTEGER NOT NULL CHECK (threshold_numerator BETWEEN 0 AND 2000000000),
    threshold_denominator INTEGER NOT NULL CHECK (threshold_denominator BETWEEN 1 AND 1000000000),
    price_numerator INTEGER NOT NULL CHECK (price_numerator BETWEEN 0 AND 2000000000),
    price_denominator INTEGER NOT NULL CHECK (price_denominator BETWEEN 1 AND 1000000000),
    CHECK (threshold_numerator <= threshold_denominator),
    CHECK (price_numerator <= 2 * price_denominator)
) STRICT;
CREATE TABLE asset_facts (
    loan_id TEXT NOT NULL REFERENCES loan_contracts(loan_id),
    period INTEGER NOT NULL CHECK (period BETWEEN 1 AND 48),
    opening_principal INTEGER NOT NULL CHECK (opening_principal >= 0),
    contract_interest INTEGER NOT NULL CHECK (contract_interest >= 0),
    contract_principal INTEGER NOT NULL CHECK (contract_principal >= 0),
    prepay_principal INTEGER NOT NULL CHECK (prepay_principal >= 0),
    defaulted_principal INTEGER NOT NULL CHECK (defaulted_principal >= 0),
    asset_loss INTEGER NOT NULL CHECK (asset_loss >= 0),
    recovery_recognized INTEGER NOT NULL CHECK (recovery_recognized >= 0),
    recovery_received INTEGER NOT NULL CHECK (recovery_received >= 0),
    pending_open INTEGER NOT NULL CHECK (pending_open >= 0),
    pending_close INTEGER NOT NULL CHECK (pending_close >= 0),
    sold_principal INTEGER NOT NULL CHECK (sold_principal >= 0),
    canceled_recovery INTEGER NOT NULL CHECK (canceled_recovery >= 0),
    sale_proceeds INTEGER NOT NULL CHECK (sale_proceeds >= 0),
    closing_principal INTEGER NOT NULL CHECK (closing_principal >= 0),
    asset_cash_in INTEGER NOT NULL CHECK (asset_cash_in >= 0),
    status TEXT NOT NULL CHECK (status IN ('active','defaulted','prepaid','closed','sold')),
    PRIMARY KEY (loan_id, period)
) STRICT;
CREATE INDEX asset_facts_period ON asset_facts(period);
CREATE TABLE tranche_facts (
    tranche TEXT NOT NULL REFERENCES tranche_terms(tranche),
    period INTEGER NOT NULL CHECK (period BETWEEN 1 AND 48),
    opening_principal INTEGER NOT NULL CHECK (opening_principal >= 0),
    opening_unpaid_interest INTEGER NOT NULL CHECK (opening_unpaid_interest >= 0),
    accrued_interest INTEGER NOT NULL CHECK (accrued_interest >= 0),
    interest_paid INTEGER NOT NULL CHECK (interest_paid >= 0),
    principal_paid INTEGER NOT NULL CHECK (principal_paid >= 0),
    closing_principal INTEGER NOT NULL CHECK (closing_principal >= 0),
    closing_unpaid_interest INTEGER NOT NULL CHECK (closing_unpaid_interest >= 0),
    PRIMARY KEY (tranche, period)
) STRICT;
CREATE TABLE pool_facts (
    period INTEGER PRIMARY KEY CHECK (period BETWEEN 1 AND 48),
    opening_reserve INTEGER NOT NULL CHECK (opening_reserve >= 0),
    reserve_used INTEGER NOT NULL CHECK (reserve_used >= 0),
    reserve_replenished INTEGER NOT NULL CHECK (reserve_replenished >= 0),
    reserve_released INTEGER NOT NULL CHECK (reserve_released >= 0),
    closing_reserve INTEGER NOT NULL CHECK (closing_reserve >= 0),
    contract_cash_in INTEGER NOT NULL CHECK (contract_cash_in >= 0),
    recovery_cash_in INTEGER NOT NULL CHECK (recovery_cash_in >= 0),
    sale_proceeds INTEGER NOT NULL CHECK (sale_proceeds >= 0),
    total_asset_principal_open INTEGER NOT NULL CHECK (total_asset_principal_open >= 0),
    total_pending_open INTEGER NOT NULL CHECK (total_pending_open >= 0),
    total_asset_principal_close INTEGER NOT NULL CHECK (total_asset_principal_close >= 0),
    total_pending_close INTEGER NOT NULL CHECK (total_pending_close >= 0),
    quote_offered INTEGER NOT NULL CHECK (quote_offered IN (0,1)),
    quote_ineligible_reason TEXT CHECK (quote_ineligible_reason IS NULL
        OR quote_ineligible_reason IN ('no_saleable_assets','above_threshold')),
    quote_price INTEGER NOT NULL CHECK (quote_price >= 0),
    cleanup_exercised INTEGER NOT NULL CHECK (cleanup_exercised IN (0,1)),
    total_debt_at_quote INTEGER NOT NULL CHECK (total_debt_at_quote >= 0),
    coverage_gap INTEGER NOT NULL,
    interest_paid INTEGER NOT NULL CHECK (interest_paid >= 0),
    principal_paid INTEGER NOT NULL CHECK (principal_paid >= 0),
    sponsor_cash INTEGER NOT NULL CHECK (sponsor_cash >= 0),
    CHECK (quote_offered=1 OR (quote_price=0 AND cleanup_exercised=0)),
    CHECK (cleanup_exercised=1 OR quote_ineligible_reason IS NOT NULL
        OR coverage_gap > 0)
) STRICT;
CREATE VIEW v_loan_principal_conservation AS
SELECT c.loan_id, c.principal AS initial_principal,
       coalesce(sum(f.contract_principal + f.prepay_principal
                    + f.defaulted_principal + f.sold_principal),0) AS principal_out,
       (c.principal = coalesce(sum(f.contract_principal + f.prepay_principal
                    + f.defaulted_principal + f.sold_principal),0)) AS balanced
FROM loan_contracts c
LEFT JOIN asset_facts f ON f.loan_id=c.loan_id
GROUP BY c.loan_id;
CREATE VIEW v_recovery_conservation AS
SELECT f.loan_id,
       sum(f.recovery_recognized) AS recognized,
       sum(f.recovery_received + f.canceled_recovery) AS settled,
       (sum(f.recovery_recognized) = sum(f.recovery_received + f.canceled_recovery)) AS balanced
FROM asset_facts f
GROUP BY f.loan_id;
CREATE VIEW v_tranche_rollforward AS
SELECT t.tranche, t.period,
       (t.opening_principal - t.principal_paid = t.closing_principal) AS principal_ok,
       (t.opening_unpaid_interest + t.accrued_interest - t.interest_paid
        = t.closing_unpaid_interest) AS interest_ok
FROM tranche_facts t;
CREATE VIEW v_pool_cash_conservation AS
SELECT p.period,
       (p.opening_reserve + p.contract_cash_in + p.recovery_cash_in + p.sale_proceeds)
           AS sources,
       (p.closing_reserve + p.interest_paid + p.principal_paid + p.sponsor_cash)
           AS uses,
       ((p.opening_reserve + p.contract_cash_in + p.recovery_cash_in + p.sale_proceeds)
        = (p.closing_reserve + p.interest_paid + p.principal_paid + p.sponsor_cash))
           AS balanced
FROM pool_facts p;
'''


def _statements(script):
    pending = ''
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            yield pending.strip()
            pending = ''
    if pending.strip():
        raise ValueError('incomplete SQL schema statement')


def build_database(dest, spec, asset_rows, tranche_rows, pool_rows, source_sha):
    """Build the full dataset at a temp path and return it; never touches dest."""
    parent = Path(dest).parent
    parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(prefix='.tranche-build-', suffix='.db', dir=parent)
    os.close(handle)
    try:
        db = sqlite3.connect(temp, isolation_level=None)
        try:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('BEGIN IMMEDIATE')
            try:
                for statement in _statements(SCHEMA):
                    db.execute(statement)
                _insert(db, spec, asset_rows, tranche_rows, pool_rows, source_sha)
                _check_sql(db, spec)
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise
        finally:
            db.close()
    except BaseException:
        if os.path.exists(temp):
            os.unlink(temp)
        raise
    return temp


def publish(temp, dest):
    """Atomic rename of the closed temp database over the destination."""
    os.replace(temp, dest)


def _insert(db, spec, asset_rows, tranche_rows, pool_rows, source_sha):
    db.execute('INSERT INTO dataset_meta(id,format,schema_version,source_sha256,pool_id) '
               'VALUES(1,?,?,?,?)',
               (FORMAT, SCHEMA_VERSION, source_sha, spec['pool_id']))
    db.execute('INSERT INTO pool_config(id,pool_id,horizon,initial_principal,reserve_target) '
               'VALUES(1,?,?,?,?)',
               (spec['pool_id'], spec['horizon'], spec['initial_principal'], spec['reserve']))
    for loan in spec['loans']:
        db.execute('INSERT INTO loan_contracts(loan_id,ordinal,principal,periods,'
                   'rate_numerator,rate_denominator) VALUES(?,?,?,?,?,?)',
                   (loan['loan_id'], loan['ordinal'], loan['principal'], loan['periods'],
                    loan['rate_numerator'], loan['rate_denominator']))
    for loan_id in sorted(spec['exits']):
        exit_event = spec['exits'][loan_id]
        db.execute('INSERT INTO exit_terms(loan_id,kind,exit_period,recovery_numerator,'
                   'recovery_denominator,recovery_lag) VALUES(?,?,?,?,?,?)',
                   (loan_id, exit_event['kind'], exit_event['period'],
                    exit_event['recovery_numerator'], exit_event['recovery_denominator'],
                    exit_event['recovery_lag']))
    for rank, name in enumerate(TRANCHES):
        terms = spec[name]
        db.execute('INSERT INTO tranche_terms(tranche,rank_order,principal,'
                   'rate_numerator,rate_denominator) VALUES(?,?,?,?,?)',
                   (name, rank, terms['principal'], terms['rate_numerator'],
                    terms['rate_denominator']))
    cleanup = spec['cleanup']
    db.execute('INSERT INTO cleanup_terms(id,threshold_numerator,threshold_denominator,'
               'price_numerator,price_denominator) VALUES(1,?,?,?,?)',
               (cleanup['threshold_numerator'], cleanup['threshold_denominator'],
                cleanup['price_numerator'], cleanup['price_denominator']))
    db.executemany(
        'INSERT INTO asset_facts(loan_id,period,opening_principal,contract_interest,'
        'contract_principal,prepay_principal,defaulted_principal,asset_loss,'
        'recovery_recognized,recovery_received,pending_open,pending_close,'
        'sold_principal,canceled_recovery,sale_proceeds,closing_principal,'
        'asset_cash_in,status) VALUES(' + ','.join('?' * 18) + ')',
        [(row['loan_id'], row['period'], row['opening_principal'], row['contract_interest'],
          row['contract_principal'], row['prepay_principal'], row['defaulted_principal'],
          row['asset_loss'], row['recovery_recognized'], row['recovery_received'],
          row['pending_open'], row['pending_close'], row['sold_principal'],
          row['canceled_recovery'], row['sale_proceeds'], row['closing_principal'],
          row['asset_cash_in'], row['status']) for row in asset_rows])
    db.executemany(
        'INSERT INTO tranche_facts(tranche,period,opening_principal,'
        'opening_unpaid_interest,accrued_interest,interest_paid,principal_paid,'
        'closing_principal,closing_unpaid_interest) VALUES(?,?,?,?,?,?,?,?,?)',
        [(row['tranche'], row['period'], row['opening_principal'],
          row['opening_unpaid_interest'], row['accrued_interest'], row['interest_paid'],
          row['principal_paid'], row['closing_principal'],
          row['closing_unpaid_interest']) for row in tranche_rows])
    db.executemany(
        'INSERT INTO pool_facts(period,opening_reserve,reserve_used,reserve_replenished,'
        'reserve_released,closing_reserve,contract_cash_in,recovery_cash_in,'
        'sale_proceeds,total_asset_principal_open,total_pending_open,'
        'total_asset_principal_close,total_pending_close,quote_offered,'
        'quote_ineligible_reason,quote_price,cleanup_exercised,total_debt_at_quote,'
        'coverage_gap,interest_paid,principal_paid,sponsor_cash) '
        'VALUES(' + ','.join('?' * 22) + ')',
        [(row['period'], row['opening_reserve'], row['reserve_used'],
          row['reserve_replenished'], row['reserve_released'], row['closing_reserve'],
          row['contract_cash_in'], row['recovery_cash_in'], row['sale_proceeds'],
          row['total_asset_principal_open'], row['total_pending_open'],
          row['total_asset_principal_close'], row['total_pending_close'],
          row['quote_offered'], row['quote_ineligible_reason'], row['quote_price'],
          row['cleanup_exercised'], row['total_debt_at_quote'], row['coverage_gap'],
          row['interest_paid'], row['principal_paid'], row['sponsor_cash'])
         for row in pool_rows])


def _check_sql(db, spec):
    """SQLite-side constraint and reconciliation checks before publication."""
    violations = db.execute('PRAGMA foreign_key_check').fetchall()
    if violations:
        raise ValueError('foreign key violation: ' + str(violations[0]))
    integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
    if integrity != 'ok':
        raise ValueError('integrity check failed: ' + str(integrity))
    horizon = spec['horizon']
    counts = {
        'loan_contracts': db.execute('SELECT count(*) FROM loan_contracts').fetchone()[0],
        'asset_facts': db.execute('SELECT count(*) FROM asset_facts').fetchone()[0],
        'tranche_facts': db.execute('SELECT count(*) FROM tranche_facts').fetchone()[0],
        'pool_facts': db.execute('SELECT count(*) FROM pool_facts').fetchone()[0],
    }
    if counts['asset_facts'] != counts['loan_contracts'] * horizon:
        raise ValueError('asset_facts must cover every loan for every period, zero rows included')
    if counts['tranche_facts'] != 2 * horizon or counts['pool_facts'] != horizon:
        raise ValueError('liability and pool facts must cover every period, zero rows included')
    for view in ('v_loan_principal_conservation', 'v_recovery_conservation'):
        bad = db.execute('SELECT count(*) FROM %s WHERE balanced=0' % view).fetchone()[0]
        if bad:
            raise ValueError(view + ' reported unbalanced rows')
    bad = db.execute('SELECT count(*) FROM v_tranche_rollforward '
                     'WHERE principal_ok=0 OR interest_ok=0').fetchone()[0]
    if bad:
        raise ValueError('tranche rollforward view reported unbalanced rows')
    bad = db.execute('SELECT count(*) FROM v_pool_cash_conservation WHERE balanced=0').fetchone()[0]
    if bad:
        raise ValueError('pool cash conservation view reported unbalanced rows')
    # The granular facts must aggregate exactly to the pool summary.
    drift = db.execute('''
        SELECT count(*) FROM pool_facts p
        JOIN (SELECT period,
                     sum(contract_interest + contract_principal + prepay_principal) AS c,
                     sum(recovery_received) AS r, sum(sale_proceeds) AS s
              FROM asset_facts GROUP BY period) a ON a.period=p.period
        WHERE a.c<>p.contract_cash_in OR a.r<>p.recovery_cash_in OR a.s<>p.sale_proceeds
    ''').fetchone()[0]
    if drift:
        raise ValueError('pool summary disagrees with per-loan asset facts')
    drift = db.execute('''
        SELECT count(*) FROM pool_facts p
        JOIN (SELECT period, sum(interest_paid) AS i, sum(principal_paid) AS pr
              FROM tranche_facts GROUP BY period) t ON t.period=p.period
        WHERE t.i<>p.interest_paid OR t.pr<>p.principal_paid
    ''').fetchone()[0]
    if drift:
        raise ValueError('pool summary disagrees with per-tranche facts')
