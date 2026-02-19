from dotenv import load_dotenv
load_dotenv()
from flask import Flask, render_template, jsonify
from flask_cors import CORS
import finnhub
import os
import time
import json
import datetime
import traceback
import yfinance as yf

app = Flask(__name__)
CORS(app)

# ─── Finnhub client ──────────────────────────────────────────────────────────
def get_client():
    api_key = os.environ.get('FINNHUB_API_KEY', '')
    if not api_key:
        raise RuntimeError('FINNHUB_API_KEY environment variable is not set.')
    return finnhub.Client(api_key=api_key)

# ─── Simple TTL cache (5 minutes) ────────────────────────────────────────────
_cache = {}
CACHE_TTL = 300

# ─── Helpers ─────────────────────────────────────────────────────────────────

def safe(d, *keys, default=None):
    """Safely traverse nested dict/list. safe(d, 'a', 'b', 0) = d['a']['b'][0]"""
    try:
        v = d
        for k in keys:
            v = v[k]
        return v if v is not None else default
    except Exception:
        return default


def to_millions(value, default=None):
    try:
        if value is None:
            return default
        return round(float(value) / 1_000_000, 2)
    except Exception:
        return default


def to_pct(value, default=None):
    """Convert a ratio (0-1) to a percentage. If value already looks like a pct (>1), just round it."""
    try:
        if value is None:
            return default
        f = float(value)
        # Finnhub margins come as ratios (0.43), PE / EPS come as absolute numbers
        return round(f * 100, 2)
    except Exception:
        return default


def latest_series(series_dict, key):
    """Return the most recent value from a Finnhub annual series list."""
    try:
        items = series_dict.get(key, [])
        if items:
            # list is newest-first
            return items[0].get('v')
    except Exception:
        pass
    return None


def unix_days_ago(days):
    return int((datetime.datetime.utcnow() - datetime.timedelta(days=days)).timestamp())


def unix_now():
    return int(datetime.datetime.utcnow().timestamp())


# ─── Data fetchers ────────────────────────────────────────────────────────────

def fetch_quote_and_price(client, symbol):
    """Current price, previous close, open, daily range from /quote."""
    errors = []
    result = {
        'current': None,
        'previous_close': None,
        'open': None,
        'daily_high': None,
        'daily_low': None,
        'daily_volume_usd_millions': None,
        'fifty_two_week_high': None,
        'fifty_two_week_low': None,
    }
    try:
        q = client.quote(symbol)
        result['current']        = safe(q, 'c')
        result['previous_close'] = safe(q, 'pc')
        result['open']           = safe(q, 'o')
        result['daily_high']     = safe(q, 'h')
        result['daily_low']      = safe(q, 'l')
    except Exception as e:
        errors.append(f'Quote: {e}')

    # NOTE: stock_candles requires a Premium plan on Finnhub.
    # We compute approximate daily USD volume from the 10-day avg volume
    # in company_basic_financials (metric['10DayAverageTradingVolume']) later
    # in build_payload once we have that data, so we leave the field as None here.

    return result, errors


def fetch_basic_financials(client, symbol):
    """
    52-week high/low, market cap, margins, PE, EPS, PS, EV/EBITDA etc.
    from /stock/metric (company_basic_financials).
    """
    errors = []
    metric = {}
    series_annual = {}
    series_quarterly = {}
    try:
        bf = client.company_basic_financials(symbol, 'all')
        metric           = bf.get('metric', {}) or {}
        series_annual    = (bf.get('series', {}) or {}).get('annual', {}) or {}
        series_quarterly = (bf.get('series', {}) or {}).get('quarterly', {}) or {}
    except Exception as e:
        errors.append(f'Basic financials: {e}')

    # 52-week range from metric snapshot
    wk52_high = metric.get('52WeekHigh')
    wk52_low  = metric.get('52WeekLow')

    # Market cap is in USD millions in Finnhub
    market_cap_millions = metric.get('marketCapitalization')

    # Trailing PE and EPS from annual series (newest-first list)
    trailing_pe  = latest_series(series_annual, 'pe')
    trailing_eps = latest_series(series_annual, 'eps')

    # PS ratio, EV/EBITDA — Finnhub doesn't directly expose EV/EBITDA in free basic financials.
    # 'ps' is price-to-sales, 'pb' is price-to-book, 'pfcf' is price-to-FCF.
    ps_ratio  = latest_series(series_annual, 'ps')
    pb_ratio  = latest_series(series_annual, 'pb')
    pfcf      = latest_series(series_annual, 'pfcf')
    ev        = latest_series(series_annual, 'ev')        # enterprise value in millions
    ev_ebitda = latest_series(series_annual, 'evEbitda')  # correct Finnhub field name
    ev_rev    = latest_series(series_annual, 'evRevenue') # correct Finnhub field name

    # Margins (ratio form — multiply by 100 for %)
    gross_margin_pct     = to_pct(latest_series(series_annual, 'grossMargin'))
    operating_margin_pct = to_pct(latest_series(series_annual, 'operatingMargin'))
    net_margin_pct       = to_pct(latest_series(series_annual, 'netMargin'))
    fcf_margin_pct       = to_pct(latest_series(series_annual, 'fcfMargin'))

    # Revenue per share — use to estimate total revenue if shares outstanding available
    sales_per_share = latest_series(series_annual, 'salesPerShare')
    ebit_per_share  = latest_series(series_annual, 'ebitPerShare')

    # Book value per share
    book_value = latest_series(series_annual, 'bookValue')

    # Beta from metric snapshot
    beta = metric.get('beta')

    # 10-day avg volume (in millions of shares)
    avg_vol_10d = metric.get('10DayAverageTradingVolume')
    avg_vol_3m  = metric.get('3MonthAverageTradingVolume')

    return {
        'wk52_high':            wk52_high,
        'wk52_low':             wk52_low,
        'market_cap_millions':  market_cap_millions,
        'trailing_pe':          trailing_pe,
        'trailing_eps':         trailing_eps,
        'ps_ratio':             ps_ratio,
        'pb_ratio':             pb_ratio,
        'pfcf':                 pfcf,
        'ev_millions':          ev,
        'ev_ebitda':            ev_ebitda,
        'ev_revenue':           ev_rev,
        'gross_margin_pct':     gross_margin_pct,
        'operating_margin_pct': operating_margin_pct,
        'net_margin_pct':       net_margin_pct,
        'fcf_margin_pct':       fcf_margin_pct,
        'sales_per_share':      sales_per_share,
        'ebit_per_share':       ebit_per_share,
        'book_value_per_share': book_value,
        'beta':                 beta,
        'avg_vol_10d_M_shares': avg_vol_10d,
        'avg_vol_3m_M_shares':  avg_vol_3m,
        'series_annual':        series_annual,
        'series_quarterly':     series_quarterly,
        'metric':               metric,
    }, errors


def fetch_reported_financials(client, symbol):
    """
    Pull the most recent annual 10-K from /financials/reported.

    IMPORTANT: Finnhub returns ic/cf/bs as a LIST of objects:
        [{"concept": "us-gaap_GrossProfit", "label": "...", "unit": "usd", "value": 123}, ...]
    We build a lookup dict keyed by the bare concept name (strip 'us-gaap_' prefix)
    and also by the full prefixed name, then match against known aliases.
    """
    errors = []
    result = {
        'revenue_millions':              None,
        'gross_profit_millions':         None,
        'operating_income_millions':     None,
        'net_income_millions':           None,
        'operating_cash_flow_millions':  None,
        'capex_millions':                None,
        'fcf_millions':                  None,
        'net_cash_millions':             None,
        'period':                        None,
        'fiscal_year':                   None,
    }

    # Aliases tried in priority order — bare names (prefix stripped)
    REVENUE_KEYS = [
        'RevenueFromContractWithCustomerExcludingAssessedTax',
        'RevenueFromContractWithCustomerIncludingAssessedTax',
        'Revenues', 'SalesRevenueNet', 'SalesRevenueGoodsNet',
        'TotalRevenues', 'NetRevenues', 'RevenueNet',
    ]
    GROSS_PROFIT_KEYS = ['GrossProfit']
    OP_INCOME_KEYS = [
        'OperatingIncomeLoss',
        'IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest',
        'IncomeLossFromContinuingOperationsBeforeIncomeTaxes',
    ]
    NET_INCOME_KEYS = ['NetIncomeLoss', 'NetIncome', 'ProfitLoss']
    OCF_KEYS = [
        'NetCashProvidedByUsedInOperatingActivities',
        'NetCashProvidedByUsedInOperatingActivitiesContinuingOperations',
    ]
    CAPEX_KEYS = [
        'PaymentsToAcquirePropertyPlantAndEquipment',
        'CapitalExpendituresIncurringObligation',
        'PurchaseOfPropertyPlantAndEquipment',
    ]
    CASH_KEYS = [
        'CashAndCashEquivalentsAtCarryingValue',
        'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents',
        'CashCashEquivalentsAndShortTermInvestments',
        'Cash',
    ]
    TOTAL_DEBT_KEYS = [
        # Try summing current + non-current debt; fall back to individual lines
        'LongTermDebtNoncurrent', 'LongTermDebt',
        'LongTermDebtAndCapitalLeaseObligation',
        'LongTermDebtCurrent',
    ]
    CURRENT_DEBT_KEYS = ['LongTermDebtCurrent', 'CommercialPaper', 'DebtCurrent']

    def section_to_dict(section):
        """Convert list of concept objects to a dict keyed by bare concept name."""
        if isinstance(section, dict):
            return section  # already a dict (future-proof)
        lookup = {}
        for item in (section or []):
            concept = item.get('concept', '')
            value   = item.get('value')
            if value is None:
                continue
            # Store under both full name and bare name (strip namespace prefix)
            lookup[concept] = float(value)
            bare = concept.split('_', 1)[-1] if '_' in concept else concept
            lookup[bare] = float(value)
        return lookup

    def first_match(lookup, keys):
        for k in keys:
            if k in lookup:
                return lookup[k]
        return None

    try:
        resp = client.financials_reported(symbol=symbol, freq='annual')
        data = resp.get('data', []) or []
        if not data:
            errors.append('financials_reported: no annual data returned')
            return result, errors

        filing = data[0]
        result['period']      = str(filing.get('endDate', ''))[:10]
        result['fiscal_year'] = filing.get('year')

        report = filing.get('report', {}) or {}
        ic = section_to_dict(report.get('ic', []))
        cf = section_to_dict(report.get('cf', []))
        bs = section_to_dict(report.get('bs', []))

        revenue    = first_match(ic, REVENUE_KEYS)
        gross_p    = first_match(ic, GROSS_PROFIT_KEYS)
        op_income  = first_match(ic, OP_INCOME_KEYS)
        net_income = first_match(ic, NET_INCOME_KEYS) or first_match(cf, NET_INCOME_KEYS)
        ocf        = first_match(cf, OCF_KEYS)
        capex_raw  = first_match(cf, CAPEX_KEYS)

        # Net cash: cash - (long-term debt noncurrent + long-term debt current)
        cash      = first_match(bs, CASH_KEYS)
        # Also include marketable securities as quasi-cash if available
        mkt_sec   = bs.get('MarketableSecuritiesCurrent', 0) or 0
        lt_debt   = first_match(bs, TOTAL_DEBT_KEYS) or 0
        cur_debt  = first_match(bs, CURRENT_DEBT_KEYS) or 0
        # Avoid double-counting if LongTermDebtCurrent already in TOTAL_DEBT_KEYS match
        total_debt = (bs.get('LongTermDebtNoncurrent') or bs.get('LongTermDebt') or 0) + \
                     (bs.get('LongTermDebtCurrent') or 0)
        net_cash   = (cash + mkt_sec - total_debt) if cash is not None else None

        capex_abs = abs(capex_raw) if capex_raw is not None else None
        fcf = (ocf - capex_abs) if ocf is not None and capex_abs is not None else None

        result.update({
            'revenue_millions':             to_millions(revenue),
            'gross_profit_millions':        to_millions(gross_p),
            'operating_income_millions':    to_millions(op_income),
            'net_income_millions':          to_millions(net_income),
            'operating_cash_flow_millions': to_millions(ocf),
            'capex_millions':               to_millions(capex_abs),
            'fcf_millions':                 to_millions(fcf),
            'net_cash_millions':            to_millions(net_cash),
        })
    except Exception as e:
        errors.append(f'financials_reported: {e}')
        print(f'[DEBUG] financials_reported exception: {traceback.format_exc()}')

    return result, errors


def fetch_estimates(client, symbol):
    """
    Forward EPS and revenue consensus for next 2 fiscal years.
    These endpoints are PREMIUM on Finnhub — we attempt them and degrade
    gracefully to N/A if access is denied or data is missing.
    """
    errors = []
    result = {
        'fy1': {
            'year': None, 'period': None,
            'eps_avg': None, 'eps_high': None, 'eps_low': None, 'eps_analysts': None,
            'revenue_avg_millions': None, 'revenue_high_millions': None,
            'revenue_low_millions': None, 'revenue_analysts': None,
        },
        'fy2': {
            'year': None, 'period': None,
            'eps_avg': None, 'eps_high': None, 'eps_low': None, 'eps_analysts': None,
            'revenue_avg_millions': None, 'revenue_high_millions': None,
            'revenue_low_millions': None, 'revenue_analysts': None,
        },
        'premium_required': False,
    }

    now_year = datetime.datetime.utcnow().year

    # EPS estimates
    try:
        eps_resp = client.company_eps_estimates(symbol, freq='annual')
        eps_data = eps_resp.get('data', []) or []
        # Filter to future years only, sorted ascending
        future_eps = sorted(
            [e for e in eps_data if e.get('year', 0) >= now_year],
            key=lambda x: x.get('year', 0)
        )
        for i, key in enumerate(['fy1', 'fy2']):
            if i < len(future_eps):
                e = future_eps[i]
                result[key]['year']          = e.get('year')
                result[key]['period']        = str(e.get('period', ''))[:10]
                result[key]['eps_avg']       = e.get('epsAvg')
                result[key]['eps_high']      = e.get('epsHigh')
                result[key]['eps_low']       = e.get('epsLow')
                result[key]['eps_analysts']  = e.get('numberAnalysts')
    except Exception as e:
        msg = str(e)
        if '403' in msg or 'Premium' in msg or 'premium' in msg or 'access' in msg.lower():
            result['premium_required'] = True
        else:
            errors.append(f'EPS estimates: {e}')

    # Revenue estimates
    try:
        rev_resp = client.company_revenue_estimates(symbol, freq='annual')
        rev_data = rev_resp.get('data', []) or []
        future_rev = sorted(
            [r for r in rev_data if r.get('year', 0) >= now_year],
            key=lambda x: x.get('year', 0)
        )
        for i, key in enumerate(['fy1', 'fy2']):
            if i < len(future_rev):
                r = future_rev[i]
                if result[key]['year'] is None:
                    result[key]['year']   = r.get('year')
                    result[key]['period'] = str(r.get('period', ''))[:10]
                result[key]['revenue_avg_millions']  = to_millions(r.get('revenueAvg'))
                result[key]['revenue_high_millions'] = to_millions(r.get('revenueHigh'))
                result[key]['revenue_low_millions']  = to_millions(r.get('revenueLow'))
                result[key]['revenue_analysts']      = r.get('numberAnalysts')
    except Exception as e:
        msg = str(e)
        if '403' in msg or 'Premium' in msg or 'premium' in msg or 'access' in msg.lower():
            result['premium_required'] = True
        else:
            errors.append(f'Revenue estimates: {e}')

    return result, errors


def fetch_ownership(client, symbol):
    """
    Top 10 institutional holders via /stock/ownership.
    PREMIUM endpoint — degrades gracefully if denied.
    """
    errors = []
    holders = []
    premium_required = False
    try:
        resp = client.ownership(symbol, limit=10)
        raw = resp.get('ownership', []) or []
        for h in raw[:10]:
            holders.append({
                'name':          h.get('name'),
                'shares':        h.get('share'),
                'change':        h.get('change'),
                'pct_held':      round(h.get('holdingPercent', 0) * 100, 4) if h.get('holdingPercent') else None,
                'value_millions': None,   # not returned by this endpoint
                'date_reported': str(h.get('filingDate', ''))[:10],
            })
    except Exception as e:
        msg = str(e)
        if '403' in msg or 'Premium' in msg or 'premium' in msg or 'access' in msg.lower():
            premium_required = True
        else:
            errors.append(f'Ownership: {e}')
    return holders, premium_required, errors


def fetch_insider_transactions(client, symbol):
    """
    Recent insider transactions — open-market buys/sells.
    Free tier: last 12 months.
    """
    errors = []
    transactions = []
    try:
        to_date   = datetime.date.today().isoformat()
        from_date = (datetime.date.today() - datetime.timedelta(days=365)).isoformat()
        resp = client.stock_insider_transactions(symbol, from_date, to_date)
        raw  = resp.get('data', []) or []

        # Sort by transaction date descending, take top 15
        raw_sorted = sorted(raw, key=lambda x: x.get('transactionDate', ''), reverse=True)
        for t in raw_sorted[:15]:
            code  = t.get('transactionCode', '')
            # Classify transaction code
            if code in ('P',):
                tx_type = 'Purchase'
            elif code in ('S',):
                tx_type = 'Sale'
            elif code in ('M', 'X'):
                tx_type = 'Option Exercise'
            elif code in ('A',):
                tx_type = 'Award/Grant'
            elif code in ('G',):
                tx_type = 'Gift'
            elif code in ('D',):
                tx_type = 'Disposition'
            elif code in ('F',):
                tx_type = 'Tax Withholding'
            else:
                tx_type = code or 'Unknown'

            price  = t.get('transactionPrice')
            change = t.get('change', 0) or 0
            value  = (abs(change) * price) if price else None

            transactions.append({
                'name':             t.get('name'),
                'title':            t.get('position', ''),
                'transaction':      tx_type,
                'transaction_code': code,
                'shares':           abs(change) if change else None,
                'price':            price,
                'value_millions':   to_millions(value),
                'date':             t.get('transactionDate', ''),
                'filing_date':      t.get('filingDate', ''),
            })
    except Exception as e:
        errors.append(f'Insider transactions: {e}')
    return transactions, errors


def fetch_profile(client, symbol):
    """Company name, exchange, sector, currency, shares outstanding."""
    errors = []
    result = {
        'name':               symbol,
        'exchange':           None,
        'sector':             None,
        'industry':           None,
        'currency':           'USD',
        'shares_outstanding': None,
        'logo':               None,
        'weburl':             None,
        'country':            None,
    }
    try:
        p = client.company_profile2(symbol=symbol)
        result.update({
            'name':               p.get('name', symbol),
            'exchange':           p.get('exchange'),
            'sector':             None,       # Finnhub profile2 gives finnhubIndustry, not a clean sector
            'industry':           p.get('finnhubIndustry'),
            'currency':           p.get('currency', 'USD'),
            'shares_outstanding': p.get('shareOutstanding'),  # in millions
            'logo':               p.get('logo'),
            'weburl':             p.get('weburl'),
            'country':            p.get('country'),
            'market_cap_millions': p.get('marketCapitalization'),
        })
    except Exception as e:
        errors.append(f'Company profile: {e}')
    return result, errors


# ─── yfinance secondary data source ──────────────────────────────────────────
# Used ONLY for: (1) forward estimates  (2) top institutional shareholders
# (3) short interest.  Finnhub remains the primary source for everything else.

def _yf_safe(df, period, col):
    """Safely extract a numeric value from a yfinance DataFrame.

    yfinance DataFrames use *periods* as the INDEX (e.g. '0y', '+1y')
    and *metric names* as COLUMNS (e.g. 'avg', 'high', 'numberOfAnalysts').
    """
    try:
        val = df.loc[period, col]
        if val is not None and str(val) not in ('nan', 'NaN', ''):
            return float(val)
    except Exception:
        pass
    return None


def _parse_fy_year(period_label):
    """Convert a yfinance period label to a calendar year.

    Labels look like '0y' (current FY), '+1y' (next FY), '0q', '+1q', etc.
    """
    try:
        s = str(period_label).strip()
        if s.endswith('y'):
            offset = int(s.replace('+', '').replace('y', ''))
            return datetime.datetime.utcnow().year + offset
        y = int(s)
        if 2000 <= y <= 2100:
            return y
    except Exception:
        pass
    return None


def yf_fetch_estimates(symbol):
    """
    Forward EPS/revenue estimates for FY1 and FY2 from yfinance.
    Returns same structure as fetch_estimates() for easy merging.

    yfinance earnings_estimate / revenue_estimate DataFrames:
        Index  : ['0q', '+1q', '0y', '+1y']   (periods)
        Columns: ['numberOfAnalysts', 'avg', 'low', 'high', ...]
    We want the '0y' (current FY → fy1) and '+1y' (next FY → fy2) rows.
    """
    result = {
        'fy1': {
            'year': None, 'period': None,
            'eps_avg': None, 'eps_high': None, 'eps_low': None, 'eps_analysts': None,
            'revenue_avg_millions': None, 'revenue_high_millions': None,
            'revenue_low_millions': None, 'revenue_analysts': None,
        },
        'fy2': {
            'year': None, 'period': None,
            'eps_avg': None, 'eps_high': None, 'eps_low': None, 'eps_analysts': None,
            'revenue_avg_millions': None, 'revenue_high_millions': None,
            'revenue_low_millions': None, 'revenue_analysts': None,
        },
        'premium_required': False,
        'source': 'yfinance',
    }

    # Map: fy_key → yfinance period index label
    PERIOD_MAP = [('fy1', '0y'), ('fy2', '+1y')]

    try:
        tk = yf.Ticker(symbol)

        # ── Earnings estimates (EPS) ──
        try:
            ee = tk.earnings_estimate
            if ee is not None and not ee.empty:
                for fy_key, period in PERIOD_MAP:
                    if period in ee.index:
                        result[fy_key]['eps_avg']      = _yf_safe(ee, period, 'avg')
                        result[fy_key]['eps_high']     = _yf_safe(ee, period, 'high')
                        result[fy_key]['eps_low']      = _yf_safe(ee, period, 'low')
                        result[fy_key]['eps_analysts'] = _yf_safe(ee, period, 'numberOfAnalysts')
                        result[fy_key]['year']         = _parse_fy_year(period)
                        result[fy_key]['period']       = period
        except Exception:
            pass

        # ── Revenue estimates ──
        try:
            re_ = tk.revenue_estimate
            if re_ is not None and not re_.empty:
                for fy_key, period in PERIOD_MAP:
                    if period in re_.index:
                        avg_val  = _yf_safe(re_, period, 'avg')
                        high_val = _yf_safe(re_, period, 'high')
                        low_val  = _yf_safe(re_, period, 'low')
                        result[fy_key]['revenue_avg_millions']  = to_millions(avg_val)
                        result[fy_key]['revenue_high_millions'] = to_millions(high_val)
                        result[fy_key]['revenue_low_millions']  = to_millions(low_val)
                        result[fy_key]['revenue_analysts']      = _yf_safe(re_, period, 'numberOfAnalysts')
                        if result[fy_key]['year'] is None:
                            result[fy_key]['year']   = _parse_fy_year(period)
                            result[fy_key]['period'] = period
        except Exception:
            pass

    except Exception:
        # yfinance rate-limited or unavailable — silent fallback
        pass

    return result


def yf_fetch_shareholders(symbol):
    """
    Top institutional holders from yfinance.
    Returns list of holder dicts compatible with fetch_ownership() output.
    """
    holders = []
    try:
        tk = yf.Ticker(symbol)
        ih = tk.institutional_holders
        if ih is not None and not ih.empty:
            for _, row in ih.head(10).iterrows():
                shares = row.get('Shares')
                pct    = row.get('% Out')
                date_r = row.get('Date Reported')
                holders.append({
                    'name':          str(row.get('Holder', '')),
                    'shares':        int(shares) if shares is not None else None,
                    'change':        None,  # yfinance doesn't provide change
                    'pct_held':      round(float(pct) * 100, 4) if pct is not None else None,
                    'value_millions': to_millions(row.get('Value')),
                    'date_reported': str(date_r.date()) if hasattr(date_r, 'date') else str(date_r)[:10] if date_r else '',
                })
    except Exception:
        # yfinance rate-limited or unavailable — silent fallback
        pass
    return holders


def yf_fetch_short_interest(symbol):
    """
    Short interest data from yfinance (via tk.info / defaultKeyStatistics).
    Returns a dict compatible with the short_interest block in the payload.
    """
    result = {
        'short_percent_of_float': None,
        'shares_short': None,
        'short_ratio': None,
        'float_shares': None,
        'note': None,
    }
    try:
        tk = yf.Ticker(symbol)
        info = tk.info or {}

        spf = info.get('shortPercentOfFloat')
        if spf is not None:
            result['short_percent_of_float'] = round(float(spf) * 100, 2)

        ss = info.get('sharesShort')
        if ss is not None:
            result['shares_short'] = int(ss)

        sr = info.get('shortRatio')
        if sr is not None:
            result['short_ratio'] = round(float(sr), 2)

        fs = info.get('floatShares')
        if fs is not None:
            result['float_shares'] = int(fs)

        # If we got at least one field, note the source
        has_data = any(result[k] is not None for k in ('short_percent_of_float', 'shares_short', 'short_ratio', 'float_shares'))
        if has_data:
            result['note'] = 'Short interest sourced from Yahoo Finance (yfinance).'
        else:
            result['note'] = 'Short interest data not available for this ticker.'
    except Exception:
        result['note'] = 'Short interest data temporarily unavailable (Yahoo Finance rate limit). Try again shortly.'
    return result


# ─── Master payload builder ──────────────────────────────────────────────────

def build_payload(symbol):
    all_errors = []
    client = get_client()

    # 1 — Profile (company name, shares outstanding etc.)
    profile, errs = fetch_profile(client, symbol)
    all_errors.extend(errs)

    # 2 — Quote + volume candle
    price_data, errs = fetch_quote_and_price(client, symbol)
    all_errors.extend(errs)

    # Validate — if we have no price at all, the symbol is probably invalid
    if not price_data['current']:
        return None, f'No price data found for "{symbol}". Check that the ticker is valid.'

    # 3 — Basic financials (metrics + time-series ratios)
    bf, errs = fetch_basic_financials(client, symbol)
    all_errors.extend(errs)

    # Merge 52-week range and market cap into price_data
    price_data['fifty_two_week_high'] = bf['wk52_high']
    price_data['fifty_two_week_low']  = bf['wk52_low']

    # Approximate daily USD volume: 10-day avg shares (millions) × current price
    # stock_candles requires Premium; this is the best free-tier alternative.
    avg_vol_10d_M = bf.get('avg_vol_10d_M_shares')  # already in millions of shares
    cur_price     = price_data.get('current')
    if avg_vol_10d_M and cur_price:
        price_data['daily_volume_usd_millions'] = round(avg_vol_10d_M * cur_price, 2)

    market_cap = bf['market_cap_millions'] or profile.get('market_cap_millions')

    # Shares outstanding in millions (from profile)
    shares_out_M = profile.get('shares_outstanding')  # in millions

    # 4 — Reported financials (absolute revenue, income, OCF etc.)
    reported, errs = fetch_reported_financials(client, symbol)
    all_errors.extend(errs)

    # Derive gross margin from reported if not in series
    gross_margin_pct     = bf['gross_margin_pct']
    operating_margin_pct = bf['operating_margin_pct']
    net_margin_pct       = bf['net_margin_pct']

    # If we have reported revenue and gross profit, compute margins from actuals
    if reported['revenue_millions'] and reported['gross_profit_millions'] and not gross_margin_pct:
        gross_margin_pct = round(reported['gross_profit_millions'] / reported['revenue_millions'] * 100, 2)
    if reported['revenue_millions'] and reported['operating_income_millions'] and not operating_margin_pct:
        operating_margin_pct = round(reported['operating_income_millions'] / reported['revenue_millions'] * 100, 2)
    if reported['revenue_millions'] and reported['net_income_millions'] and not net_margin_pct:
        net_margin_pct = round(reported['net_income_millions'] / reported['revenue_millions'] * 100, 2)

    # Current FY financials block
    current_fy = {
        'label':                        f"Current FY ({reported['period'] or 'LTM'})",
        'period':                        reported['period'],
        'fiscal_year':                   reported['fiscal_year'],
        'revenue_millions':              reported['revenue_millions'],
        'gross_margin_pct':              gross_margin_pct,
        'operating_margin_pct':          operating_margin_pct,
        'net_margin_pct':                net_margin_pct,
        'net_income_millions':           reported['net_income_millions'],
        'net_cash_millions':             reported['net_cash_millions'],
        'operating_cash_flow_millions':  reported['operating_cash_flow_millions'],
        'capex_millions':                reported['capex_millions'],
        'fcf_millions':                  reported['fcf_millions'],
    }

    # 5 — Forward estimates (Finnhub primary, yfinance secondary)
    estimates, errs = fetch_estimates(client, symbol)
    all_errors.extend(errs)
    estimates_note = None
    estimates_source = 'Finnhub'

    # If Finnhub estimates are empty or premium-gated, fall back to yfinance
    finnhub_est_empty = (
        estimates['premium_required']
        or (estimates['fy1']['eps_avg'] is None and estimates['fy1']['revenue_avg_millions'] is None)
    )
    if finnhub_est_empty:
        yf_est = yf_fetch_estimates(symbol)
        yf_has_data = (
            yf_est['fy1']['eps_avg'] is not None
            or yf_est['fy1']['revenue_avg_millions'] is not None
        )
        if yf_has_data:
            estimates = yf_est
            estimates_source = 'yfinance'
            estimates_note = 'Forward estimates sourced from Yahoo Finance (yfinance).'
        elif estimates['premium_required']:
            estimates_note = (
                'Forward estimates unavailable — Finnhub requires Premium and '
                'Yahoo Finance did not return data for this ticker.'
            )

    def est_block(fy_key):
        e = estimates[fy_key]
        rev_avg = e['revenue_avg_millions']
        eps_avg = e['eps_avg']
        shares_abs = (shares_out_M * 1_000_000) if shares_out_M else None
        net_inc_est = to_millions(eps_avg * shares_abs) if eps_avg and shares_abs else None

        # Forward PE: use current price / forward EPS
        cur_price = price_data['current']
        fwd_pe = round(cur_price / eps_avg, 2) if cur_price and eps_avg and eps_avg > 0 else None

        # Forward PS: market cap / forward revenue
        fwd_ps = round(market_cap / rev_avg, 2) if market_cap and rev_avg and rev_avg > 0 else None

        return {
            'year':                        e['year'],
            'period':                      e['period'],
            'label':                       f"FY{e['year']} (Est.)" if e['year'] else 'Next FY (Est.)',
            'eps_avg':                     eps_avg,
            'eps_high':                    e['eps_high'],
            'eps_low':                     e['eps_low'],
            'eps_analysts':                e['eps_analysts'],
            'revenue_avg_millions':        rev_avg,
            'revenue_high_millions':       e['revenue_high_millions'],
            'revenue_low_millions':        e['revenue_low_millions'],
            'revenue_analysts':            e['revenue_analysts'],
            'net_income_est_millions':     net_inc_est,
            'forward_pe':                  fwd_pe,
            'forward_ps':                  fwd_ps,
            # Margin estimates not available via Finnhub
            'gross_margin_pct':            None,
            'operating_margin_pct':        None,
            'fcf_millions':                None,
            'net_cash_millions':           None,
            'operating_cash_flow_millions': None,
            'capex_millions':              None,
        }

    next_fy  = est_block('fy1')
    next_fy2 = est_block('fy2')

    # 6 — Institutional ownership (Finnhub primary, yfinance secondary)
    shareholders, ownership_premium, errs = fetch_ownership(client, symbol)
    all_errors.extend(errs)
    ownership_note = None

    # If Finnhub ownership is empty or premium-gated, fall back to yfinance
    if not shareholders or ownership_premium:
        yf_holders = yf_fetch_shareholders(symbol)
        if yf_holders:
            shareholders = yf_holders
            ownership_note = 'Institutional holders sourced from Yahoo Finance (yfinance).'
        elif ownership_premium:
            ownership_note = (
                'Institutional ownership unavailable — Finnhub requires Premium and '
                'Yahoo Finance did not return data for this ticker.'
            )

    # 7 — Insider transactions (free)
    insider_txs, errs = fetch_insider_transactions(client, symbol)
    all_errors.extend(errs)

    # 8 — Valuation block
    trailing_pe  = bf['trailing_pe']
    trailing_eps = bf['trailing_eps']
    ps_ratio     = bf['ps_ratio']
    ev_millions  = bf['ev_millions']
    ev_ebitda    = bf['ev_ebitda']   # now populated from series_annual['evEbitda']
    ev_revenue   = bf['ev_revenue']  # now populated from series_annual['evRevenue']

    # 9 — Short interest (yfinance — not available on Finnhub)
    short_interest = yf_fetch_short_interest(symbol)

    payload = {
        'ticker':               symbol,
        'company_name':         profile['name'],
        'exchange':             profile['exchange'],
        'sector':               profile['sector'],
        'industry':             profile['industry'],
        'currency':             profile['currency'],
        'country':              profile['country'],
        'logo':                 profile['logo'],
        'weburl':               profile['weburl'],
        'shares_outstanding_M': shares_out_M,
        'market_cap_millions':  market_cap,
        'price': price_data,
        'valuation': {
            'trailing_pe':               trailing_pe,
            'trailing_eps':              trailing_eps,
            'ps_ratio':                  ps_ratio,
            'pb_ratio':                  bf['pb_ratio'],
            'pfcf':                      bf['pfcf'],
            'ev_ebitda':                 ev_ebitda,
            'ev_revenue':                ev_revenue,
            'enterprise_value_millions': ev_millions,
            'beta':                      bf['beta'],
        },
        'financials': {
            'current_fy': current_fy,
            'next_fy':    next_fy,
            'next_fy2':   next_fy2,
            'estimates_note': estimates_note,
        },
        'short_interest':    short_interest,
        'top_shareholders':  shareholders,
        'ownership_note':    ownership_note,
        'ceo_incentives': {
            'note': (
                'Detailed compensation (salary, option grants, RSU schedules) is filed in SEC DEF 14A '
                'proxy statements. Shown below are recent insider open-market transactions from Finnhub.'
            ),
            'executives':            [],   # Finnhub has no executive roster endpoint
            'recent_transactions':   insider_txs,
        },
        'errors': all_errors,
    }
    return payload, None


# ─── Routes ──────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/stock/<ticker_symbol>')
def get_stock_data(ticker_symbol):
    symbol = ticker_symbol.upper().strip()

    # Serve from cache if fresh
    now = time.time()
    if symbol in _cache:
        data, ts = _cache[symbol]
        if now - ts < CACHE_TTL:
            return jsonify(data)

    try:
        payload, error = build_payload(symbol)
        if error:
            return jsonify({'error': error}), 404
        _cache[symbol] = (payload, now)
        return jsonify(payload)
    except RuntimeError as e:
        # Missing API key
        return jsonify({'error': str(e)}), 500
    except Exception as e:
        return jsonify({
            'error': f'Failed to fetch data for "{symbol}": {str(e)}',
            'trace': traceback.format_exc(),
        }), 500


if __name__ == '__main__':
    app.run(debug=True, port=5000)
