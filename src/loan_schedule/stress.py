"""Stressed cash-flow engine for the securitisation tranche dataset.

The engine never invents a receipt.  Contractual cash is recomputed from the
same four-field contract and the same equal-principal / nonnegative half-up
rules as the static schedule (``loan_schedule.calculation.schedule``); the
scenario then decides which of those contractual flows actually happen
(performance, opening default with lagged recovery, period-end prepay or a
cleanup sale).  Every amount stays an integral minor unit; the only rational
arithmetic uses :class:`fractions.Fraction` and is rounded once, half-up, at
the points the contract defines (period interest, default recovery, per-loan
sale price).
"""
from fractions import Fraction

from .calculation import half_up, schedule
from .common import ident, only, require
from .model import contract as validate_contract

TOP_KEYS = 'pool_id horizon loans exits senior junior reserve cleanup'
LOAN_KEYS = 'loan_id contract'
PREPAY_KEYS = 'loan_id kind period'
DEFAULT_KEYS = 'loan_id kind period recovery_numerator recovery_denominator recovery_lag'
TRANCHE_KEYS = 'principal rate_numerator rate_denominator'
CLEANUP_KEYS = ('threshold_numerator', 'threshold_denominator',
                'price_numerator', 'price_denominator')
INT_MAX = 10 ** 9
BIG_MAX = 10 ** 12
CLEANUP_NUM_MAX = 2 * 10 ** 9
HORIZON_MAX = 48
MAX_LOANS = 2000
MAX_CONTRACT_PERIODS = 20000


def _integer(value, minimum, maximum, field):
    if type(value) is not int:
        raise ValueError('%s must be an integer (booleans are rejected)' % field)
    if not minimum <= value <= maximum:
        raise ValueError('%s must be within %d..%d' % (field, minimum, maximum))
    return value


def _fraction_term(value, keys, label):
    only(value, ' '.join(keys))
    missing = set(keys) - set(value)
    if missing:
        raise ValueError('%s requires %s' % (label, ', '.join(sorted(missing))))
    return {key: value[key] for key in keys}


def validate_input(data):
    """Parse and cross-check the strict stress scenario object."""
    require(data, TOP_KEYS)
    pool_id = ident(data['pool_id'])
    horizon = _integer(data['horizon'], 1, HORIZON_MAX, 'horizon')

    raw_loans = data['loans']
    if not isinstance(raw_loans, list) or not 1 <= len(raw_loans) <= MAX_LOANS:
        raise ValueError('loans must be an array of 1..%d items' % MAX_LOANS)
    loans = []
    seen = set()
    period_sum = 0
    for index, item in enumerate(raw_loans):
        where = 'loans[%d]' % index
        if not isinstance(item, dict):
            raise ValueError(where + ' must be an object')
        only(item, LOAN_KEYS)
        loan_id = ident(item['loan_id'])
        if loan_id in seen:
            raise ValueError('duplicate loan_id: ' + loan_id)
        seen.add(loan_id)
        terms = validate_contract(item['contract'])
        if terms['periods'] > horizon:
            raise ValueError('%s contract periods %d exceed horizon %d'
                             % (loan_id, terms['periods'], horizon))
        if terms['rate_numerator'] > terms['rate_denominator']:
            raise ValueError('%s per-period rate must be <= 1' % loan_id)
        period_sum += terms['periods']
        loans.append({'loan_id': loan_id, 'ordinal': index, **terms})
    if period_sum > MAX_CONTRACT_PERIODS:
        raise ValueError('sum of contract periods exceeds %d' % MAX_CONTRACT_PERIODS)
    initial_principal = sum(loan['principal'] for loan in loans)
    if initial_principal > BIG_MAX:
        raise ValueError('aggregate loan principal exceeds %d' % BIG_MAX)
    loans.sort(key=lambda loan: loan['loan_id'])

    exits = _validate_exits(data['exits'], loans, horizon)

    senior = _validate_tranche(data['senior'], 'senior')
    junior = _validate_tranche(data['junior'], 'junior')
    if senior['principal'] + junior['principal'] != initial_principal:
        raise ValueError('tranche principal total must equal aggregate loan principal %d'
                         % initial_principal)
    reserve = _integer(data['reserve'], 0, initial_principal, 'reserve')

    cleanup_raw = data['cleanup']
    if not isinstance(cleanup_raw, dict):
        raise ValueError('cleanup must be an object')
    only(cleanup_raw, ' '.join(CLEANUP_KEYS))
    missing = set(CLEANUP_KEYS) - set(cleanup_raw)
    if missing:
        raise ValueError('cleanup requires ' + ', '.join(sorted(missing)))
    cleanup = {key: cleanup_raw[key] for key in CLEANUP_KEYS}
    _integer(cleanup['threshold_denominator'], 1, INT_MAX, 'threshold_denominator')
    _integer(cleanup['price_denominator'], 1, INT_MAX, 'price_denominator')
    _integer(cleanup['threshold_numerator'], 0, CLEANUP_NUM_MAX, 'threshold_numerator')
    _integer(cleanup['price_numerator'], 0, CLEANUP_NUM_MAX, 'price_numerator')
    if cleanup['threshold_numerator'] > cleanup['threshold_denominator']:
        raise ValueError('cleanup threshold must be within 0..1')
    if cleanup['price_numerator'] > 2 * cleanup['price_denominator']:
        raise ValueError('cleanup price ratio must be within 0..2')

    return {'pool_id': pool_id, 'horizon': horizon, 'loans': loans,
            'exits': exits, 'senior': senior, 'junior': junior,
            'reserve': reserve, 'cleanup': cleanup,
            'initial_principal': initial_principal}


def _validate_tranche(raw, label):
    if not isinstance(raw, dict):
        raise ValueError(label + ' must be an object')
    terms = _fraction_term(raw, TRANCHE_KEYS.split(), label)
    _integer(terms['principal'], 0, BIG_MAX, label + '.principal')
    _integer(terms['rate_numerator'], 0, 10 ** 6, label + '.rate_numerator')
    _integer(terms['rate_denominator'], 1, INT_MAX, label + '.rate_denominator')
    if terms['rate_numerator'] > terms['rate_denominator']:
        raise ValueError(label + ' per-period rate must be <= 1')
    return terms


def _validate_exits(raw_exits, loans, horizon):
    if not isinstance(raw_exits, list):
        raise ValueError('exits must be an array')
    by_loan = {loan['loan_id']: loan for loan in loans}
    exits = {}
    for index, item in enumerate(raw_exits):
        where = 'exits[%d]' % index
        if not isinstance(item, dict):
            raise ValueError(where + ' must be an object')
        kind = item.get('kind')
        keys = PREPAY_KEYS if kind == 'prepay' else DEFAULT_KEYS if kind == 'default' else None
        if keys is None:
            raise ValueError(where + ' kind must be prepay or default')
        only(item, keys)
        missing = set(keys.split()) - set(item)
        if missing:
            raise ValueError(where + ' requires ' + ', '.join(sorted(missing)))
        loan_id = ident(item['loan_id'])
        if loan_id not in by_loan:
            raise ValueError(where + ' references unknown loan_id: ' + loan_id)
        if loan_id in exits:
            raise ValueError('loan_id %s has more than one exit' % loan_id)
        periods = by_loan[loan_id]['periods']
        period = _integer(item['period'], 1, periods, where + '.period')
        if kind == 'prepay':
            exits[loan_id] = {'loan_id': loan_id, 'kind': 'prepay', 'period': period,
                              'recovery_numerator': None, 'recovery_denominator': None,
                              'recovery_lag': None}
            continue
        rnum = _integer(item['recovery_numerator'], 0, INT_MAX, where + '.recovery_numerator')
        rden = _integer(item['recovery_denominator'], 1, INT_MAX, where + '.recovery_denominator')
        if rnum > rden:
            raise ValueError(where + ' recovery fraction must be within 0..1')
        lag = _integer(item['recovery_lag'], 1, HORIZON_MAX, where + '.recovery_lag')
        if period + lag > horizon:
            raise ValueError(where + ': default period + recovery_lag exceeds horizon')
        exits[loan_id] = {'loan_id': loan_id, 'kind': 'default', 'period': period,
                          'recovery_numerator': rnum, 'recovery_denominator': rden,
                          'recovery_lag': lag}
    return exits


def _empty_asset_row(loan_id, period):
    return {'loan_id': loan_id, 'period': period, 'opening_principal': 0,
            'contract_interest': 0, 'contract_principal': 0, 'prepay_principal': 0,
            'defaulted_principal': 0, 'asset_loss': 0, 'recovery_recognized': 0,
            'recovery_received': 0, 'pending_open': 0, 'pending_close': 0,
            'sold_principal': 0, 'canceled_recovery': 0, 'sale_proceeds': 0,
            'closing_principal': 0, 'asset_cash_in': 0, 'status': 'closed'}


def build_scenario(spec):
    """Simulate every period 1..horizon and return all fact rows.

    Returns ``(asset_rows, tranche_rows, pool_rows)`` where rows are already in
    deterministic order (loan_id / tranche rank / period).  The function
    reconciles every conservation identity before returning and raises
    ``ValueError`` on any internal inconsistency, so a partially computed
    dataset can never be published.
    """
    horizon = spec['horizon']
    loans = spec['loans']
    exits = spec['exits']
    target_reserve = spec['reserve']
    cleanup = spec['cleanup']
    initial_principal = spec['initial_principal']

    plans = {loan['loan_id']: schedule(
        loan['principal'], loan['periods'],
        loan['rate_numerator'], loan['rate_denominator'])['schedule']
        for loan in loans}
    state = {loan['loan_id']: {
        'balance': loan['principal'], 'pending': 0, 'pending_due': None,
        'status': 'active'} for loan in loans}

    tranches = [('senior', dict(spec['senior'])), ('junior', dict(spec['junior']))]
    debt = {name: {'principal': terms['principal'], 'unpaid': 0,
                   'num': terms['rate_numerator'], 'den': terms['rate_denominator']}
            for name, terms in tranches}
    reserve = target_reserve
    cleanup_done = False
    asset_rows = []
    tranche_rows = []
    pool_rows = []

    for period in range(1, horizon + 1):
        period_assets = []
        if cleanup_done:
            for loan in loans:
                row = _empty_asset_row(loan['loan_id'], period)
                row['status'] = state[loan['loan_id']]['status']
                period_assets.append(row)
            pool_rows.append(_empty_pool_row(period, reserve))
            for name, _ in tranches:
                tranche_rows.append(_empty_tranche_row(name, period))
            asset_rows.extend(period_assets)
            continue

        contract_cash = 0
        recovery_cash = 0
        for loan in loans:
            loan_id = loan['loan_id']
            st = state[loan_id]
            row = _empty_asset_row(loan_id, period)
            row['opening_principal'] = st['balance']
            row['pending_open'] = st['pending']
            exit_event = exits.get(loan_id)
            if st['status'] == 'active':
                opening = st['balance']
                planned = plans[loan_id][period - 1]
                if opening != planned['opening']:
                    raise ValueError('internal schedule divergence for loan ' + loan_id)
                if exit_event is not None and exit_event['period'] == period:
                    if exit_event['kind'] == 'default':
                        recovery = half_up(Fraction(
                            opening * exit_event['recovery_numerator'],
                            exit_event['recovery_denominator']))
                        row['defaulted_principal'] = opening
                        row['recovery_recognized'] = recovery
                        row['asset_loss'] = opening - recovery
                        st['pending'] = recovery
                        st['pending_due'] = period + exit_event['recovery_lag']
                        st['balance'] = 0
                        st['status'] = 'defaulted'
                    else:
                        row['contract_interest'] = planned['interest']
                        row['prepay_principal'] = opening
                        st['balance'] = 0
                        st['status'] = 'prepaid'
                else:
                    row['contract_interest'] = planned['interest']
                    row['contract_principal'] = planned['principal']
                    st['balance'] = opening - planned['principal']
                    if st['balance'] == 0:
                        st['status'] = 'closed'
            if st['pending'] and st['pending_due'] == period:
                row['recovery_received'] = st['pending']
                recovery_cash += st['pending']
                st['pending'] = 0
                st['pending_due'] = None
            contract_cash += row['contract_interest'] + row['contract_principal'] + row['prepay_principal']
            row['status'] = st['status']
            row['closing_principal'] = st['balance']
            row['pending_close'] = st['pending']
            row['asset_cash_in'] = (row['contract_interest'] + row['contract_principal']
                                    + row['prepay_principal'] + row['recovery_received'])
            period_assets.append(row)

        # Liability accrual happens on opening principal, before any payment.
        accrued = {}
        opening = {}
        for name, _ in tranches:
            item = debt[name]
            opening[name] = (item['principal'], item['unpaid'])
            amount = half_up(Fraction(item['principal'] * item['num'], item['den']))
            item['unpaid'] += amount
            accrued[name] = amount

        opening_reserve = reserve
        total_normal = sum(st['balance'] for st in state.values())
        total_pending = sum(st['pending'] for st in state.values())
        quote_price = 0
        quote_offered = 0
        reason = None
        per_loan_price = {}
        if total_normal + total_pending == 0:
            reason = 'no_saleable_assets'
        elif total_normal * cleanup['threshold_denominator'] > \
                initial_principal * cleanup['threshold_numerator']:
            reason = 'above_threshold'
        else:
            quote_offered = 1
            for loan in loans:
                st = state[loan['loan_id']]
                base = st['balance'] + st['pending']
                price = half_up(Fraction(base * cleanup['price_numerator'],
                                         cleanup['price_denominator'])) if base else 0
                per_loan_price[loan['loan_id']] = price
                quote_price += price

        total_debt = sum(item['principal'] + item['unpaid'] for item in debt.values())
        period_cash = contract_cash + recovery_cash
        coverage_gap = (total_debt - period_cash - opening_reserve - quote_price
                        if quote_offered else 0)
        exercised = 1 if quote_offered and coverage_gap <= 0 else 0

        pool = {'period': period, 'opening_reserve': opening_reserve,
                'reserve_used': 0, 'reserve_replenished': 0, 'reserve_released': 0,
                'closing_reserve': opening_reserve,
                'contract_cash_in': contract_cash, 'recovery_cash_in': recovery_cash,
                'sale_proceeds': 0, 'total_asset_principal_open': sum(
                    row['opening_principal'] for row in period_assets),
                'total_pending_open': sum(row['pending_open'] for row in period_assets),
                'quote_offered': quote_offered, 'quote_ineligible_reason': reason,
                'quote_price': quote_price, 'cleanup_exercised': exercised,
                'total_debt_at_quote': total_debt if quote_offered else 0,
                'coverage_gap': coverage_gap, 'interest_paid': 0,
                'principal_paid': 0, 'sponsor_cash': 0}

        pay_log = {name: {'interest_paid': 0, 'principal_paid': 0} for name, _ in tranches}
        if exercised:
            cleanup_done = True
            sale_total = 0
            for row in period_assets:
                st = state[row['loan_id']]
                row['sold_principal'] = st['balance']
                row['canceled_recovery'] = st['pending']
                row['sale_proceeds'] = per_loan_price[row['loan_id']]
                sale_total += row['sale_proceeds']
                if row['sold_principal'] or row['canceled_recovery']:
                    st['status'] = 'sold'
                st['balance'] = 0
                st['pending'] = 0
                st['pending_due'] = None
                row['closing_principal'] = 0
                row['pending_close'] = 0
                row['asset_cash_in'] += row['sale_proceeds']
            if sale_total != quote_price:
                raise ValueError('internal cleanup price mismatch')
            pool['sale_proceeds'] = quote_price
            funds = period_cash + opening_reserve + quote_price
            pool['reserve_used'] = opening_reserve
            reserve = 0
            for name, _ in tranches:
                item = debt[name]
                pay_log[name]['interest_paid'] = item['unpaid']
                item['unpaid'] = 0
            for name, _ in tranches:
                item = debt[name]
                pay_log[name]['principal_paid'] = item['principal']
                item['principal'] = 0
            paid = sum(entry['interest_paid'] + entry['principal_paid']
                       for entry in pay_log.values())
            if paid > funds:
                raise ValueError('internal cleanup coverage mismatch')
            pool['sponsor_cash'] = funds - paid
            pool['closing_reserve'] = 0
        else:
            cash = period_cash
            if period == horizon:
                pool['reserve_released'] = reserve
                cash += reserve
                reserve = 0
            for name, _ in tranches:
                item = debt[name]
                payment = min(item['unpaid'], cash + reserve)
                cash_part = min(cash, payment)
                reserve_part = payment - cash_part
                cash -= cash_part
                reserve -= reserve_part
                pool['reserve_used'] += reserve_part
                item['unpaid'] -= payment
                pay_log[name]['interest_paid'] = payment
            if period != horizon:
                top_up = min(cash, target_reserve - reserve)
                reserve += top_up
                cash -= top_up
                pool['reserve_replenished'] = top_up
            for name, _ in tranches:
                item = debt[name]
                payment = min(item['principal'], cash)
                cash -= payment
                item['principal'] -= payment
                pay_log[name]['principal_paid'] = payment
            pool['sponsor_cash'] = cash
            pool['closing_reserve'] = reserve

        pool['interest_paid'] = sum(entry['interest_paid'] for entry in pay_log.values())
        pool['principal_paid'] = sum(entry['principal_paid'] for entry in pay_log.values())
        pool['total_asset_principal_close'] = sum(
            row['closing_principal'] for row in period_assets)
        pool['total_pending_close'] = sum(row['pending_close'] for row in period_assets)

        for name, terms in tranches:
            item = debt[name]
            tranche_rows.append({
                'tranche': name, 'period': period,
                'opening_principal': opening[name][0],
                'opening_unpaid_interest': opening[name][1],
                'accrued_interest': accrued[name],
                'interest_paid': pay_log[name]['interest_paid'],
                'principal_paid': pay_log[name]['principal_paid'],
                'closing_principal': item['principal'],
                'closing_unpaid_interest': item['unpaid']})
        asset_rows.extend(period_assets)
        pool_rows.append(pool)

    _reconcile(spec, asset_rows, tranche_rows, pool_rows, state)
    return asset_rows, tranche_rows, pool_rows


def _empty_pool_row(period, reserve):
    return {'period': period, 'opening_reserve': 0, 'reserve_used': 0,
            'reserve_replenished': 0, 'reserve_released': 0, 'closing_reserve': 0,
            'contract_cash_in': 0, 'recovery_cash_in': 0, 'sale_proceeds': 0,
            'total_asset_principal_open': 0, 'total_pending_open': 0,
            'quote_offered': 0, 'quote_ineligible_reason': 'no_saleable_assets',
            'quote_price': 0, 'cleanup_exercised': 0, 'total_debt_at_quote': 0,
            'coverage_gap': 0, 'interest_paid': 0, 'principal_paid': 0,
            'sponsor_cash': 0, 'total_asset_principal_close': 0,
            'total_pending_close': 0}


def _empty_tranche_row(name, period):
    return {'tranche': name, 'period': period, 'opening_principal': 0,
            'opening_unpaid_interest': 0, 'accrued_interest': 0,
            'interest_paid': 0, 'principal_paid': 0, 'closing_principal': 0,
            'closing_unpaid_interest': 0}


def _reconcile(spec, asset_rows, tranche_rows, pool_rows, state):
    """All conservation identities the dataset promises to its reviewers."""
    by_loan = {}
    for row in asset_rows:
        by_loan.setdefault(row['loan_id'], []).append(row)
    contracts = {loan['loan_id']: loan for loan in spec['loans']}
    for loan_id, rows in by_loan.items():
        initial = contracts[loan_id]['principal']
        principal_out = sum(row['contract_principal'] + row['prepay_principal']
                            + row['defaulted_principal'] + row['sold_principal']
                            for row in rows)
        if principal_out != initial:
            raise ValueError('loan %s principal conservation failed' % loan_id)
        for row in rows:
            if row['defaulted_principal'] != row['asset_loss'] + row['recovery_recognized']:
                raise ValueError('loan %s period %d default split failed' % (loan_id, row['period']))
            rollforward = (row['pending_open'] + row['recovery_recognized']
                           - row['recovery_received'] - row['canceled_recovery'])
            if rollforward != row['pending_close']:
                raise ValueError('loan %s period %d recovery rollforward failed'
                                 % (loan_id, row['period']))
            if row['opening_principal'] - row['contract_principal'] \
                    - row['prepay_principal'] - row['defaulted_principal'] \
                    - row['sold_principal'] != row['closing_principal']:
                raise ValueError('loan %s period %d principal rollforward failed'
                                 % (loan_id, row['period']))
        recognized = sum(row['recovery_recognized'] for row in rows)
        settled = sum(row['recovery_received'] + row['canceled_recovery'] for row in rows)
        if recognized != settled:
            raise ValueError('loan %s recovery conservation failed' % loan_id)
    if any(st['balance'] or st['pending'] for st in state.values()):
        raise ValueError('asset balances remain after horizon simulation')

    by_tranche = {}
    for row in tranche_rows:
        by_tranche.setdefault(row['tranche'], []).append(row)
    terms = {'senior': spec['senior'], 'junior': spec['junior']}
    for name, rows in by_tranche.items():
        if rows[0]['opening_principal'] != terms[name]['principal']:
            raise ValueError('tranche %s opening principal mismatch' % name)
        for row in rows:
            if row['opening_principal'] - row['principal_paid'] != row['closing_principal']:
                raise ValueError('tranche %s period %d principal rollforward failed'
                                 % (name, row['period']))
            if row['opening_unpaid_interest'] + row['accrued_interest'] \
                    - row['interest_paid'] != row['closing_unpaid_interest']:
                raise ValueError('tranche %s period %d interest rollforward failed'
                                 % (name, row['period']))

    for row in pool_rows:
        sources = (row['opening_reserve'] + row['contract_cash_in']
                   + row['recovery_cash_in'] + row['sale_proceeds'])
        uses = (row['closing_reserve'] + row['interest_paid']
                + row['principal_paid'] + row['sponsor_cash'])
        if sources != uses:
            raise ValueError('pool period %d cash conservation failed' % row['period'])
        asset_contract = sum(r['contract_interest'] + r['contract_principal']
                             + r['prepay_principal'] for r in asset_rows
                             if r['period'] == row['period'])
        asset_recovery = sum(r['recovery_received'] for r in asset_rows
                             if r['period'] == row['period'])
        asset_sale = sum(r['sale_proceeds'] for r in asset_rows
                         if r['period'] == row['period'])
        if (asset_contract, asset_recovery, asset_sale) != (
                row['contract_cash_in'], row['recovery_cash_in'], row['sale_proceeds']):
            raise ValueError('pool period %d asset aggregation mismatch' % row['period'])
    exercised_periods = [row['period'] for row in pool_rows if row['cleanup_exercised']]
    if len(exercised_periods) > 1:
        raise ValueError('cleanup exercised more than once')
    if exercised_periods:
        cut = exercised_periods[0]
        later = [row for row in pool_rows if row['period'] > cut]
        if any(row['contract_cash_in'] or row['recovery_cash_in']
               or row['sale_proceeds'] or row['interest_paid']
               or row['principal_paid'] or row['cleanup_exercised']
               or row['quote_offered'] for row in later):
            raise ValueError('post-cleanup flows must be zero')
