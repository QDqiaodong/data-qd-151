"""SQLite transaction ownership for the single-loan repayment ledger.

All writes happen inside ``BEGIN IMMEDIATE`` so validation and commit observe
the same predecessor state.  A failure rolls the whole transaction back: no
partial batch, raw version, sequence number or knowledge pointer is visible.
Audit tables are guarded by SQLite triggers against UPDATE/DELETE.
"""
from pathlib import Path
import contextlib, sqlite3

CONTRACT_KEYS=('principal','periods','rate_numerator','rate_denominator')
FORMAT='data-qd-151-ledger/1'

SCHEMA='''
CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;
CREATE TABLE contract (
    id INTEGER PRIMARY KEY CHECK (id=1),
    principal INTEGER NOT NULL CHECK (principal BETWEEN 1 AND 1000000000000),
    periods INTEGER NOT NULL CHECK (periods BETWEEN 1 AND 10000),
    rate_numerator INTEGER NOT NULL CHECK (rate_numerator BETWEEN 0 AND 1000000),
    rate_denominator INTEGER NOT NULL CHECK (rate_denominator BETWEEN 1 AND 1000000000),
    CHECK (periods <= principal)
) STRICT;
CREATE TABLE knowledge (
    seq INTEGER PRIMARY KEY CHECK (seq >= 0),
    status TEXT NOT NULL CHECK (status='committed'),
    source_sha256 TEXT
) STRICT;
CREATE TABLE batches (
    seq INTEGER PRIMARY KEY CHECK (seq >= 1),
    batch_id TEXT NOT NULL UNIQUE CHECK (length(batch_id) BETWEEN 1 AND 128),
    source_sha256 TEXT NOT NULL CHECK (length(source_sha256)=64),
    event_count INTEGER NOT NULL CHECK (event_count BETWEEN 0 AND 20000),
    accepted_at TEXT NOT NULL
) STRICT;
CREATE TABLE raw_versions (
    seq INTEGER NOT NULL REFERENCES batches(seq),
    event_id TEXT NOT NULL,
    rev INTEGER NOT NULL CHECK (rev >= 1),
    op TEXT NOT NULL CHECK (op IN ('upsert','void')),
    kind TEXT CHECK (kind IN ('receipt','reversal')),
    period INTEGER CHECK (period BETWEEN 1 AND 10000),
    amount INTEGER CHECK (amount BETWEEN 1 AND 1000000000000),
    target TEXT,
    PRIMARY KEY (seq,event_id),
    UNIQUE (event_id,rev)
) STRICT;
CREATE TABLE event_meta (
    event_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('receipt','reversal')),
    max_seq INTEGER NOT NULL REFERENCES batches(seq),
    max_rev INTEGER NOT NULL CHECK (max_rev >= 1)
) STRICT;
CREATE INDEX raw_by_rev ON raw_versions(event_id,rev);
CREATE INDEX raw_by_seq ON raw_versions(seq);
CREATE TRIGGER contract_immutable BEFORE UPDATE ON contract
BEGIN SELECT RAISE(ABORT,'contract is immutable'); END;
CREATE TRIGGER contract_no_delete BEFORE DELETE ON contract
BEGIN SELECT RAISE(ABORT,'contract is immutable'); END;
CREATE TRIGGER batch_immutable BEFORE UPDATE ON batches
BEGIN SELECT RAISE(ABORT,'committed batches are immutable'); END;
CREATE TRIGGER batch_no_delete BEFORE DELETE ON batches
BEGIN SELECT RAISE(ABORT,'committed batches are immutable'); END;
CREATE TRIGGER version_immutable BEFORE UPDATE ON raw_versions
BEGIN SELECT RAISE(ABORT,'raw versions are immutable'); END;
CREATE TRIGGER version_no_delete BEFORE DELETE ON raw_versions
BEGIN SELECT RAISE(ABORT,'raw versions are immutable'); END;
CREATE TRIGGER knowledge_immutable BEFORE UPDATE ON knowledge
BEGIN SELECT RAISE(ABORT,'knowledge sequence is append-only'); END;
CREATE TRIGGER knowledge_no_delete BEFORE DELETE ON knowledge
BEGIN SELECT RAISE(ABORT,'knowledge sequence is append-only'); END;
CREATE TRIGGER event_kind_immutable BEFORE UPDATE OF kind ON event_meta
WHEN OLD.kind<>NEW.kind
BEGIN SELECT RAISE(ABORT,'event kind is immutable'); END;
'''


def _statements(script):
    """Split a schema script on complete statements (trigger bodies contain ;)."""
    pending=''
    for line in script.splitlines(keepends=True):
        pending+=line
        if sqlite3.complete_statement(pending):
            yield pending.strip()
            pending=''
    if pending.strip():raise ValueError('incomplete SQL schema statement')


def connect(path,must_exist=False):
    """Open a ledger.  Never creates a database when ``must_exist`` is true."""
    dest=Path(path)
    if must_exist and not dest.is_file():
        raise ValueError('ledger database does not exist: '+str(dest))
    try:
        db=sqlite3.connect(str(dest),isolation_level=None)
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA busy_timeout=5000')
    except sqlite3.Error as error:
        raise ValueError('cannot open ledger database: '+str(error)) from error
    return db


@contextlib.contextmanager
def transaction(db):
    """Write transaction; rolls back on any failure, including commit errors."""
    db.execute('BEGIN IMMEDIATE')
    try:
        yield
    except BaseException:
        _rollback(db)
        raise
    else:
        try:
            db.execute('COMMIT')
        except BaseException:
            _rollback(db)
            raise


@contextlib.contextmanager
def snapshot(db):
    """Read transaction giving every query the same committed view."""
    db.execute('BEGIN')
    try:
        yield
    except BaseException:
        _rollback(db)
        raise
    else:
        try:
            db.execute('COMMIT')
        except sqlite3.Error:
            _rollback(db)
            raise


def _rollback(db):
    try:
        db.execute('ROLLBACK')
    except sqlite3.Error:
        pass


def metadata(db,key):
    row=db.execute('SELECT value FROM metadata WHERE key=?',(key,)).fetchone()
    if row is None:raise ValueError('not a ledger database: missing '+key)
    return row[0]


def load_contract(db):
    row=db.execute('SELECT principal,periods,rate_numerator,rate_denominator FROM contract WHERE id=1').fetchone()
    if row is None:raise ValueError('not a ledger database: missing contract')
    return dict(zip(CONTRACT_KEYS,row))


def latest_seq(db):
    return int(metadata(db,'latest_seq'))


def verify_header(db):
    """Check the format marker and the append-only sequence invariants."""
    try:
        marker=metadata(db,'format')
        latest=latest_seq(db)
        contract=load_contract(db)
        knowledge_max=db.execute('SELECT max(seq) FROM knowledge').fetchone()[0]
        knowledge_count=db.execute('SELECT count(*) FROM knowledge').fetchone()[0]
        batch_max=db.execute('SELECT coalesce(max(seq),0) FROM batches').fetchone()[0]
    except sqlite3.Error as error:
        raise ValueError('not a ledger database: '+str(error)) from error
    if marker!=FORMAT:raise ValueError('unsupported ledger format: '+marker)
    if latest<0:raise ValueError('invalid latest_seq')
    if knowledge_count!=latest+1 or knowledge_max!=latest:
        raise ValueError('knowledge sequence is not contiguous')
    if latest>0 and batch_max!=latest:raise ValueError('batch sequence disagrees with latest_seq')
    return contract


def initialize(db,contract):
    """Create the schema for a brand-new database; seq 0 is empty knowledge."""
    if db.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]:
        raise ValueError('database is not empty')
    for statement in _statements(SCHEMA):
        db.execute(statement)
    db.execute("INSERT INTO metadata(key,value) VALUES('format',?)",(FORMAT,))
    db.execute("INSERT INTO metadata(key,value) VALUES('latest_seq','0')")
    db.execute('INSERT INTO contract(id,principal,periods,rate_numerator,rate_denominator) VALUES(1,?,?,?,?)',
               (contract['principal'],contract['periods'],contract['rate_numerator'],contract['rate_denominator']))
    db.execute('INSERT INTO knowledge(seq,status,source_sha256) VALUES(0,?,NULL)',('committed',))
