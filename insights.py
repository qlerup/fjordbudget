from collections import defaultdict
from datetime import date, timedelta
from math import ceil
import re

from db import connect, transaction_title


MERCHANT_SAVINGS_RATE = 0.10


def _merchant_key(value):
    return re.sub(r"[\s'’`´.\-]+", '', transaction_title(value))


def _month_number(value):
    year, month = map(int, value.split('-'))
    return year * 12 + month - 1


def _month_label(number):
    year, month0 = divmod(number, 12)
    return f'{year:04d}-{month0 + 1:02d}'


def _average(values):
    return int(round(sum(values) / len(values))) if values else 0


def _history_months(db, source, currency, today):
    current = today.strftime('%Y-%m')
    months = [row[0] for row in db.execute(
        '''SELECT DISTINCT substr(t.booked_on,1,7)
           FROM transactions t JOIN accounts a ON a.id=t.account_id
           WHERE a.source=? AND a.included=1 AND a.selection_pending=0 AND t.currency=? AND substr(t.booked_on,1,7)<=?
           ORDER BY 1 DESC LIMIT 7''',
        (source, currency, current))]
    completed = [month for month in months if month < current][:6]
    return completed if len(completed) >= 2 else months[:6]


def _profile(db, source, currency, today):
    months = _history_months(db, source, currency, today)
    if not months:
        return {
            'months': [], 'months_analyzed': 0, 'average_income': 0,
            'average_expenses': 0, 'average_available': 0,
            'categories': [], 'merchants': [], 'opportunities': [],
        }

    placeholders = ','.join('?' for _ in months)
    monthly = {month: {'income': 0, 'expenses': 0} for month in months}
    for row in db.execute(
        f'''SELECT substr(t.booked_on,1,7) month,
                   SUM(CASE WHEN t.amount>0 AND t.category!='Overførsler' THEN t.amount ELSE 0 END) income,
                   SUM(CASE WHEN t.amount<0 AND t.category!='Overførsler' THEN -t.amount ELSE 0 END) expenses
            FROM transactions t JOIN accounts a ON a.id=t.account_id
            WHERE a.source=? AND a.included=1 AND a.selection_pending=0 AND t.currency=? AND substr(t.booked_on,1,7) IN ({placeholders})
            GROUP BY 1''',
        (source, currency, *months)):
        monthly[row['month']] = {'income': row['income'] or 0, 'expenses': row['expenses'] or 0}

    category_spend = defaultdict(int)
    merchant_spend = defaultdict(lambda: {'total': 0, 'count': 0, 'months': set(), 'categories': defaultdict(int)})
    for row in db.execute(
        f'''SELECT substr(t.booked_on,1,7) month,t.amount,t.category,t.merchant
            FROM transactions t JOIN accounts a ON a.id=t.account_id
            WHERE a.source=? AND a.included=1 AND a.selection_pending=0 AND t.currency=? AND t.amount<0 AND t.category!='Overførsler'
              AND substr(t.booked_on,1,7) IN ({placeholders})''',
        (source, currency, *months)):
        spend = -row['amount']
        category_spend[row['category']] += spend
        merchant = (row['merchant'] or '').strip()
        if merchant:
            item = merchant_spend[merchant]
            item['total'] += spend
            item['count'] += 1
            item['months'].add(row['month'])
            item['categories'][row['category']] += spend

    month_count = len(months)
    categories = [{
        'name': name,
        'monthly_average': int(round(total / month_count)),
        'total': total,
    } for name, total in category_spend.items()]
    categories.sort(key=lambda item: item['monthly_average'], reverse=True)

    merchant_preferences = {row['merchant_key']: bool(row['adjustable']) for row in db.execute(
        'SELECT merchant_key,adjustable FROM merchant_preferences WHERE source=?', (source,))}
    merchants = []
    for name, item in merchant_spend.items():
        category = max(item['categories'], key=item['categories'].get)
        monthly_average = int(round(item['total'] / month_count))
        merchants.append({
            'name': name,
            'category': category,
            'monthly_average': monthly_average,
            'total': item['total'],
            'purchases': item['count'],
            'active_months': len(item['months']),
            'adjustable': merchant_preferences.get(_merchant_key(name), True),
        })
    merchants.sort(key=lambda item: item['monthly_average'], reverse=True)

    opportunities = []
    for merchant in merchants:
        if not merchant['adjustable']:
            continue
        saving = int(round(merchant['monthly_average'] * MERCHANT_SAVINGS_RATE))
        if saving >= 2500:
            opportunities.append({
                'type': 'merchant', 'name': merchant['name'], 'category': merchant['category'],
                'monthly_average': merchant['monthly_average'], 'suggested_cut': saving,
                'yearly_effect': saving * 12, 'purchases': merchant['purchases'],
            })

    opportunities.sort(key=lambda item: item['suggested_cut'], reverse=True)

    incomes = [monthly[month]['income'] for month in months]
    expenses = [monthly[month]['expenses'] for month in months]
    available = [monthly[month]['income'] - monthly[month]['expenses'] for month in months]
    return {
        'months': list(reversed(months)),
        'months_analyzed': month_count,
        'average_income': _average(incomes),
        'average_expenses': _average(expenses),
        'average_available': _average(available),
        'categories': categories,
        'merchants': merchants,
        'opportunities': opportunities[:10],
    }



def _month_equivalent(start_date, end_date):
    cursor = start_date.replace(day=1)
    total = 0.0
    while cursor <= end_date:
        if cursor.month == 12:
            next_month = cursor.replace(year=cursor.year + 1, month=1, day=1)
        else:
            next_month = cursor.replace(month=cursor.month + 1, day=1)
        month_end = next_month - timedelta(days=1)
        overlap_start = max(start_date, cursor)
        overlap_end = min(end_date, month_end)
        if overlap_start <= overlap_end:
            overlap_days = (overlap_end - overlap_start).days + 1
            month_days = (month_end - cursor).days + 1
            total += overlap_days / month_days
        cursor = next_month
    return max(total, 1 / 31)


def _period_profile(db, source, currency, start_date, end_date):
    start_iso = start_date.isoformat()
    end_iso = end_date.isoformat()
    period_days = (end_date - start_date).days + 1
    month_equivalent = _month_equivalent(start_date, end_date)

    monthly_rows = list(db.execute(
        '''SELECT substr(t.booked_on,1,7) month,
                  SUM(CASE WHEN t.amount>0 AND t.category!='Overførsler' THEN t.amount ELSE 0 END) income,
                  SUM(CASE WHEN t.amount<0 AND t.category!='Overførsler' THEN -t.amount ELSE 0 END) expenses
           FROM transactions t JOIN accounts a ON a.id=t.account_id
           WHERE a.source=? AND a.included=1 AND a.selection_pending=0 AND t.currency=?
             AND t.booked_on BETWEEN ? AND ?
           GROUP BY 1 ORDER BY 1''',
        (source, currency, start_iso, end_iso)))

    total_income = sum(int(row['income'] or 0) for row in monthly_rows)
    total_expenses = sum(int(row['expenses'] or 0) for row in monthly_rows)
    total_available = total_income - total_expenses

    category_spend = defaultdict(int)
    merchant_spend = defaultdict(lambda: {'total': 0, 'count': 0, 'months': set(), 'categories': defaultdict(int)})
    for row in db.execute(
        '''SELECT substr(t.booked_on,1,7) month,t.amount,t.category,t.merchant
           FROM transactions t JOIN accounts a ON a.id=t.account_id
           WHERE a.source=? AND a.included=1 AND a.selection_pending=0 AND t.currency=?
             AND t.amount<0 AND t.category!='Overførsler'
             AND t.booked_on BETWEEN ? AND ?''',
        (source, currency, start_iso, end_iso)):
        spend = -row['amount']
        category_spend[row['category']] += spend
        merchant = (row['merchant'] or '').strip()
        if merchant:
            item = merchant_spend[merchant]
            item['total'] += spend
            item['count'] += 1
            item['months'].add(row['month'])
            item['categories'][row['category']] += spend

    categories = [{
        'name': name,
        'monthly_average': int(round(total / month_equivalent)),
        'total': total,
    } for name, total in category_spend.items()]
    categories.sort(key=lambda item: item['total'], reverse=True)

    merchant_preferences = {row['merchant_key']: bool(row['adjustable']) for row in db.execute(
        'SELECT merchant_key,adjustable FROM merchant_preferences WHERE source=?', (source,))}
    merchants = []
    for name, item in merchant_spend.items():
        category = max(item['categories'], key=item['categories'].get)
        monthly_average = int(round(item['total'] / month_equivalent))
        merchants.append({
            'name': name,
            'category': category,
            'monthly_average': monthly_average,
            'total': item['total'],
            'purchases': item['count'],
            'active_months': len(item['months']),
            'adjustable': merchant_preferences.get(_merchant_key(name), True),
        })
    merchants.sort(key=lambda item: item['total'], reverse=True)

    opportunities = []
    for merchant in merchants:
        if not merchant['adjustable']:
            continue
        saving = int(round(merchant['monthly_average'] * MERCHANT_SAVINGS_RATE))
        if saving >= 2500:
            opportunities.append({
                'type': 'merchant', 'name': merchant['name'], 'category': merchant['category'],
                'monthly_average': merchant['monthly_average'], 'suggested_cut': saving,
                'yearly_effect': saving * 12, 'purchases': merchant['purchases'],
            })

    opportunities.sort(key=lambda item: item['suggested_cut'], reverse=True)

    return {
        'period_start': start_iso,
        'period_end': end_iso,
        'period_days': period_days,
        'months': [row['month'] for row in monthly_rows],
        'months_analyzed': len(monthly_rows),
        'total_income': total_income,
        'total_expenses': total_expenses,
        'total_available': total_available,
        'average_income': int(round(total_income / month_equivalent)),
        'average_expenses': int(round(total_expenses / month_equivalent)),
        'average_available': int(round(total_available / month_equivalent)),
        'categories': categories,
        'merchants': merchants,
        'opportunities': opportunities[:10],
    }

def _goal_analysis(goal, profile, today):
    target = int(goal['target_amount'])
    saved = int(goal.get('saved_amount') or 0)
    remaining = max(0, target - saved)
    current_month = _month_number(today.strftime('%Y-%m'))
    deadline_month = _month_number(goal['deadline'][:7])
    months_remaining = max(0, deadline_month - current_month + 1)
    required = ceil(remaining / months_remaining) if remaining and months_remaining else (remaining if remaining else 0)
    available = profile['average_available']
    positive_available = max(0, available)
    gap = max(0, required - positive_available)

    potential_total = sum(item['suggested_cut'] for item in profile['opportunities'])
    suggestions = []
    if gap:
        still_needed = gap
        for item in profile['opportunities']:
            if still_needed <= 0:
                break
            amount = min(item['suggested_cut'], still_needed)
            suggestions.append({**item, 'recommended_cut': amount, 'yearly_effect': amount * 12})
            still_needed -= amount
    else:
        suggestions = [{**item, 'recommended_cut': item['suggested_cut']} for item in profile['opportunities'][:3]]

    if remaining == 0:
        status = 'complete'
    elif months_remaining == 0:
        status = 'expired'
    elif not profile['months_analyzed']:
        status = 'insufficient_data'
    elif required <= positive_available * 0.8:
        status = 'realistic'
    elif required <= positive_available:
        status = 'tight'
    elif required <= positive_available + potential_total:
        status = 'possible_with_changes'
    else:
        status = 'not_realistic'

    projected_deadline = None
    if remaining and positive_available > 0:
        months_needed = ceil(remaining / positive_available)
        projected_deadline = _month_label(current_month + months_needed - 1)

    improved_available = positive_available + sum(item['recommended_cut'] for item in suggestions)
    projected_with_changes = None
    if remaining and improved_available > 0:
        months_needed = ceil(remaining / improved_available)
        projected_with_changes = _month_label(current_month + months_needed - 1)

    return {
        'status': status,
        'remaining': remaining,
        'months_remaining': months_remaining,
        'required_monthly': required,
        'average_income': profile['average_income'],
        'average_expenses': profile['average_expenses'],
        'average_available': available,
        'monthly_shortfall': gap,
        'potential_extra_saving': potential_total,
        'projected_deadline': projected_deadline,
        'projected_with_changes': projected_with_changes,
        'max_by_deadline': saved + positive_available * months_remaining,
        'max_with_suggestions': saved + (positive_available + potential_total) * months_remaining,
        'months_analyzed': profile['months_analyzed'],
        'suggestions': suggestions,
    }


def build_insights(db_path, source, currency, today=None, period_start=None, period_end=None):
    today = today or date.today()
    with connect(db_path) as db:
        profile = (_period_profile(db, source, currency, period_start, period_end)
                   if period_start is not None and period_end is not None
                   else _profile(db, source, currency, today))
        goals = [dict(row) for row in db.execute(
            '''SELECT g.id,g.name,g.target_amount,g.saved_amount,substr(g.deadline,1,7) deadline,
                      g.currency,g.featured,g.account_id,
                      COALESCE(a.custom_name,a.name) account_name,a.bank account_bank,a.last4 account_last4,
                      a.balance account_balance,a.synced_at account_synced_at
               FROM savings_goals g
               LEFT JOIN accounts a ON a.id=g.account_id AND a.source=g.source AND a.currency=g.currency
               WHERE g.source=? AND g.currency=? ORDER BY g.featured DESC,g.deadline,g.id''',
            (source, currency))]
    for goal in goals:
        goal['manual_saved_amount'] = int(goal['saved_amount'] or 0)
        goal['uses_live_balance'] = bool(goal.get('account_id') and goal.get('account_name'))
        goal['account_balance_available'] = bool(goal['uses_live_balance'] and goal.get('account_balance') is not None)
        if goal['uses_live_balance']:
            goal['saved_amount'] = int(goal['account_balance']) if goal['account_balance'] is not None else 0
        goal['analysis'] = _goal_analysis(goal, profile, today)
    featured = next((goal for goal in goals if goal['featured']), goals[0] if goals else None)
    return {
        'profile': profile,
        'goals': goals,
        'featured_goal': featured,
    }
