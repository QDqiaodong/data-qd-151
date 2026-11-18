DESCRIPTION='Equal-principal loan data schedule and auditable repayment ledger'
import argparse, json, os, sqlite3, sys, tempfile
from pathlib import Path
from .common import read, save
from .model import analyze, contract
from . import award, cdc, ledger, meter_tou, sqlstore, stress, stress_sql, table_keys

COMMANDS=('ledger-init','ledger-import','ledger-report','tranche-build','contract-award','meter-tou','table-keys','cdc-materialize')


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


def meter_tou_report(args):
    """Publish the time-of-use meter energy and local-day billing report."""
    if args.output.is_symlink():
        raise ValueError('output cannot be a symlink: '+str(args.output))
    if args.source.resolve()==args.output.resolve():
        raise ValueError('input and output must differ')
    data,digest=read(args.source)
    spec=meter_tou.validate_input(data)
    report=meter_tou.build_report(spec,digest)
    meter_tou.validate_result(spec,report)
    save(args.output,report)
    return {'schema_version':meter_tou.SCHEMA_VERSION,'command':'meter-tou',
            'output':str(args.output),'dataset_id':spec['dataset_id'],
            'source_sha256':digest,'timezone':spec['timezone'],
            'meter_count':len(spec['meters']),
            'day_count':report['totals']['day_count'],
            'segment_count':report['totals']['segment_count'],
            'cost_exact':report['totals']['cost_exact'],
            'cost_integer':report['totals']['cost_integer']}


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


def table_keys_report(args):
    """Publish the auditable relational candidate-key profile."""
    if args.output.is_symlink():
        raise ValueError('output cannot be a symlink: '+str(args.output))
    if args.source.resolve()==args.output.resolve():
        raise ValueError('input and output must differ')
    data,digest=read(args.source)
    spec=table_keys.validate_input(data)
    found=table_keys.discover_boundaries(spec)
    report=table_keys.build_report(spec,digest,found)
    table_keys.validate_result(spec,report,found['path'])
    save(args.output,report)
    return {'schema_version':table_keys.SCHEMA_VERSION,'command':'table-keys',
            'output':str(args.output),'dataset_id':spec['dataset_id'],
            'source_sha256':digest,'row_count':report['row_count'],
            'minimal_key_count':len(report['minimal_keys']),
            'maximal_nonkey_count':len(report['maximal_nonkeys'])}


def cdc_materialize_report(args):
    """Publish the CDC materialization snapshot and per-transaction audit."""
    if args.output.is_symlink():
        raise ValueError('output cannot be a symlink: '+str(args.output))
    if args.source.resolve()==args.output.resolve():
        raise ValueError('input and output must differ')
    data,digest=read(args.source)
    spec=cdc.validate_input(data)
    material=cdc.materialize(spec)
    report=cdc.build_report(spec,digest,material)
    cdc.validate_result(spec,digest,report)
    save(args.output,report)
    accepted=sum(1 for item in report['transactions'] if item['status']=='accepted')
    rejected=sum(1 for item in report['transactions'] if item['status']=='rejected')
    return {'schema_version':cdc.SCHEMA_VERSION,'command':'cdc-materialize',
            'output':str(args.output),'dataset_id':spec['dataset_id'],
            'source_sha256':digest,'table_count':len(report['tables']),
            'transaction_count':len(report['transactions']),
            'accepted_count':accepted,'rejected_count':rejected}


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

    meter_parser=subparsers.add_parser(
        'meter-tou',
        description='Publish time-of-use meter energy and local-day billing.')
    meter_parser.add_argument('--source',type=Path,required=True)
    meter_parser.add_argument('--output',type=Path,required=True)

    keys_parser=subparsers.add_parser(
        'table-keys',
        description='Publish the relational candidate-key and collision profile.')
    keys_parser.add_argument('--source',type=Path,required=True)
    keys_parser.add_argument('--output',type=Path,required=True)

    cdc_parser=subparsers.add_parser(
        'cdc-materialize',
        description='Materialize an interleaved CDC log into the committed snapshot.')
    cdc_parser.add_argument('--source',type=Path,required=True)
    cdc_parser.add_argument('--output',type=Path,required=True)
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
            elif args.command=='meter-tou':
                summary=meter_tou_report(args)
            elif args.command=='table-keys':
                summary=table_keys_report(args)
            elif args.command=='cdc-materialize':
                summary=cdc_materialize_report(args)
            else:
                raise ValueError('unknown command: '+str(args.command))
    except (ValueError,TypeError,KeyError,OSError,OverflowError,RecursionError) as error:
        print(str(error),file=sys.stderr);return 2
    except Exception as error:
        # Any internal materialization/verification failure must still preserve
        # the previous report and use the structural-failure exit code.
        print('internal error: '+str(error),file=sys.stderr);return 2
    print(json.dumps(summary,sort_keys=True,ensure_ascii=False,allow_nan=False));return 0


def meter_tou_main(argv=None):
    """Console entry point that always dispatches ``meter-tou``."""
    arguments=list(sys.argv[1:] if argv is None else argv)
    return main(['meter-tou',*arguments])


def table_keys_main(argv=None):
    """Console entry point that always dispatches ``table-keys``."""
    arguments=list(sys.argv[1:] if argv is None else argv)
    return main(['table-keys',*arguments])


def contract_award_main(argv=None):
    """Console entry point that always dispatches ``contract-award``.

    ``loan_schedule.cli:main`` chooses the static schedule when the first
    argument is not a subcommand, so a dedicated entry point is needed for a
    script whose arguments start with ``--source``.
    """
    arguments=list(sys.argv[1:] if argv is None else argv)
    return main(['contract-award',*arguments])


def cdc_materialize_main(argv=None):
    """Console entry point that always dispatches ``cdc-materialize``."""
    arguments=list(sys.argv[1:] if argv is None else argv)
    return main(['cdc-materialize',*arguments])


if __name__=='__main__':raise SystemExit(main())
