"""Pure state-allocation engine for the repayment ledger.

The ledger does not apply the static contractual amortisation plan.  Contract
principal is the opening balance at period 1; it only falls when a receipt
actually clears principal, and only rises again when a valid reversal restores
the allocation of the receipt it targets.  Interest accrues every period on the
actual opening principal and is only extinguished by receipts; it never
compounds.  All money stays integral; the only rational value is the exact
period interest retained for audit.
"""
from fractions import Fraction
from .calculation import half_up

def _event_ref(event):
    return "event_id %r rev %d" % (event['event_id'],event['rev'])

def _check_references(events):
    receipts={event['event_id']:event for event in events if event['kind']=='receipt'}
    reversed_receipts={}
    for event in sorted(events,key=lambda item:(item['period'],item['event_id'])):
        if event['kind']!='reversal':continue
        target=event['target']
        receipt=receipts.get(target)
        if receipt is None:
            raise ValueError('%s in period %d: reversal target %r is not an active receipt' % (_event_ref(event),event['period'],target))
        if receipt['period']>=event['period']:
            raise ValueError('%s in period %d: reversal target %r has period %d, which is not strictly earlier' % (_event_ref(event),event['period'],target,receipt['period']))
        if target in reversed_receipts:
            raise ValueError('%s in period %d: receipt %r already has an active reversal %r' % (_event_ref(event),event['period'],target,reversed_receipts[target]))
        reversed_receipts[target]=event['event_id']

def allocate(contract,events,through_period=None):
    """Recompute period states and per-event allocations.

    ``events`` are the effective (post-revision, non-void) event versions.
    Events after ``through_period`` are ignored and must not be referenced by
    earlier events.  Returns ``(periods, settled)`` in deterministic settlement
    order: per period, reversals then receipts, each group by event_id code
    point order, with interest accrued before any event of the period.

    ``settled`` covers every period of the loan, not only the cutoff, so a
    reversal's allocation is always derived from its target receipt as
    recomputed at the final knowledge version, never copied from an earlier
    run.  The rows in ``periods`` stop at ``through_period``.
    """
    periods=contract['periods'];numerator=contract['rate_numerator'];denominator=contract['rate_denominator']
    limit=periods if through_period is None else through_period
    if type(limit) is not int or not 1<=limit<=periods:
        raise ValueError('through period must be within 1..%d' % periods)
    active=[event for event in events if event['period']<=periods]
    _check_references(active)
    by_period={}
    for event in active:by_period.setdefault(event['period'],[]).append(event)
    principal=contract['principal'];unpaid=0
    cumulative_interest=0;cumulative_net=0
    allocations={};period_rows=[];settled=[]
    for period in range(1,periods+1):
        opening=principal
        exact=Fraction(opening*numerator,denominator)
        interest=half_up(exact)
        unpaid+=interest
        receipt_amount=receipt_interest=receipt_principal=0
        reversal_interest=reversal_principal=0
        top=by_period.get(period,[])
        reversals=sorted((event for event in top if event['kind']=='reversal'),key=lambda event:event['event_id'])
        receipts=sorted((event for event in top if event['kind']=='receipt'),key=lambda event:event['event_id'])
        for event in reversals:
            refund_interest,refund_principal=allocations[event['target']]
            unpaid+=refund_interest
            principal+=refund_principal
            allocations[event['event_id']]=(refund_interest,refund_principal)
            reversal_interest+=refund_interest
            reversal_principal+=refund_principal
            settled.append({'event_id':event['event_id'],'rev':event['rev'],'kind':'reversal','period':period,
                            'amount':None,'target':event['target'],
                            'interest_applied':refund_interest,'principal_applied':refund_principal})
        for event in receipts:
            amount=event['amount'];outstanding=unpaid+principal
            if amount>outstanding:
                raise ValueError('%s in period %d: receipt %d exceeds outstanding interest %d + principal %d' % (_event_ref(event),period,amount,unpaid,principal))
            applied_interest=min(amount,unpaid);applied_principal=amount-applied_interest
            unpaid-=applied_interest
            principal-=applied_principal
            allocations[event['event_id']]=(applied_interest,applied_principal)
            receipt_amount+=amount
            receipt_interest+=applied_interest
            receipt_principal+=applied_principal
            settled.append({'event_id':event['event_id'],'rev':event['rev'],'kind':'receipt','period':period,
                            'amount':amount,'target':None,
                            'interest_applied':applied_interest,'principal_applied':applied_principal})
        cumulative_interest+=interest
        cumulative_net+=receipt_amount-reversal_interest-reversal_principal
        if period<=limit:
            period_rows.append({'period':period,'opening_principal':opening,'exact_interest':str(exact),
                                'interest':interest,'unpaid_interest':unpaid,'closing_principal':principal,
                                'cumulative_interest':cumulative_interest,
                                'cumulative_net_receipts':cumulative_net,
                                'remaining_debt':principal+unpaid,
                                'receipts':{'count':len(receipts),'amount':receipt_amount,
                                            'interest_applied':receipt_interest,'principal_applied':receipt_principal},
                                'reversals':{'count':len(reversals),'interest_refunded':reversal_interest,
                                             'principal_refunded':reversal_principal}})
    if limit<periods:
        settled=[event for event in settled if event['period']<=limit]
    return period_rows,settled

def reconcile(contract,period_rows,settled):
    """Verify principal conservation and the reporting identity at the cutoff."""
    initial=contract['principal']
    interest_accrued=sum(row['interest'] for row in period_rows)
    receipts=[event for event in settled if event['kind']=='receipt']
    reversals=[event for event in settled if event['kind']=='reversal']
    receipts_total=sum(event['amount'] for event in receipts)
    reversal_total=sum(event['interest_applied']+event['principal_applied'] for event in reversals)
    net_receipts=receipts_total-reversal_total
    receipt_principal=sum(event['principal_applied'] for event in receipts)
    reversal_principal=sum(event['principal_applied'] for event in reversals)
    remaining_principal=period_rows[-1]['closing_principal']
    unpaid_interest=period_rows[-1]['unpaid_interest']
    principal_ok=remaining_principal==initial-receipt_principal+reversal_principal
    identity_ok=initial+interest_accrued-net_receipts==remaining_principal+unpaid_interest
    chain_ok=period_rows[0]['opening_principal']==initial and all(
        period_rows[index]['opening_principal']==period_rows[index-1]['closing_principal']
        for index in range(1,len(period_rows)))
    if not (principal_ok and identity_ok and chain_ok):
        raise ValueError('internal reconciliation failure')
    return {'initial_principal':initial,'interest_accrued':interest_accrued,
            'receipts_total':receipts_total,'reversal_refunds_total':reversal_total,
            'net_receipts':net_receipts,'receipt_principal':receipt_principal,
            'reversal_principal':reversal_principal,'remaining_principal':remaining_principal,
            'unpaid_interest':unpaid_interest,'remaining_debt':remaining_principal+unpaid_interest,
            'checks':{'principal_conservation':principal_ok,'balance_identity':identity_ok,
                      'period_chain':chain_ok}}
