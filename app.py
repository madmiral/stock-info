from flask import Flask, render_template, jsonify
from flask_cors import CORS
import yfinance as yf
import pandas as pd
import traceback
import time
import json

app = Flask(__name__)
CORS(app)

# ─── Simple TTL cache (5 minutes) ───────────────────────────────────────────
_cache = {}
CACHE_TTL = 300


# ─── Helpers ─────────────────────────────────────────────────────────────────

def safe_get(d, key, default=None):
    try:
        val = d.get(key, default)
        return val if val is not None else default
    except Exception:
        return default


def safe_df_row(df, row_labels, col_index=0, default=None):
    """Try multiple row label variants; return the first match."""
    if df is None or df.empty:
        return default
    labels = row_labels if isinstance(row_labels, list) else [row_labels]
    for label in labels:
        try:
            if label in df.index:
                val = df.loc[label].iloc[col_index]
                if not pd.isna(val):
                    return float(val)
        except Exception:
            continue
    return default


def to_millions(value, default=None):
    try:
        if value is None:
            return default
        return round(float(value) / 1_000_000, 2)
    except Exception:
        return default


def to_percent(value, default=None):
    try:
        if value is None:
            return default
        return round(float(value) * 100, 4)
    except Exception:
        return default


# ─── Data fetch functions ────────────────────────────────────────────────────

def fetch_price_data(info, fast_info):
    current_price = safe_get(info, 'currentPrice') or safe_get(info, 'regularMarketPrice')
    if current_price is None:
        try:
            current_price = float(fast_info.last_price)
        except Exception:
            current_price = None

    volume = safe_get(info, 'regularMarketVolume') or safe_get(info, 'averageVolume')
    daily_vol_usd = None
    if volume and current_price:
        daily_vol_usd = round(volume * current_price / 1_000_000, 2)

    return {
        'current': current_price,
        'fifty_two_week_high': safe_get(info, 'fiftyTwoWeekHigh'),
        'fifty_two_week_low': safe_get(info, 'fiftyTwoWeekLow'),
        'daily_volume_usd_millions': daily_vol_usd,
    }


def fetch_current_fy_financials(ticker):
    errors = []
    result = {
        'revenue_millions': None,
        'gross_margin_pct': None,
        'operating_margin_pct': None,
        'net_income_millions': None,
        'net_cash_millions': None,
        'operating_cash_flow_millions': None,
        'capex_millions': None,
        'fcf_millions': None,
        'label': 'Current FY (Actual)',
        'period': None,
    }
    try:
        income = ticker.income_stmt
        cashflow = ticker.cashflow
        balance = ticker.balance_sheet

        # Detect fiscal year end date
        if income is not None and not income.empty:
            try:
                result['period'] = str(income.columns[0].date())
            except Exception:
                pass

        revenue = safe_df_row(income, ['Total Revenue', 'Revenue'])
        gross_profit = safe_df_row(income, ['Gross Profit'])
        op_income = safe_df_row(income, ['Operating Income', 'Ebit'])
        net_income = safe_df_row(income, ['Net Income', 'Net Income Common Stockholders'])
        op_cash_flow = safe_df_row(cashflow, ['Operating Cash Flow', 'Cash Flow From Operations'])
        capex = safe_df_row(cashflow, ['Capital Expenditure', 'Capital Expenditures'])
        cash = safe_df_row(balance, [
            'Cash And Cash Equivalents',
            'Cash Cash Equivalents And Short Term Investments',
            'Cash And Cash Equivalents And Short Term Investments',
        ])
        total_debt = safe_df_row(balance, ['Total Debt', 'Long Term Debt And Capital Lease Obligation'])

        gross_margin = (gross_profit / revenue) if revenue and gross_profit else None
        op_margin = (op_income / revenue) if revenue and op_income else None
        # capex is typically stored as a negative number
        capex_abs = abs(capex) if capex is not None else None
        fcf = (op_cash_flow + capex) if op_cash_flow is not None and capex is not None else None
        net_cash = (cash - total_debt) if cash is not None and total_debt is not None else None

        result.update({
            'revenue_millions': to_millions(revenue),
            'gross_margin_pct': to_percent(gross_margin),
            'operating_margin_pct': to_percent(op_margin),
            'net_income_millions': to_millions(net_income),
            'net_cash_millions': to_millions(net_cash),
            'operating_cash_flow_millions': to_millions(op_cash_flow),
            'capex_millions': to_millions(capex_abs),
            'fcf_millions': to_millions(fcf),
        })
    except Exception as e:
        errors.append(f'Current FY financials: {str(e)}')
    return result, errors


def fetch_next_fy_estimates(ticker, info):
    errors = []
    result = {
        'revenue_millions': None,
        'gross_margin_pct': None,
        'operating_margin_pct': None,
        'net_income_millions': None,
        'net_cash_millions': None,
        'operating_cash_flow_millions': None,
        'capex_millions': None,
        'fcf_millions': None,
        'eps_estimate': None,
        'label': 'Next FY (Analyst Est.)',
        'period': None,
        'data_notes': 'Margin, net cash, OCF, capex and FCF estimates are not available via free APIs.',
    }
    try:
        rev_est = ticker.revenue_estimate
        earn_est = ticker.earnings_estimate

        if rev_est is not None and not rev_est.empty and '+1y' in rev_est.index:
            next_rev = rev_est.loc['+1y', 'avg'] if 'avg' in rev_est.columns else None
            result['revenue_millions'] = to_millions(next_rev)

        if earn_est is not None and not earn_est.empty and '+1y' in earn_est.index:
            next_eps = earn_est.loc['+1y', 'avg'] if 'avg' in earn_est.columns else None
            result['eps_estimate'] = float(next_eps) if next_eps and not pd.isna(next_eps) else None
            shares = safe_get(info, 'sharesOutstanding')
            if result['eps_estimate'] and shares:
                result['net_income_millions'] = to_millions(result['eps_estimate'] * shares)

    except Exception as e:
        errors.append(f'Next FY estimates: {str(e)}')
    return result, errors


def fetch_valuation(info):
    return {
        'trailing_pe': safe_get(info, 'trailingPE'),
        'forward_pe': safe_get(info, 'forwardPE'),
        'ps_ratio': safe_get(info, 'priceToSalesTrailing12Months'),
        'ev_ebitda': safe_get(info, 'enterpriseToEbitda'),
        'ev_revenue': safe_get(info, 'enterpriseToRevenue'),
        'enterprise_value_millions': to_millions(safe_get(info, 'enterpriseValue')),
    }


def fetch_short_interest(info):
    short_pct = safe_get(info, 'shortPercentOfFloat')
    return {
        'short_percent_of_float': to_percent(short_pct) if short_pct else None,
        'shares_short': safe_get(info, 'sharesShort'),
        'short_ratio': safe_get(info, 'shortRatio'),
        'float_shares': safe_get(info, 'floatShares'),
        'shares_short_prior_month': safe_get(info, 'sharesShortPriorMonth'),
    }


def fetch_shareholders(ticker):
    errors = []
    holders = []
    try:
        inst = ticker.institutional_holders
        if inst is not None and not inst.empty:
            top10 = inst.head(10)
            for _, row in top10.iterrows():
                shares_val = row.get('Shares')
                pct_held = row.get('pctHeld') or row.get('% Out')
                value_val = row.get('Value')
                date_val = row.get('Date Reported')
                holders.append({
                    'name': str(row.get('Holder', 'N/A')),
                    'shares': int(shares_val) if shares_val is not None and not pd.isna(shares_val) else None,
                    'pct_held': to_percent(pct_held) if pct_held is not None and not pd.isna(pct_held) else None,
                    'value_millions': to_millions(value_val) if value_val is not None and not pd.isna(value_val) else None,
                    'date_reported': str(date_val.date()) if hasattr(date_val, 'date') else (str(date_val) if date_val and not pd.isna(date_val) else None),
                })
    except Exception as e:
        errors.append(f'Shareholders: {str(e)}')
    return holders, errors


def fetch_ceo_incentives(ticker):
    errors = []
    result = {
        'note': (
            'Detailed compensation data (base salary, bonus targets, stock option grants, RSU schedules) '
            'is filed in SEC DEF 14A proxy statements and is not available via free APIs. '
            'Shown below are insider roster positions and recent open-market transactions as a proxy.'
        ),
        'executives': [],
        'recent_transactions': [],
    }
    try:
        roster = ticker.insider_roster_holders
        if roster is not None and not roster.empty:
            for _, row in roster.iterrows():
                shares_val = row.get('Shares') or row.get('sharesOwnedDirectly')
                result['executives'].append({
                    'name': str(row.get('Name', 'N/A')),
                    'position': str(row.get('Position', 'N/A')),
                    'shares_held': int(shares_val) if shares_val is not None and not pd.isna(shares_val) else None,
                })
    except Exception as e:
        errors.append(f'Insider roster: {str(e)}')

    try:
        transactions = ticker.insider_transactions
        if transactions is not None and not transactions.empty:
            recent = transactions.head(15)
            for _, row in recent.iterrows():
                shares_val = row.get('Shares')
                value_val = row.get('Value')
                date_val = row.get('Start Date')
                result['recent_transactions'].append({
                    'name': str(row.get('Insider', row.get('Name', 'N/A'))),
                    'title': str(row.get('Title', row.get('Position', 'N/A'))),
                    'transaction': str(row.get('Transaction', row.get('Type', 'N/A'))),
                    'shares': int(shares_val) if shares_val is not None and not pd.isna(shares_val) else None,
                    'value_millions': to_millions(value_val) if value_val is not None and not pd.isna(value_val) else None,
                    'date': str(date_val.date()) if hasattr(date_val, 'date') else (str(date_val) if date_val else None),
                })
    except Exception as e:
        errors.append(f'Insider transactions: {str(e)}')

    return result, errors


RATE_LIMIT_STRINGS = ('429', 'Too Many Requests', 'rate limit')


def is_rate_limit_error(e):
    if isinstance(e, (json.JSONDecodeError, ValueError)) and 'Expecting value' in str(e):
        return True
    s = str(e)
    return any(r in s for r in RATE_LIMIT_STRINGS)


def build_stock_payload(ticker_symbol):
    all_errors = []
    ticker = yf.Ticker(ticker_symbol)

    # Fetch info with retry on rate-limit
    info = {}
    last_exc = None
    for attempt in range(3):
        try:
            info = ticker.info
            # yfinance can return an empty dict on rate limit without raising
            if not info or len(info) < 5:
                raise ValueError('Empty info dict returned — possible rate limit')
            last_exc = None
            break
        except Exception as e:
            last_exc = e
            wait = 3 + attempt * 3
            time.sleep(wait)
            if attempt == 2:
                return None, 'Yahoo Finance is rate-limiting requests. Please wait ~30 seconds and try again.'

    # Validate ticker
    price_check = info.get('currentPrice') or info.get('regularMarketPrice')
    if not price_check:
        try:
            price_check = ticker.fast_info.last_price
        except Exception:
            price_check = None
    if not price_check:
        return None, f'Ticker "{ticker_symbol}" not found or has no price data.'

    fast_info = ticker.fast_info
    price_data = fetch_price_data(info, fast_info)
    current_fy, errs = fetch_current_fy_financials(ticker)
    all_errors.extend(errs)
    next_fy, errs = fetch_next_fy_estimates(ticker, info)
    all_errors.extend(errs)
    valuation = fetch_valuation(info)
    short_interest = fetch_short_interest(info)
    shareholders, errs = fetch_shareholders(ticker)
    all_errors.extend(errs)
    ceo_incentives, errs = fetch_ceo_incentives(ticker)
    all_errors.extend(errs)

    payload = {
        'ticker': ticker_symbol,
        'company_name': safe_get(info, 'longName', ticker_symbol),
        'sector': safe_get(info, 'sector'),
        'industry': safe_get(info, 'industry'),
        'market_cap_millions': to_millions(safe_get(info, 'marketCap')),
        'currency': safe_get(info, 'currency', 'USD'),
        'exchange': safe_get(info, 'exchange') or safe_get(info, 'fullExchangeName'),
        'price': price_data,
        'valuation': valuation,
        'financials': {
            'current_fy': current_fy,
            'next_fy': next_fy,
        },
        'short_interest': short_interest,
        'top_shareholders': shareholders,
        'ceo_incentives': ceo_incentives,
        'errors': all_errors,
    }
    return payload, None


# ─── Routes ──────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/stock/<ticker_symbol>')
def get_stock_data(ticker_symbol):
    ticker_symbol = ticker_symbol.upper().strip()

    # Check cache
    now = time.time()
    if ticker_symbol in _cache:
        data, ts = _cache[ticker_symbol]
        if now - ts < CACHE_TTL:
            return jsonify(data)

    try:
        payload, error = build_stock_payload(ticker_symbol)
        if error:
            status = 429 if 'rate' in error.lower() else 404
            return jsonify({'error': error}), status
        _cache[ticker_symbol] = (payload, now)
        return jsonify(payload)
    except Exception as e:
        err_str = str(e)
        if is_rate_limit_error(e):
            return jsonify({'error': 'Yahoo Finance is rate-limiting requests. Please wait ~30 seconds and try again.'}), 429
        return jsonify({
            'error': f'Failed to fetch data for "{ticker_symbol}": {err_str}',
            'trace': traceback.format_exc(),
        }), 500


if __name__ == '__main__':
    app.run(debug=True, port=5000)
