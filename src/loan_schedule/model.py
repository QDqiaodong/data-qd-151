from .common import require,number
from .calculation import schedule

def analyze(data):
    require(data,'principal periods rate_numerator rate_denominator')
    principal=number(data['principal'],1,10**12);periods=number(data['periods'],1,10000)
    numerator=number(data['rate_numerator'],0,10**6);denominator=number(data['rate_denominator'],1,10**9)
    if periods>principal:raise ValueError('periods exceed minor-unit principal')
    return schedule(principal,periods,numerator,denominator)
