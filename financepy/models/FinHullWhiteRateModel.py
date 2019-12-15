# -*- coding: utf-8 -*-

import numpy as np
from numba import njit
from math import ceil, sqrt, exp, log
from ..finutils.FinMath import N
from ..finutils.FinError import FinError
from ..finutils.FinGlobalVariables import gDaysInYear
from ..market.curves.FinInterpolate import FinInterpMethods, uinterpolate

###############################################################################

@njit(fastmath=True, cache=True)
def buildTreeFast(a, sigma, treeTimes, numTimeSteps, discountFactors):

    treeMaturity = treeTimes[-1]
    dt = treeMaturity / (numTimeSteps+1)
    dR = sigma * sqrt(3.0 * dt)
    jmax = ceil(0.1835/(a * dt))
    jmin = - jmax
    N = jmax

    pu = np.zeros(shape=(2*jmax+1))
    pm = np.zeros(shape=(2*jmax+1))
    pd = np.zeros(shape=(2*jmax+1))

    # The short rate goes out one step extra to have the final short rate
    rt = np.zeros(shape=(numTimeSteps+2, 2*jmax+1))

    # probabilities start at time 0 and go out to one step before T
    # Branching is simple trinomial out to time step m=1 after which
    # the top node and bottom node connect internally to two lower nodes
    # and two upper nodes respectively. The probabilities only depend on j

    for j in range(-jmax, jmax+1):
        ajdt = a*j*dt
        jN = j + N
        if j == jmax:
            pu[jN] = 7.0/6.0 + 0.50*(ajdt*ajdt - 3.0*ajdt)
            pm[jN] = -1.0/3.0 - ajdt*ajdt + 2.0*ajdt
            pd[jN] = 1.0/6.0 + 0.50*(ajdt*ajdt - ajdt)
        elif j == -jmax:
            pu[jN] = 1.0/6.0 + 0.50*(ajdt*ajdt + ajdt)
            pm[jN] = -1.0/3.0 - ajdt*ajdt - 2.0*ajdt
            pd[jN] = 7.0/6.0 + 0.50*(ajdt*ajdt + 3.0*ajdt)
        else:
            pu[jN] = 1.0/6.0 + 0.50*(ajdt*ajdt - ajdt)
            pm[jN] = 2.0/3.0 - ajdt*ajdt
            pd[jN] = 1.0/6.0 + 0.50*(ajdt*ajdt + ajdt)

    # Arrow-Debreu array
    Q = np.zeros(shape=(numTimeSteps+2, 2*N+1))

    # This is the drift adjustment to ensure no arbitrage at each time
    alpha = np.zeros(numTimeSteps+1)

    # Time zero is trivial for the Arrow-Debreu price
    Q[0, N] = 1.0

    # Big loop over time steps
    for m in range(0, numTimeSteps + 1):

        nm = min(m, jmax)
        sumQZ = 0.0
        for j in range(-nm, nm+1):
            rdt = j*dR*dt
            sumQZ += Q[m, j+N] * exp(-rdt)
        alpha[m] = log(sumQZ/discountFactors[m+1]) / dt

        for j in range(-nm, nm+1):
            jN = j + N
            rt[m, jN] = alpha[m] + j*dR

        # Loop over all nodes at time m to calculate next values of Q
        for j in range(-nm, nm+1):
            jN = j + N
            rdt = rt[m, jN] * dt
            z = exp(-rdt)

            if j == jmax:
                Q[m+1, jN] += Q[m, jN] * pu[jN] * z
                Q[m+1, jN-1] += Q[m, jN] * pm[jN] * z
                Q[m+1, jN-2] += Q[m, jN] * pd[jN] * z
            elif j == jmin:
                Q[m+1, jN] += Q[m, jN] * pd[jN] * z
                Q[m+1, jN+1] += Q[m, jN] * pm[jN] * z
                Q[m+1, jN+2] += Q[m, jN] * pu[jN] * z
            else:
                Q[m+1, jN+1] += Q[m, jN] * pu[jN] * z
                Q[m+1, jN] += Q[m, jN] * pm[jN] * z
                Q[m+1, jN-1] += Q[m, jN] * pd[jN] * z

    return (Q, pu, pm, pd, rt, dt)

##########################################################################


class FinHullWhiteRateModel():

    def __init__(self, a, sigma):
        ''' Constructs the Hull-White rate model. The speed of mean reversion
        a and volatility are passed in. The short rate process is given by
        dr = (theta(t) - ar) * dt  + sigma * dW '''

        if sigma < 0.0:
            raise FinError("Negative volatility not allowed.")

        if a < 0.0:
            raise FinError("Mean reversion speed parameter should be >= 0.")

        self._a = a
        self._sigma = sigma

        self._Q = None
        self._r = None
        self._treeTimes = None
        self._pu = None
        self._pm = None
        self._pd = None
        self._discountCurve = None

###############################################################################

    def P(self, t, T, Rt, delta, pt, ptd, pT):
        ''' Forward discount factor as seen at some time t which may be in the
        future for payment at time T where Rt is the delta-period short rate
        seen at time t and pt is the discount factor to time t, ptd is the one
        period discount factor to time t+dt and pT is the discount factor from
        now until the payment of the $1 of the discount factor. '''

        sigma = self._sigma
        a = self._a

        BtT = (1.0 - exp(-self._a*(T-t)))/self._a
        BtDelta = (1.0 - exp(-self._a * delta))/self._a

        term1 = log(pT/pt) - (BtT/BtDelta) * log(ptd/pt)
        term2 = (sigma**2)*(1.0-exp(-2.0*a*t)) * BtT * (BtT - BtDelta)/(4.0*a)

        logAhat = term1 - term2
        BhattT = (BtT/BtDelta) * delta
        p = exp(logAhat - BhattT * Rt)
        return p

###############################################################################

    def europeanBondOption(self, settlementDate, expiryDate,
                           strikePrice, face, bond):
        ''' Price an option on a coupon-paying bond using tree to generate
        short rates at the expiry date and then to analytical solution of
        zero coupon bond in HW model to calculate the corresponding bond price.
        User provides bond object and option details. '''

        texp = (expiryDate - settlementDate) / gDaysInYear
        tmat = (bond._maturityDate - settlementDate) / gDaysInYear

        if texp > tmat:
            raise FinError("Option expiry after bond matures.")

        if texp < 0.0:
            raise FinError("Option expiry time negative.")

        if self._treeTimes is None:
            raise FinError("Tree has not been constructed.")

        if self._treeTimes[-1] < texp:
            raise FinError("Tree expiry must be >= option expiry date.")

        bond.calculateFlowDates(expiryDate)
        flowTimes = []
        for flowDate in bond._flowDates[1:]:
            t = (flowDate - settlementDate) / gDaysInYear
            flowTimes.append(t)

        dt = self._dt
        tdelta = texp + dt
        ptexp = self._discountCurve.df(texp)
        ptdelta = self._discountCurve.df(tdelta)

        numTimeSteps, numNodes = self._Q.shape
        expiryStep = int(texp/dt+0.50)

        callPrice = 0.0
        putPrice = 0.0

        for k in range(0, numNodes):
            q = self._Q[expiryStep, k]
            rt = self._rt[expiryStep, k]

            pv = 0.0
            for tflow in flowTimes:
                ptflow = self._discountCurve.df(tflow)
                zcb = self.P(texp, tflow, rt, dt, ptexp, ptdelta, ptflow)
                pv += bond._coupon / bond._frequency * zcb
            pv += zcb

            putPayoff = max(strikePrice - pv * face, 0.0)
            callPayoff = max(pv * face - strikePrice, 0.0)
            putPrice += q * putPayoff
            callPrice += q * callPayoff

        return (callPrice, putPrice)

###############################################################################

    def europeanOptionOnZeroCouponBond_Anal(self, settlementDate,
                                       expiryDate, maturityDate,
                                       strikePrice, face, discountCurve):
        ''' Price an option on a zero coupon bond using analytical solution of
        Hull-White model. User provides bond face and option strike and expiry
        date and maturity date. '''

        texp = (expiryDate - settlementDate) / gDaysInYear
        tmat = (maturityDate - settlementDate) / gDaysInYear

        if texp > tmat:
            raise FinError("Option expiry after bond matures.")

        if texp < 0.0:
            raise FinError("Option expiry time negative.")

        ptexp = discountCurve.df(texp)
        ptmat = discountCurve.df(tmat)

        sigma = self._sigma
        a = self._a

        sigmap = (sigma/a) * (1.0 - exp(-a*(tmat-texp)))
        sigmap *= sqrt((1.0-exp(-2.0*a*texp))/2.0/a)
        h = log((face * ptmat)/(strikePrice * ptexp)) / sigmap + sigmap/2.0

        callPrice = face * ptmat * N(h) - strikePrice * ptexp * N(h-sigmap)
        putPrice = strikePrice * ptexp * N(-h+sigmap) - face * ptmat * N(-h)

        return callPrice, putPrice

###############################################################################

    def europeanOptionOnZeroCouponBond_Tree(self, settlementDate,
                                    expiryDate, maturityDate,
                                    strikePrice, face):

        ''' Price an option on a zero coupon bond using a HW trinomial
        tree. The discount curve was already supplied to the tree build. '''

        texp = (expiryDate - settlementDate) / gDaysInYear
        tmat = (maturityDate - settlementDate) / gDaysInYear

        if texp > tmat:
            raise FinError("Option expiry after bond matures.")

        if texp < 0.0:
            raise FinError("Option expiry time negative.")

        if self._treeTimes is None:
            raise FinError("Tree has not been constructed.")

        if self._treeTimes[-1] < texp:
            raise FinError("Tree expiry must be >= option expiry date.")

        dt = self._dt
        tdelta = texp + dt
        ptexp = self._discountCurve.df(texp)
        ptdelta = self._discountCurve.df(tdelta)
        ptmat = self._discountCurve.df(tmat)

        numTimeSteps, numNodes = self._Q.shape
        expiryStep = int(texp/dt+0.50)

        callPrice = 0.0
        putPrice = 0.0

        for k in range(0, numNodes):
            q = self._Q[expiryStep, k]
            rt = self._rt[expiryStep, k]
            zcb = self.P(texp, tmat, rt, dt, ptexp, ptdelta, ptmat)
            putPayoff = max(strikePrice - zcb * face, 0.0)
            callPayoff = max(zcb * face - strikePrice, 0.0)
            putPrice += q * putPayoff
            callPrice += q * callPayoff

        return (callPrice, putPrice)

###############################################################################

    def bondOption(self, settlementDate, expiryDate, strikePrice,
                   face, bond, americanExercise):
        ''' Value an option on a bond with coupons that can have European or
        American exercise. Some minor issues to do with handling coupons on
        the option expiry date need to be solved. Also this function should be
        moved out of the class so it can be sped up using NUMBA. '''

        interp = FinInterpMethods.FLAT_FORWARDS.value

        texp = (expiryDate - settlementDate) / gDaysInYear
        tmat = (bond._maturityDate - settlementDate) / gDaysInYear

        if texp > tmat:
            raise FinError("Option expiry after bond matures.")

        if texp < 0.0:
            raise FinError("Option expiry time negative.")

        #######################################################################

        dfTimes = self._discountCurve._times
        dfValues = self._discountCurve._values

        #######################################################################

        numTimeSteps, numNodes = self._Q.shape
        dt = self._dt
        jmax = ceil(0.1835/(self._a * dt))
        N = jmax
        expiryStep = int(texp/dt + 0.50)
        maturityStep = int(tmat/dt + 0.50)

        #######################################################################

        bond.calculateFlowDates(settlementDate)
        couponTimes = [0.0]
        couponFlows = [0.0]
        cpn = bond._coupon/bond._frequency
        for flowDate in bond._flowDates[1:]:
            flowTime = (flowDate - settlementDate) / gDaysInYear
            couponTimes.append(flowTime)
            couponFlows.append(cpn)
        numCoupons = len(couponTimes)
        couponTimes = np.array(couponTimes)
        couponFlows = np.array(couponFlows)

        if np.any(couponTimes < 0.0):
            raise FinError("No coupon times can be before the value date.")

        if np.any(couponTimes > tmat):
            raise FinError("No coupon times can be after the maturity date.")

        treeFlows = np.zeros(numTimeSteps)

        for i in range(0, numCoupons):
            flowTime = couponTimes[i]
            if flowTime <= texp:
                n = int(round(flowTime/dt, 0))
                treeTime = self._treeTimes[n]
                df_flow = uinterpolate(flowTime, dfTimes, dfValues, interp)
                df_tree = uinterpolate(treeTime, dfTimes, dfValues, interp)
                treeFlows[n] += couponFlows[i] * 1.0 * df_flow / df_tree

        accrued = np.zeros(numTimeSteps)
        for m in range(0, expiryStep+1):
            treeTime = self._treeTimes[m]

            for nextCpn in range(1, numCoupons):
                prevTime = couponTimes[nextCpn-1]
                nextTime = couponTimes[nextCpn]
                if treeTime > prevTime and treeTime < nextTime:
                    accdPeriod = treeTime - prevTime
                    period = (nextTime - prevTime)
                    accd = accdPeriod * cpn * face / period
                    accrued[m] = accd
                    break

        #######################################################################

        optionValues = np.zeros(shape=(numTimeSteps, numNodes))
        bondValues = np.zeros(shape=(numTimeSteps, numNodes))

        ptexp = uinterpolate(texp, dfTimes, dfValues, interp)
        ptdelta = uinterpolate(texp+dt, dfTimes, dfValues, interp)

        flow = treeFlows[expiryStep] * face
        nm = min(expiryStep, jmax)
        for k in range(-nm, nm+1):
            kN = k + N
            rt = self._rt[expiryStep, kN]
            bondPrice = 0.0
            for i in range(0, numCoupons):
                tflow = couponTimes[i]
                if tflow > self._treeTimes[expiryStep]: # must be >
                    ptflow = uinterpolate(tflow, dfTimes, dfValues, interp)
                    zcb = self.P(texp, tflow, rt, dt, ptexp, ptdelta, ptflow)
                    bondPrice += cpn * face * zcb

            bondPrice += face * zcb
            bondValues[expiryStep, kN] = bondPrice + flow

  #      print(">>> bondValue", bondValues[expiryStep,N], "accrued", accrued[expiryStep],"flow", flow)

        # Now consider exercise of the option on the expiry date
        # Start with the value of the bond at maturity and overwrite values
        nm = min(expiryStep, jmax)
        for k in range(-nm, nm+1):
            kN = k + N
            cleanPrice = bondValues[expiryStep, kN] - accrued[expiryStep]
            optionValues[expiryStep, kN] = max(cleanPrice - strikePrice, 0.0)

        # Now step back to today considering early exercise
        for m in range(expiryStep-1, -1, -1):
            nm = min(m, jmax)
            flow = treeFlows[m] * face

            for k in range(-nm, nm+1):
                kN = k + N
                rt = self._rt[m, kN]
                df = exp(-rt*dt)
                pu = self._pu[kN]
                pm = self._pm[kN]
                pd = self._pd[kN]

                if k == jmax:
                    vu = bondValues[m+1, kN]
                    vm = bondValues[m+1, kN-1]
                    vd = bondValues[m+1, kN-2]
                    v = (pu*vu + pm*vm + pd*vd) * df
                    bondValues[m, kN] = v
                elif k == jmax:
                    vu = bondValues[m+1, kN+2]
                    vm = bondValues[m+1, kN+1]
                    vd = bondValues[m+1, kN]
                    v = (pu*vu + pm*vm + pd*vd) * df
                    bondValues[m, kN] = v
                else:
                    vu = bondValues[m+1, kN+1]
                    vm = bondValues[m+1, kN]
                    vd = bondValues[m+1, kN-1]
                    v = (pu*vu + pm*vm + pd*vd) * df
                    bondValues[m, kN] = v

                bondValues[m, kN] += flow

                if k == jmax:
                    vu = optionValues[m+1, kN]
                    vm = optionValues[m+1, kN-1]
                    vd = optionValues[m+1, kN-2]
                    v = (pu*vu + pm*vm + pd*vd) * df
                    optionValues[m, kN] = v
                elif k == jmax:
                    vu = optionValues[m+1, kN+2]
                    vm = optionValues[m+1, kN+1]
                    vd = optionValues[m+1, kN]
                    v = (pu*vu + pm*vm + pd*vd) * df
                    optionValues[m, kN] = v
                else:
                    vu = optionValues[m+1, kN+1]
                    vm = optionValues[m+1, kN]
                    vd = optionValues[m+1, kN-1]
                    v = (pu*vu + pm*vm + pd*vd) * df
                    optionValues[m, kN] = v

                if americanExercise is True:
                    cleanPrice = bondValues[m, kN] - accrued[m]
                    exercise = max(cleanPrice - strikePrice,0)
                    hold = optionValues[m, kN]
                    optionValues[m, kN] = max(exercise, hold)

        self._bondValues = bondValues
        self._optionValues = optionValues
        return optionValues[0, N], bondValues[0,N]

###############################################################################

    def df_Tree(self, tmat):
        ''' Discount factor as seen from now to time tmat as long as the time
        is on the tree grid. '''

        if tmat == 0.0:
            return 1.0

        numTimeSteps, numNodes = self._Q.shape
        fn1 = tmat/self._dt
        fn2 = float(int(tmat/self._dt))
        if abs(fn1 - fn2) > 1e-6:
            raise FinError("Time not on tree time grid")

        timeStep = int(tmat / self._dt) + 1

        p = 0.0
        for i in range(0, numNodes):
            ad = self._Q[timeStep, i]
            p += ad
        zeroRate = -log(p)/tmat
        return p, zeroRate

###############################################################################

    def buildTree(self, startDate, endDate, numTimeSteps, discountCurve):

        maturity = (endDate - startDate) / gDaysInYear
        treeMaturity = maturity * (numTimeSteps+1)/numTimeSteps
        treeTimes = np.linspace(0.0, treeMaturity, numTimeSteps + 2)

        discountFactors = np.zeros(shape=(numTimeSteps+2))
        discountFactors[0] = 1.0

        for i in range(1, numTimeSteps+2):
            t = treeTimes[i]
            discountFactors[i] = discountCurve.df(t)

        self._Q, self._pu, self._pm, self._pd, self._rt, self._dt \
            = buildTreeFast(self._a, self._sigma,
                           treeTimes, numTimeSteps, discountFactors)

        self._treeTimes = treeTimes
        self._discountCurve = discountCurve

###############################################################################
