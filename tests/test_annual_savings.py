import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app
from db import connect
from insights import _savings_history_profile, build_insights


class AnnualSavingsTests(unittest.TestCase):
    today = date(2026, 10, 22)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        app = create_app({'TESTING': True, 'DATA_DIR': self.temp.name})
        self.db = app.extensions['db_path']
        with connect(self.db) as db:
            db.execute('DELETE FROM transactions')

    def purchase(self, db, month, name, amount=10000, visits=2, account='demo-daily'):
        for visit in range(visits):
            db.execute('''INSERT INTO transactions(account_id,external_id,booked_on,description,
                          amount,currency,category,merchant) VALUES (?,?,?,'Fixture',?,'DKK','Fritid',?)''',
                       (account, f'{name}-{month}-{visit}', month+'-10', -amount, name))

    def history(self, db):
        return _savings_history_profile(db, 'demo', 'DKK', self.today, ['demo-daily'])

    def test_year_boundary_excludes_current_and_old_months_and_checks_recent_habits(self):
        months = ['2025-10','2025-11','2025-12'] + [f'2026-{m:02d}' for m in range(1,10)]
        with connect(self.db) as db:
            for index, month in enumerate(months):
                self.purchase(db, month, 'Stable Cafe')
                if index < 9:
                    self.purchase(db, month, 'Old Habit', 20000)
                    self.purchase(db, month, 'Reduced Habit', 100000, 4)
                else:
                    self.purchase(db, month, 'Reduced Habit', 30000)
            self.purchase(db, '2025-09', 'Outside Window', 1000000, 100)
            self.purchase(db, '2026-10', 'Current Month', 1000000, 100)
            self.purchase(db, '2026-09', 'Other Account', 1000000, 100, 'demo-bills')
            result = self.history(db)
        self.assertEqual(result['suggestion_history']['months_analyzed'], 12)
        self.assertEqual(result['suggestion_history']['period_start'], '2025-10-01')
        self.assertEqual(result['suggestion_history']['period_end'], '2026-09-30')
        self.assertEqual(result['suggestion_history']['recent_period_start'], '2026-07-01')
        self.assertEqual([s['name'] for s in result['opportunities']], ['Stable Cafe', 'Reduced Habit'])
        stable, reduced = result['opportunities']
        self.assertEqual(stable['purchases'], 24)
        self.assertEqual(stable['monthly_average'], 20000)
        self.assertEqual(stable['suggested_cut'], 20000)
        self.assertEqual(stable['suggestion_kind'], 'stop_merchant_purchases')
        self.assertEqual(stable['saving_reference'], 'history')
        self.assertFalse(stable['recent_reduced'])
        self.assertEqual(reduced['monthly_average'], 315000)
        self.assertEqual(reduced['recent_monthly_average'], 60000)
        self.assertEqual(reduced['suggested_cut'], 60000)
        self.assertEqual(reduced['yearly_effect'], 720000)
        self.assertEqual(reduced['saving_reference'], 'recent')
        self.assertTrue(reduced['recent_reduced'])

    def test_short_history_includes_zero_spend_months_and_does_not_divide_by_twelve(self):
        with connect(self.db) as db:
            self.purchase(db, '2026-07', 'Cafe', visits=3)
            self.purchase(db, '2026-09', 'Cafe', visits=3)
            result = self.history(db)
        self.assertEqual(result['suggestion_history']['months_analyzed'], 3)
        self.assertEqual(result['opportunities'][0]['monthly_average'], 20000)
        self.assertEqual(result['opportunities'][0]['average_visits_per_month'], 2)

    def test_only_current_month_has_no_completed_history(self):
        with connect(self.db) as db:
            self.purchase(db, '2026-10', 'Cafe', visits=30)
            result = self.history(db)
        self.assertEqual(result['opportunities'], [])
        self.assertEqual(result['suggestion_history']['months_analyzed'], 0)
        self.assertIsNone(result['suggestion_history']['period_start'])

    def test_once_a_month_recurring_purchases_use_full_average_not_one_purchase_reduction(self):
        with connect(self.db) as db:
            for month, amount in (('2026-06',20000),('2026-07',30000),
                                  ('2026-08',40000),('2026-09',50000)):
                self.purchase(db,month,'Monthly Shop',amount,1)
            result=self.history(db)
        item=result['opportunities'][0]
        self.assertEqual(item['monthly_average'],35000)
        self.assertEqual(item['recent_monthly_average'],40000)
        self.assertEqual(item['suggested_cut'],35000)
        self.assertEqual(item['yearly_effect'],420000)
        with connect(self.db) as db:
            db.execute("INSERT INTO merchant_preferences VALUES ('demo','monthlyshop','Monthly Shop',0)")
            self.assertEqual(self.history(db)['opportunities'],[])

    def test_changing_selected_period_does_not_change_suggestions(self):
        with connect(self.db) as db:
            self.purchase(db, '2026-07', 'Cafe', visits=3)
            self.purchase(db, '2026-09', 'Cafe', amount=20000, visits=3)
            ident=db.execute('''INSERT INTO savings_goals(source,currency,name,target_amount,deadline)
                                VALUES ('demo','DKK','House',10000000,'2027-12')''').lastrowid
            db.execute("INSERT INTO savings_goal_analysis_accounts VALUES (?,'demo-daily')", (ident,))
        results = []
        for month in (7,9):
            result = build_insights(self.db, 'demo', 'DKK', today=self.today,
                                    period_start=date(2026,month,1), period_end=date(2026,month,30))
            results.append(next(g['analysis'] for g in result['goals'] if g['id']==ident))
        self.assertEqual(results[0]['suggestions'], results[1]['suggestions'])
        self.assertEqual(results[0]['suggestion_history'], results[1]['suggestion_history'])
        self.assertNotEqual(results[0]['average_available'], results[1]['average_available'])
        self.assertEqual(len(results[0]['suggestions']),1)


if __name__ == '__main__':
    unittest.main()
