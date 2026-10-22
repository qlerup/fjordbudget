from collections import defaultdict
from datetime import date, timedelta
from math import ceil
from statistics import median
import re

from db import connect, transaction_title



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


def _spending_opportunities(merchant_spend, preferences, months_equivalent, period_months, excluded_keys=()):
    """Find plausible one-fewer-visit opportunities based on real purchases.

    Unlike flat percentage suggestions, each estimate uses the median of
    actual purchase amounts and skips one-off spending.
    """
    opportunities = []
    for name, item in merchant_spend.items():
        if _merchant_key(name) in excluded_keys or not preferences.get(_merchant_key(name), True):
            continue
        amounts = item['amounts']
        active_months = len(item['months'])
        visits_per_month = len(amounts) / months_equivalent
        if len(amounts) < 2 or visits_per_month < 1.5:
            continue
        if period_months > 1 and active_months < 2:
            continue
        monthly_average = int(round(item['total'] / months_equivalent))
        typical_purchase = int(median(amounts))
        saving = min(typical_purchase, monthly_average // 2)
        if saving < 2500:
            continue
        category = max(item['categories'], key=item['categories'].get)
        opportunities.append({
            'type': 'merchant', 'name': name, 'category': category,
            'monthly_average': monthly_average,
            'suggested_cut': saving, 'yearly_effect': saving * 12,
            'purchases': len(amounts), 'active_months': active_months,
            'average_visits_per_month': round(visits_per_month, 1),
            'typical_purchase': typical_purchase, 'suggestion_kind': 'skip_one_visit',
        })
    opportunities.sort(key=lambda item: (-item['suggested_cut'], item['name'].casefold()))
    return opportunities


def last_month_spending(db, source, currency, today=None):
    """Top four adjustable merchants in the last completed calendar month.

    The dashboard is independent of savings goals and the month picker.
    Recorded spend is not a claim that every purchase was unnecessary.
    """
    today = today or date.today()
    month = (today.replace(day=1) - timedelta(days=1)).strftime('%Y-%m')
    preferences = {row['merchant_key']: bool(row['adjustable']) for row in db.execute(
        'SELECT merchant_key,adjustable FROM merchant_preferences WHERE source=?', (source,))}
    grouped = {}
    for row in db.execute(
        '''SELECT t.merchant,COUNT(*) purchases,SUM(-t.amount) total
           FROM transactions t JOIN accounts a ON a.id=t.account_id
           WHERE a.source=? AND a.included=1 AND a.selection_pending=0
             AND t.currency=? AND substr(t.booked_on,1,7)=?
             AND t.amount<0 AND t.category!='Overførsler'
             AND t.merchant IS NOT NULL AND trim(t.merchant)!=''
           GROUP BY t.merchant''', (source, currency, month)):
        name = row['merchant'].strip()
        key = _merchant_key(name)
        if not key or not preferences.get(key, True):
            continue
        item = grouped.setdefault(key, {'name': name, 'spent': 0, 'purchases': 0})
        item['spent'] += int(row['total'])
        item['purchases'] += int(row['purchases'])
    items = sorted(grouped.values(), key=lambda item: (-item['spent'], item['name'].casefold()))
    return {'month': month, 'items': items[:4]}


def _account_filter(account_ids):
    if account_ids is None:
        return 'a.included=1', ()
    if not account_ids:
        return '0=1', ()
    return 't.account_id IN (' + ','.join('?' for _ in account_ids) + ')', tuple(account_ids)


def _history_months(db, source, currency, today, account_ids=None):
    account_filter, account_params = _account_filter(account_ids)
    current = today.strftime('%Y-%m')
    months = [row[0] for row in db.execute(
        f'''SELECT DISTINCT substr(t.booked_on,1,7)
           FROM transactions t JOIN accounts a ON a.id=t.account_id
           WHERE a.source=? AND {account_filter} AND a.selection_pending=0 AND t.currency=? AND substr(t.booked_on,1,7)<=?
           ORDER BY 1 DESC LIMIT 7''',
        (source, *account_params, currency, current))]
    completed = [month for month in months if month < current][:6]
    return completed if len(completed) >= 2 else months[:6]


def _profile(db, source, currency, today, account_ids=None, excluded_keys=()):
    account_filter, account_params = _account_filter(account_ids)
    months = _history_months(db, source, currency, today, account_ids)
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
            WHERE a.source=? AND {account_filter} AND a.selection_pending=0 AND t.currency=? AND substr(t.booked_on,1,7) IN ({placeholders})
            GROUP BY 1''',
        (source, *account_params, currency, *months)):
        monthly[row['month']] = {'income': row['income'] or 0, 'expenses': row['expenses'] or 0}

    category_spend = defaultdict(int)
    merchant_spend = defaultdict(lambda: {'total': 0, 'count': 0, 'months': set(), 'categories': defaultdict(int), 'amounts': []})
    for row in db.execute(
        f'''SELECT substr(t.booked_on,1,7) month,t.amount,t.category,t.merchant
            FROM transactions t JOIN accounts a ON a.id=t.account_id
            WHERE a.source=? AND {account_filter} AND a.selection_pending=0 AND t.currency=? AND t.amount<0 AND t.category!='Overførsler'
              AND substr(t.booked_on,1,7) IN ({placeholders})''',
        (source, *account_params, currency, *months)):
        spend = -row['amount']
        category_spend[row['category']] += spend
        merchant = (row['merchant'] or '').strip()
        if merchant:
            item = merchant_spend[merchant]
            item['total'] += spend
            item['count'] += 1
            item['months'].add(row['month'])
            item['amounts'].append(spend)
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

    opportunities = _spending_opportunities(merchant_spend, merchant_preferences, month_count, month_count, excluded_keys)

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
        'opportunities': opportunities,
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


def _period_profile(db, source, currency, start_date, end_date, account_ids=None, excluded_keys=()):
    account_filter, account_params = _account_filter(account_ids)
    start_iso = start_date.isoformat()
    end_iso = end_date.isoformat()
    period_days = (end_date - start_date).days + 1
    month_equivalent = _month_equivalent(start_date, end_date)

    monthly_rows = list(db.execute(
        f'''SELECT substr(t.booked_on,1,7) month,
                  SUM(CASE WHEN t.amount>0 AND t.category!='Overførsler' THEN t.amount ELSE 0 END) income,
                  SUM(CASE WHEN t.amount<0 AND t.category!='Overførsler' THEN -t.amount ELSE 0 END) expenses
           FROM transactions t JOIN accounts a ON a.id=t.account_id
           WHERE a.source=? AND {account_filter} AND a.selection_pending=0 AND t.currency=?
             AND t.booked_on BETWEEN ? AND ?
           GROUP BY 1 ORDER BY 1''',
        (source, *account_params, currency, start_iso, end_iso)))

    total_income = sum(int(row['income'] or 0) for row in monthly_rows)
    total_expenses = sum(int(row['expenses'] or 0) for row in monthly_rows)
    total_available = total_income - total_expenses

    category_spend = defaultdict(int)
    merchant_spend = defaultdict(lambda: {'total': 0, 'count': 0, 'months': set(), 'categories': defaultdict(int), 'amounts': []})
    for row in db.execute(
        f'''SELECT substr(t.booked_on,1,7) month,t.amount,t.category,t.merchant
           FROM transactions t JOIN accounts a ON a.id=t.account_id
           WHERE a.source=? AND {account_filter} AND a.selection_pending=0 AND t.currency=?
             AND t.amount<0 AND t.category!='Overførsler'
             AND t.booked_on BETWEEN ? AND ?''',
        (source, *account_params, currency, start_iso, end_iso)):
        spend = -row['amount']
        category_spend[row['category']] += spend
        merchant = (row['merchant'] or '').strip()
        if merchant:
            item = merchant_spend[merchant]
            item['total'] += spend
            item['count'] += 1
            item['months'].add(row['month'])
            item['amounts'].append(spend)
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

    opportunities = _spending_opportunities(merchant_spend, merchant_preferences, month_equivalent, ceil(month_equivalent), excluded_keys)

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
        'opportunities': opportunities,
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
    suggestions = [{**item, 'recommended_cut': item['suggested_cut']}
                   for item in profile['opportunities']]

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
        selected_by_goal = defaultdict(list)
        if goals:
            placeholders = ','.join('?' for _ in goals)
            for row in db.execute(
                f'''SELECT ga.goal_id,ga.account_id
                    FROM savings_goal_analysis_accounts ga
                    JOIN accounts a ON a.id=ga.account_id
                    WHERE ga.goal_id IN ({placeholders}) AND a.source=? AND a.currency=?
                      AND a.selection_pending=0
                    ORDER BY ga.account_id''',
                (*[goal['id'] for goal in goals], source, currency)):
                selected_by_goal[row['goal_id']].append(row['account_id'])

        scoped_profiles = {}
        suggestion_month = today.strftime('%Y-%m')
        skipped_by_goal = defaultdict(list)
        for row in db.execute(
            '''SELECT s.goal_id,s.merchant_key,s.name FROM savings_suggestion_skips s
               JOIN savings_goals g ON g.id=s.goal_id
               WHERE g.source=? AND g.currency=? AND s.month=? ORDER BY s.name,s.merchant_key''',
            (source, currency, suggestion_month)):
            skipped_by_goal[row['goal_id']].append(dict(row))
        for goal in goals:
            goal['manual_saved_amount'] = int(goal['saved_amount'] or 0)
            goal['uses_live_balance'] = bool(goal.get('account_id') and goal.get('account_name'))
            goal['account_balance_available'] = bool(goal['uses_live_balance'] and goal.get('account_balance') is not None)
            if goal['uses_live_balance']:
                goal['saved_amount'] = int(goal['account_balance']) if goal['account_balance'] is not None else 0

            account_ids = tuple(selected_by_goal[goal['id']])
            goal['analysis_account_ids'] = list(account_ids)
            skipped = skipped_by_goal[goal['id']]
            excluded_keys = tuple(sorted(item['merchant_key'] for item in skipped))
            profile_key = (account_ids, excluded_keys)
            if profile_key not in scoped_profiles:
                scoped_profiles[profile_key] = (
                    _period_profile(db, source, currency, period_start, period_end, account_ids, excluded_keys)
                    if period_start is not None and period_end is not None
                    else _profile(db, source, currency, today, account_ids, excluded_keys)
                )
            goal['analysis'] = _goal_analysis(goal, scoped_profiles[profile_key], today)
            goal['analysis']['suggestion_month'] = suggestion_month
            goal['analysis']['skipped_suggestions'] = [{'name': item['name']} for item in skipped]

    featured = next((goal for goal in goals if goal['featured']), goals[0] if goals else None)
    return {
        'profile': profile,
        'goals': goals,
        'featured_goal': featured,
    }
