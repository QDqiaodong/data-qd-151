DESCRIPTION='Equal-principal loan data schedule and auditable repayment ledger'
import argparse, json, os, sqlite3, sys, tempfile
from pathlib import Path
from .common import read, save
from .model import analyze, contract
from . import award, ledger, sqlstore, stress, stress_sql

COMMANDS=('ledger-init','ledger-import','ledger-report','tranche-build','contract-award')


def run(source):
    data,digest=read(source)
    return {'schema_version':1,'source_sha256':digest,'result':analyze(data)}


def ledger_init(args):
    """Create the ledger atomically: a failed init never leaves a database."""
    data,_=read(args.source)
    values=contract(data)
    if args.db.exists() or args.db.is_symlink():
        raise ValueError('ledger database already exists: '+str(args.db))
    args.db.parent.mkdir(parents=True,exist_ok=True)
    handle,temp=tempfile.mkstemp(prefix='.ledger-init-',suffix='.db',dir=args.db.parent)
    os.close(handle)
    try:
        db=sqlstore.connect(temp)
        try:
            with sqlstore.transaction(db):
                sqlstore.initialize(db,values)
        finally:
            db.close()
        payload=Path(temp).read_bytes()
        try:
            with args.db.open('xb') as stream:stream.write(payload)
        except FileExistsError:
            raise ValueError('ledger database already exists: '+str(args.db)) from None
        except OSError:
            if args.db.exists():args.db.unlink()
            raise
    except sqlite3.Error as error:
        raise ValueError('ledger initialization failed: '+str(error)) from error
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return {'schema_version':2,'command':'ledger-init','db':str(args.db),
            'contract':values,'latest_seq':0}


def ledger_import(args):
    data,digest=read(args.source)
    db=sqlstore.connect(args.db,must_exist=True)
    try:
        with sqlstore.transaction(db):
            sqlstore.verify_header(db)
            result=ledger.validate_and_commit(db,data,digest)
    except sqlite3.Error as error:
        raise ValueError('ledger import failed: '+str(error)) from error
    finally:
        db.close()
    return {'schema_version':2,'command':'ledger-import',**result}


def ledger_report(args):
    db=sqlstore.connect(args.db,must_exist=True)
    try:
        with sqlstore.snapshot(db):
            sqlstore.verify_header(db)
            latest=sqlstore.latest_seq(db)
            if not 0<=args.known_seq<=latest:
                raise ValueError('known_seq must be within 0..%d' % latest)
            periods=sqlstore.load_contract(db)['periods']
            if not 1<=args.through_period<=periods:
                raise ValueError('through_period must be within 1..%d' % periods)
            result=ledger.build_snapshot(db,args.known_seq,args.through_period)
    except sqlite3.Error as error:
        raise ValueError('ledger report failed: '+str(error)) from error
    finally:
        db.close()
    return result


def tranche_build(args):
    """Build the standalone stressed cash-flow SQLite dataset atomically."""
    if args.output.is_symlink():
        raise ValueError('output cannot be a symlink: '+str(args.output))
    if args.source.resolve()==args.output.resolve():
        raise ValueError('input and output must differ')
    data,digest=read(args.source)
    spec=stress.validate_input(data)
    asset_rows,tranche_rows,pool_rows=stress.build_scenario(spec)
    try:
        temp=stress_sql.build_database(args.output,spec,asset_rows,tranche_rows,pool_rows,digest)
        try:
            stress_sql.publish(temp,args.output)
        except OSError:
            if os.path.exists(temp):os.unlink(temp)
            raise
    except sqlite3.Error as error:
        raise ValueError('tranche-build failed: '+str(error)) from error
    exercised=[row['period'] for row in pool_rows if row['cleanup_exercised']]
    return {'schema_version':stress_sql.SCHEMA_VERSION,'command':'tranche-build',
            'db':str(args.output),'pool_id':spec['pool_id'],
            'source_sha256':digest,'horizon':spec['horizon'],
            'loan_count':len(spec['loans']),'initial_principal':spec['initial_principal'],
            'cleanup_period':exercised[0] if exercised else None,
            'final_senior_principal':tranche_rows[-2]['closing_principal'],
            'final_junior_principal':tranche_rows[-1]['closing_principal'],
            'final_senior_unpaid_interest':tranche_rows[-2]['closing_unpaid_interest'],
            'final_junior_unpaid_interest':tranche_rows[-1]['closing_unpaid_interest']}


def contract_award(args):
    """Publish the auditable lot-award dataset without touching other paths."""
    if args.output.is_symlink():
        raise ValueError('output cannot be a symlink: '+str(args.output))
    if args.source.resolve()==args.output.resolve():
        raise ValueError('input and output must differ')
    data,digest=read(args.source)
    spec=award.validate_input(data)
    result=award.optimize(spec)
    report=award.build_report(spec,result,digest)
    award.validate_result(spec,report)
    save(args.output,report)
    return {'schema_version':award.SCHEMA_VERSION,'command':'contract-award',
            'output':str(args.output),'procurement_id':spec['procurement_id'],
            'source_sha256':digest,
            'awarded_lot_count':report['objective']['awarded_lot_count'],
            'awarded_total_amount':report['objective']['awarded_total_amount']}


def ledger_parser():
    parser=argparse.ArgumentParser(description=DESCRIPTION)
    subparsers=parser.add_subparsers(dest='command',required=True)

    init_parser=subparsers.add_parser('ledger-init',description='Create an immutable single-loan fact ledger.')
    init_parser.add_argument('--db',type=Path,required=True)
    init_parser.add_argument('--source',type=Path,required=True)

    import_parser=subparsers.add_parser('ledger-import',description='Import one repayment fact batch.')
    import_parser.add_argument('--db',type=Path,required=True)
    import_parser.add_argument('--source',type=Path,required=True)

    report_parser=subparsers.add_parser('ledger-report',description='Publish a knowledge-time debt report.')
    report_parser.add_argument('--db',type=Path,required=True)
    report_parser.add_argument('--known-seq',type=int,required=True,dest='known_seq')
    report_parser.add_argument('--through-period',type=int,required=True,dest='through_period')
    report_parser.add_argument('--output',type=Path,required=True)

    build_parser=subparsers.add_parser(
        'tranche-build',
        description='Build a standalone stressed cash-flow SQLite dataset.')
    build_parser.add_argument('--source',type=Path,required=True)
    build_parser.add_argument('--output',type=Path,required=True)

    award_parser=subparsers.add_parser(
        'contract-award',
        description='Publish an auditable lot-award dataset.')
    award_parser.add_argument('--source',type=Path,required=True)
    award_parser.add_argument('--output',type=Path,required=True)
    return parser


def static_parser():
    parser=argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    return parser


def main(argv=None):
    argv=list(sys.argv[1:] if argv is None else argv)
    command=argv[0] if argv and argv[0] in COMMANDS else None
    try:
        if command is None:
            args=static_parser().parse_args(argv)
            if args.source.resolve()==args.output.resolve():
                raise ValueError('input and output must differ')
            summary=run(args.source);save(args.output,summary)
        else:
            args=ledger_parser().parse_args(argv)
            if args.command=='ledger-init':
                summary=ledger_init(args)
            elif args.command=='ledger-import':
                summary=ledger_import(args)
            elif args.command=='ledger-report':
                if args.output.is_symlink():raise ValueError('output cannot be a symlink')
                if args.output.resolve()==args.db.resolve():
                    raise ValueError('output must differ from the ledger database')
                summary=ledger_report(args);save(args.output,summary)
            elif args.command=='tranche-build':
                summary=tranche_build(args)
            elif args.command=='contract-award':
                summary=contract_award(args)
            else:
                raise ValueError('unknown command: '+str(args.command))
    except (ValueError,TypeError,KeyError,OSError,OverflowError,RecursionError) as error:
        print(str(error),file=sys.stderr);return 2
    print(json.dumps(summary,sort_keys=True,ensure_ascii=False,allow_nan=False));return 0


def contract_award_main(argv=None):
    """Console entry point that always dispatches ``contract-award``.

    ``loan_schedule.cli:main`` chooses the static schedule when the first
    argument is not a subcommand, so a dedicated entry point is needed for a
    script whose arguments start with ``--source``.
    """
    arguments=list(sys.argv[1:] if argv is None else argv)
    return main(['contract-award',*arguments])


if __name__=='__main__':raise SystemExit(main())
