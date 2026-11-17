
import copy,json,unittest,tempfile,subprocess,sys,hashlib,random,math
from pathlib import Path
from fractions import Fraction
from loan_schedule.model import analyze
ROOT=Path(__file__).resolve().parents[1]
def sample():return json.loads((ROOT/'fixtures/input.json').read_text())
class Business(unittest.TestCase):
    def bad(self,d):
        with self.assertRaises((ValueError,TypeError)):analyze(d)
    def test_unknown_fields(self):
        d=sample();d['extra']=1;self.bad(d)

    def test_schedule(self):
        r=analyze(sample());self.assertEqual([x['principal'] for x in r['schedule']],[333,333,334]);self.assertEqual([x['interest'] for x in r['schedule']],[10,7,3]);self.assertEqual(r['total_payment'],1020)
    def test_half_up_tie(self):
        d={'principal':1,'periods':1,'rate_numerator':1,'rate_denominator':2};r=analyze(d);self.assertEqual(r['total_interest'],1);self.assertEqual(r['schedule'][0]['exact_interest'],'1/2')
    def test_zero_interest(self):
        d=sample();d['rate_numerator']=0;self.assertEqual(analyze(d)['total_payment'],1000)
    def test_minor_unit_bound(self):
        d=sample();d['periods']=1001;self.bad(d)
    def test_boolean(self):
        d=sample();d['principal']=True;self.bad(d)
    def test_denominator(self):
        d=sample();d['rate_denominator']=0;self.bad(d)
    def test_random_principal_conservation(self):
        rng=random.Random(14)
        for _ in range(100):
            principal=rng.randrange(1,10000);periods=rng.randrange(1,min(principal,100)+1);r=analyze({'principal':principal,'periods':periods,'rate_numerator':1,'rate_denominator':97});self.assertEqual(r['total_principal'],principal);self.assertEqual(r['schedule'][-1]['closing'],0)
            for row in r['schedule']:self.assertEqual(row['opening']-row['principal'],row['closing'])

class Command(unittest.TestCase):
    def call(self,src,out):
        return subprocess.run([sys.executable,'-m','loan_schedule.cli','--source',str(src),'--output',str(out)],capture_output=True,text=True)
    def test_cli_replay(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)/'out.json';src=ROOT/'fixtures/input.json';r=self.call(src,out);self.assertEqual(r.returncode,0,r.stderr);first=out.read_bytes();self.assertEqual(json.loads(first)['source_sha256'],hashlib.sha256(src.read_bytes()).hexdigest());self.assertEqual(self.call(src,out).returncode,0);self.assertEqual(first,out.read_bytes())
    def test_failure_preserves(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'bad.json';out=Path(td)/'out.json';out.write_text('prior')
            for body in ['{}','{"a":1,"a":2}','NaN']:
                src.write_text(body);self.assertEqual(self.call(src,out).returncode,2);self.assertEqual(out.read_text(),'prior')
    def test_output_safety(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'in.json';src.write_bytes((ROOT/'fixtures/input.json').read_bytes());before=src.read_bytes();self.assertEqual(self.call(src,src).returncode,2);self.assertEqual(src.read_bytes(),before);out=Path(td)/'link.json';out.symlink_to(src);self.assertEqual(self.call(src,out).returncode,2);self.assertEqual(src.read_bytes(),before)
