import json, os, random, shutil, sqlite3, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
FIXTURES=ROOT/'fixtures/ledger'
SCHEMA_TABLES=('contract','knowledge','batches','raw_versions','event_meta')
LOAN={'principal':100,'periods':3,'rate_numerator':1,'rate_denominator':10}

sys.path.insert(0,str(ROOT/'src'))
from loan_schedule.allocation import allocate, reconcile


def call(*args):
    return subprocess.run([sys.executable,'-m','loan_schedule.cli',*map(str,args)],capture_output=True,text=True)


def dump(db):
    connection=sqlite3.connect(str(db))
    try:
        return {table:connection.execute('SELECT * FROM %s ORDER BY rowid' % table).fetchall()
                for table in SCHEMA_TABLES}
    finally:
        connection.close()


class Ledger(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.dir=Path(self.tmp.name)
        self.db=self.dir/'loan.db'
        self.assertEqual(call('ledger-init','--db',self.db,'--source',FIXTURES/'contract.json').returncode,0)

    def tearDown(self):
        self.tmp.cleanup()

    def batch(self,name,events):
        path=self.dir/(name+'.json')
        path.write_text(json.dumps({'batch_id':name,'events':events}))
        return path

    def imp(self,batch):
        path=batch if isinstance(batch,Path) else self.batch(*batch)
        return call('ledger-import','--db',self.db,'--source',path)

    def ok(self,batch):
        result=self.imp(batch)
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(result.stdout)

    def first(self,name,period,amount):
        return [{'event_id':name,'rev':1,'op':'upsert','kind':'receipt','period':period,'amount':amount}]

    def report(self,seq,through):
        out=self.dir/('report-%d-%d.json'%(seq,through))
        result=call('ledger-report','--db',self.db,'--known-seq',seq,'--through-period',through,'--output',out)
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(out.read_text()),out

    def fail(self,*args):
        before=dump(self.db)
        result=call(*args)
        self.assertEqual(result.returncode,2,result.stdout)
        self.assertTrue(result.stderr.strip())
        self.assertEqual(before,dump(self.db))
        return result

    def test_init_refuses_overwrite_and_is_not_empty(self):
        config=self.dir/'other.json'
        config.write_text(json.dumps(dict(LOAN,principal=200)))
        before=dump(self.db)
        self.fail('ledger-init','--db',self.db,'--source',config)
        self.assertEqual(before,dump(self.db))
        self.assertEqual(self.report(0,1)[0]['loan'],LOAN)

    def test_allocation_example(self):
        self.ok((('receipt',self.first('receipt-1',1,50))))
        self.ok((('reversal',[{'event_id':'reversal-1','rev':1,'op':'upsert','kind':'reversal','period':2,'target':'receipt-1'}])))
        report,_=self.report(2,3)
        self.assertEqual([row['opening_principal'] for row in report['periods']],[100,60,100])
        self.assertEqual([row['interest'] for row in report['periods']],[10,6,10])
        self.assertEqual([row['unpaid_interest'] for row in report['periods']],[0,16,26])
        reversal=report['events'][1]
        self.assertEqual((reversal['interest_applied'],reversal['principal_applied']),(10,40))
        self.assertEqual(report['totals']['remaining_debt'],126)

    def test_correction_retroactively_changes_reversal(self):
        self.test_allocation_example()
        self.ok((('correction',[{'event_id':'receipt-1','rev':2,'op':'upsert','kind':'receipt','period':1,'amount':70}])))
        report,_=self.report(3,3)
        self.assertEqual([row['opening_principal'] for row in report['periods']],[100,40,100])
        self.assertEqual(report['periods'][1]['interest'],4)
        reversal=report['events'][1]
        self.assertEqual((reversal['interest_applied'],reversal['principal_applied']),(10,60))
        self.assertEqual(report['totals']['unpaid_interest'],24)
        old,_=self.report(2,3)
        self.assertEqual(old['events'][0]['amount'],50)
        self.assertEqual(old['totals']['unpaid_interest'],26)

    def test_move_to_later_period_rejects_then_joint_void_commits(self):
        self.test_allocation_example()
        before=dump(self.db)
        moved=self.batch('move',[{'event_id':'receipt-1','rev':2,'op':'upsert','kind':'receipt','period':3,'amount':70}])
        result=call('ledger-import','--db',self.db,'--source',moved)
        self.assertEqual(result.returncode,2)
        self.assertIn('not strictly earlier',result.stderr)
        self.assertEqual(before,dump(self.db))
        joint=self.batch('joint',[{'event_id':'receipt-1','rev':2,'op':'upsert','kind':'receipt','period':3,'amount':70},
                                  {'event_id':'reversal-1','rev':2,'op':'void'}])
        self.assertEqual(call('ledger-import','--db',self.db,'--source',joint).returncode,0)
        report,_=self.report(3,3)
        self.assertEqual([(event['event_id'],event['period']) for event in report['events']],[('receipt-1',3)])
        self.assertEqual(report['totals']['remaining_debt'],60)
        old,_=self.report(2,3)
        self.assertEqual([(event['event_id'],event['period']) for event in old['events']],[('receipt-1',1),('reversal-1',2)])

    def test_same_batch_correction_and_reversal_are_joint(self):
        self.ok((('receipt',self.first('receipt-1',1,50))))
        joint=self.batch('joint',[{'event_id':'receipt-1','rev':2,'op':'upsert','kind':'receipt','period':1,'amount':70},
                                  {'event_id':'reversal-1','rev':1,'op':'upsert','kind':'reversal','period':2,'target':'receipt-1'}])
        self.assertEqual(call('ledger-import','--db',self.db,'--source',joint).returncode,0)
        report,_=self.report(2,3)
        self.assertEqual((report['events'][1]['interest_applied'],report['events'][1]['principal_applied']),(10,60))
        self.assertEqual(report['totals']['unpaid_interest'],24)

    def test_duplicate_batch_keeps_seq_and_changed_bytes_fail(self):
        receipt=self.batch('batch-a',self.first('receipt-1',1,50))
        first=self.ok(receipt)
        self.assertEqual(first['seq'],1)
        duplicate=self.ok(receipt)
        self.assertTrue(duplicate['duplicate'])
        self.assertEqual(duplicate['seq'],1)
        self.assertEqual(len(dump(self.db)['batches']),1)
        receipt.write_text(json.dumps({'batch_id':'batch-a','events':self.first('receipt-1',1,51)}))
        self.fail('ledger-import','--db',self.db,'--source',receipt)
        changed=self.batch('batch-b',self.first('receipt-2',1,51))
        self.assertEqual(self.ok(changed)['seq'],2)

    def test_overpayment_after_correction_rejects(self):
        zero=self.dir/'zero.json'
        zero.write_text(json.dumps({'principal':100,'periods':2,'rate_numerator':0,'rate_denominator':1}))
        db=self.dir/'zero.db'
        self.assertEqual(call('ledger-init','--db',db,'--source',zero).returncode,0)
        path=self.dir/'zero-batch.json'
        path.write_text(json.dumps({'batch_id':'z1','events':[
            {'event_id':'a','rev':1,'op':'upsert','kind':'receipt','period':1,'amount':60},
            {'event_id':'b','rev':1,'op':'upsert','kind':'receipt','period':2,'amount':40}]}))
        self.assertEqual(call('ledger-import','--db',db,'--source',path).returncode,0)
        before=dump(db)
        path.write_text(json.dumps({'batch_id':'z2','events':[
            {'event_id':'a','rev':2,'op':'upsert','kind':'receipt','period':1,'amount':80}]}))
        result=call('ledger-import','--db',db,'--source',path)
        self.assertEqual(result.returncode,2)
        self.assertIn('exceeds outstanding',result.stderr)
        self.assertEqual(before,dump(db))

    def test_revision_and_kind_rules(self):
        self.ok((('receipt',self.first('receipt-1',1,50))))
        for events,message in [
            ([{'event_id':'receipt-1','rev':3,'op':'upsert','kind':'receipt','period':1,'amount':50}],'expected rev 2'),
            ([{'event_id':'receipt-1','rev':2,'op':'upsert','kind':'reversal','period':2,'target':'receipt-1'}],'kind cannot change'),
            ([{'event_id':'fresh','rev':2,'op':'upsert','kind':'receipt','period':1,'amount':5}],'first version must be rev 1'),
            ([{'event_id':'fresh','rev':1,'op':'void'}],'void requires an existing'),
        ]:
            result=self.imp((('bad',events)))
            self.assertEqual(result.returncode,2)
            self.assertIn(message,result.stderr)

    def test_duplicates_in_batch_and_void_revival(self):
        self.ok((('receipt',self.first('receipt-1',1,50))))
        self.assertTrue(self.imp((('dup',self.first('receipt-1',1,50)+self.first('receipt-1',1,60)))).returncode)
        self.assertEqual(self.imp((('dup',self.first('receipt-1',1,50)+self.first('receipt-1',1,60)))).returncode,2)
        self.ok((('void',[{'event_id':'receipt-1','rev':2,'op':'void'}])))
        report,_=self.report(2,3)
        self.assertEqual(report['events'],[])
        self.assertEqual(report['periods'][0]['closing_principal'],100)
        void_again=self.imp((('void-again',[{'event_id':'receipt-1','rev':3,'op':'void'}])))
        self.assertEqual(void_again.returncode,2)
        self.ok((('revive',[{'event_id':'receipt-1','rev':3,'op':'upsert','kind':'receipt','period':1,'amount':30}])))
        report,_=self.report(3,1)
        self.assertEqual(report['events'][0]['rev'],3)
        self.assertEqual(report['periods'][0]['unpaid_interest'],0)

    def test_truncation_uses_latest_versions_then_period(self):
        self.ok((('receipt',self.first('r1',1,50))))
        report,_=self.report(1,3)
        self.assertEqual(len(report['events']),1)
        self.ok((('late',self.first('late-1',3,20))))
        report,_=self.report(2,2)
        self.assertEqual([event['event_id'] for event in report['events']],['r1'])
        self.assertEqual(report['totals']['remaining_debt'],66)
        self.ok((('move',[{'event_id':'r1','rev':2,'op':'upsert','kind':'receipt','period':3,'amount':50}])))
        report,_=self.report(3,2)
        self.assertEqual(report['events'],[])
        self.assertEqual(report['periods'][-1]['unpaid_interest'],20)
        old,_=self.report(2,2)
        self.assertEqual(old['events'][0]['period'],1)

    def test_batches_and_sources_are_listed_up_to_seq(self):
        first=self.ok((('b1',self.first('r1',1,50))))
        second=self.ok((('b2',[])))
        self.assertEqual(second['seq'],2)
        report,_=self.report(1,3)
        self.assertEqual([item['seq'] for item in report['batches']],[1])
        report,_=self.report(2,3)
        self.assertEqual([item['seq'] for item in report['batches']],[1,2])
        self.assertEqual(report['batches'][0]['batch_id'],'b1')
        self.assertEqual(report['batches'][1]['event_count'],0)

    def test_report_is_deterministic_and_preserves_on_failure(self):
        self.ok((('b1',self.first('r1',1,50))))
        first,out=self.report(1,2)
        original=out.read_bytes()
        second,_=self.report(1,2)
        self.assertEqual(original,out.read_bytes())
        result=call('ledger-report','--db',self.db,'--known-seq',9,'--through-period',2,'--output',out)
        self.assertEqual(result.returncode,2)
        self.assertEqual(original,out.read_bytes())
        result=call('ledger-report','--db',self.db,'--known-seq',1,'--through-period',4,'--output',out)
        self.assertEqual(result.returncode,2)
        self.assertEqual(original,out.read_bytes())

    def test_report_rejects_symlink_and_is_not_implicit(self):
        missing=self.dir/'missing.db'
        self.assertEqual(call('ledger-import','--db',missing,'--source',FIXTURES/'batch-01-receipt.json').returncode,2)
        self.assertFalse(missing.exists())
        self.assertEqual(call('ledger-report','--db',missing,'--known-seq',0,'--through-period',1,'--output',self.dir/'x.json').returncode,2)
        self.assertFalse(missing.exists())
        link=self.dir/'link.json'
        link.symlink_to(self.dir/'target.json')
        self.assertEqual(call('ledger-report','--db',self.db,'--known-seq',0,'--through-period',1,'--output',link).returncode,2)

    def test_bad_json_is_rejected(self):
        for body in ['{"batch_id":"x","events":[],"batch_id":"y"}',
                     '{"batch_id":"x","events":[],"extra":1}',
                     '{"batch_id":"x","events":[{"event_id":"a","rev":1,"op":"upsert","kind":"receipt","period":1,"amount":true}]}',
                     '{"batch_id":"x","events":[{"event_id":"a","rev":1,"op":"upsert","kind":"receipt","period":4,"amount":1}]}',
                     '{"batch_id":"x","events":[{"event_id":"a","rev":1,"op":"upsert","kind":"receipt","period":1,"amount":0}]}',
                     '{"batch_id":"x","events":[],"v":Infinity}']:
            path=self.dir/'bad.json';path.write_text(body)
            result=call('ledger-import','--db',self.db,'--source',path)
            self.assertEqual(result.returncode,2,body)
        self.assertEqual(len(dump(self.db)['batches']),0)

    def test_half_up_rounding_and_no_receipts(self):
        tiny=self.dir/'tiny.json'
        tiny.write_text(json.dumps({'principal':1,'periods':1,'rate_numerator':1,'rate_denominator':2}))
        db=self.dir/'tiny.db'
        self.assertEqual(call('ledger-init','--db',db,'--source',tiny).returncode,0)
        out=self.dir/'tiny-report.json'
        self.assertEqual(call('ledger-report','--db',db,'--known-seq',0,'--through-period',1,'--output',out).returncode,0)
        report=json.loads(out.read_text())
        self.assertEqual(report['periods'][0]['exact_interest'],'1/2')
        self.assertEqual(report['periods'][0]['interest'],1)
        self.assertEqual(report['totals']['remaining_debt'],2)
        self.assertEqual(report['events'],[])
        self.assertEqual(report['batches'],[])

    def test_reopen_after_each_process(self):
        self.ok((('b1',self.first('r1',1,50))))
        connection=sqlite3.connect(str(self.db))
        self.assertEqual(connection.execute('SELECT value FROM metadata WHERE key=?',('latest_seq',)).fetchone()[0],'1')
        connection.close()
        reopened=call('ledger-report','--db',self.db,'--known-seq',1,'--through-period',3,'--output',self.dir/'r.json')
        self.assertEqual(reopened.returncode,0,reopened.stderr)
        self.assertEqual(json.loads((self.dir/'r.json').read_text())['totals']['remaining_principal'],60)

    def test_limits_and_no_partial_init(self):
        bad_contract=self.dir/'bad-contract.json'
        bad_contract.write_text(json.dumps({'principal':2,'periods':3,'rate_numerator':1,'rate_denominator':10}))
        db=self.dir/'never.db'
        self.assertEqual(call('ledger-init','--db',db,'--source',bad_contract).returncode,2)
        self.assertFalse(db.exists())
        too_many=[dict(self.first('r%d'%index,1,1)[0]) for index in range(20001)]
        result=self.imp((('huge',too_many)))
        self.assertEqual(result.returncode,2)
        self.assertIn('20000',result.stderr)
        for amount in (0,10**12+1):
            result=self.imp((('amount',self.first('a',1,amount))))
            self.assertEqual(result.returncode,2,amount)
        self.assertEqual(len(dump(self.db)['batches']),0)

    def test_non_ledger_database_is_rejected(self):
        foreign=self.dir/'foreign.db'
        connection=sqlite3.connect(str(foreign))
        connection.execute('CREATE TABLE t(x)');connection.commit();connection.close()
        self.assertEqual(call('ledger-import','--db',foreign,'--source',FIXTURES/'batch-01-receipt.json').returncode,2)
        self.assertEqual(call('ledger-report','--db',foreign,'--known-seq',0,'--through-period',1,
                              '--output',self.dir/'o.json').returncode,2)
        connection=sqlite3.connect(str(foreign))
        self.assertEqual(connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(),[('t',)])
        connection.close()

    def test_immutable_tables_reject_direct_writes(self):
        self.ok((('b1',self.first('r1',1,50))))
        connection=sqlite3.connect(str(self.db))
        statements=["UPDATE raw_versions SET rev=rev","DELETE FROM raw_versions",
                    "UPDATE batches SET batch_id='x'","DELETE FROM batches",
                    "UPDATE contract SET principal=200","DELETE FROM knowledge"]
        for statement in statements:
            with self.assertRaises(sqlite3.DatabaseError,msg=statement):connection.execute(statement)
        connection.close()
        self.assertEqual(dump(self.db)['batches'][0][1],'b1')

    def test_sql_write_failure_rolls_back(self):
        self.ok((('b1',self.first('r1',1,50))))
        connection=sqlite3.connect(str(self.db))
        connection.execute("CREATE TRIGGER fail_batch BEFORE INSERT ON knowledge BEGIN SELECT RAISE(ABORT,'forced'); END")
        connection.commit();connection.close()
        before=dump(self.db)
        path=self.batch('b2',self.first('r2',2,10))
        result=call('ledger-import','--db',self.db,'--source',path)
        self.assertEqual(result.returncode,2)
        self.assertIn('forced',result.stderr)
        self.assertEqual(before,dump(self.db))
        self.assertEqual(self.report(1,3)[0]['events'][0]['event_id'],'r1')

    def test_randomized_invariants(self):
        rng=random.Random(2024)
        for _ in range(60):
            principal=rng.randrange(1,250);periods=rng.randrange(1,min(principal,8)+1)
            contract={'principal':principal,'periods':periods,
                      'rate_numerator':rng.randrange(0,5),'rate_denominator':rng.choice([1,2,7,10])}
            events=[]
            for index in range(rng.randrange(0,7)):
                period=rng.randrange(1,periods+1)
                rows,_=allocate(contract,events)
                outstanding=rows[period-1]['unpaid_interest']+rows[period-1]['closing_principal']
                if not outstanding:continue
                candidate=events+[{'event_id':'r%02d'%index,'rev':1,'kind':'receipt','period':period,
                                   'amount':rng.randrange(1,outstanding+1),'target':None}]
                try:allocate(contract,candidate)
                except ValueError:continue
                events=candidate
            for receipt in list(events):
                if rng.random()<0.4 and receipt['period']<periods:
                    candidate=events+[{'event_id':'v-'+receipt['event_id'],'rev':1,'kind':'reversal',
                                       'period':rng.randrange(receipt['period']+1,periods+1),
                                       'target':receipt['event_id'],'amount':None}]
                    try:allocate(contract,candidate)
                    except ValueError:continue
                    events=candidate
            rows,settled=allocate(contract,events)
            totals=reconcile(contract,rows,settled)
            self.assertTrue(totals['checks']['principal_conservation'])
            self.assertTrue(totals['checks']['balance_identity'])
            cut=rng.randrange(1,periods+1)
            cut_rows,cut_settled=allocate(contract,events,cut)
            cut_totals=reconcile(contract,cut_rows,cut_settled)
            self.assertEqual(cut_totals['remaining_debt'],cut_rows[-1]['remaining_debt'])
            self.assertTrue(cut_totals['checks']['balance_identity'])

    def test_fixture_flow(self):
        db=self.dir/'fixture.db'
        self.assertEqual(call('ledger-init','--db',db,'--source',FIXTURES/'contract.json').returncode,0)
        for name in ('batch-01-receipt.json','batch-02-reversal.json','batch-03-correction.json'):
            result=call('ledger-import','--db',db,'--source',FIXTURES/name)
            self.assertEqual(result.returncode,0,result.stderr)
        out=self.dir/'report.json'
        result=call('ledger-report','--db',db,'--known-seq',3,'--through-period',3,'--output',out)
        self.assertEqual(result.returncode,0,result.stderr)
        report=json.loads(out.read_text())
        self.assertEqual(report['totals']['unpaid_interest'],24)
        self.assertEqual(report['totals']['remaining_principal'],100)
        self.assertFalse(any(isinstance(value,float) for value in self.floats(report)))
        self.assertEqual([item['seq'] for item in report['batches']],[1,2,3])

    def floats(self,value):
        if isinstance(value,float):yield value
        elif isinstance(value,dict):
            for item in value.values():yield from self.floats(item)
        elif isinstance(value,list):
            for item in value:yield from self.floats(item)


if __name__=='__main__':
    unittest.main()
