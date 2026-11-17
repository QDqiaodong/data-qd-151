from fractions import Fraction

def half_up(value):
    return (2*value.numerator+value.denominator)//(2*value.denominator)

def schedule(principal,periods,numerator,denominator):
    regular=principal//periods;balance=principal;rows=[]
    for period in range(1,periods+1):
        start=balance;repaid=regular if period<periods else balance
        exact_interest=Fraction(start*numerator,denominator);interest=half_up(exact_interest);balance-=repaid
        rows.append({'period':period,'opening':start,'principal':repaid,'exact_interest':str(exact_interest),'interest':interest,'payment':repaid+interest,'closing':balance})
    total_interest=sum(r['interest'] for r in rows)
    return {'schedule':rows,'total_principal':sum(r['principal'] for r in rows),'total_interest':total_interest,'total_payment':principal+total_interest,'rounding':'nonnegative half-up to one minor unit, each period'}
