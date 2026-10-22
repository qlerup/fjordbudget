import re
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app
from db import connect, initialize
from insights import build_insights


class SavingsSkipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = create_app({'TESTING': True, 'DATA_DIR': self.temp.name})
        self.client = self.app.test_client()
        self.headers = {'X-CSRF-Token': re.search(
            r'name="csrf-token" content="([^"]+)"', self.client.get('/').text)[1]}
        self.db = self.app.extensions['db_path']
        self.today = date.today()
        self.month = self.today.strftime('%Y-%m')
        self.end = self.today.replace(day=1) - timedelta(days=1)
        self.start = self.end.replace(day=1)
        self.scope = f'source=demo&from={self.start}&to={self.end}'
        with connect(self.db) as db:
            db.execute('DELETE FROM transactions')
            for index in range(12):
                for visit in range(2):
                    db.execute('''INSERT INTO transactions(account_id,external_id,booked_on,
                                  description,amount,currency,category,merchant)
                                  VALUES ('demo-daily',?,?,?,?,'DKK','Fritid',?)''',
                               (f'skip-{index}-{visit}', self.start.isoformat(), 'Fixture',
                                -(24000 - index * 1000), f'Cafe {index:02d}'))
            db.execute('''INSERT INTO transactions(account_id,external_id,booked_on,
                          description,amount,currency,category)
                          VALUES ('demo-daily','income',?,'Løn',1000000,'DKK','Indkomst')''',
                       (self.start.isoformat(),))
        self.goal = self.create_goal()

    def create_goal(self):
        response = self.client.post('/api/savings-goals?source=demo', headers=self.headers,
                                    json={'name': 'Huskøb', 'target_amount': '1000',
                                          'deadline': f'{self.today.year + 1}-12',
                                          'analysis_account_ids': ['demo-daily']})
        self.assertEqual(response.status_code, 200)
        return response.json['id']

    def analysis(self, goal=None):
        result = self.client.get('/api/savings-goals?' + self.scope)
        self.assertEqual(result.status_code, 200)
        return next(g['analysis'] for g in result.json['items'] if g['id'] == (goal or self.goal))

    def skip(self, name, method='put', **kwargs):
        return getattr(self.client, method)(
            f'/api/savings-goals/{self.goal}/suggestion-skips?source=demo',
            headers=self.headers, json={'merchant': name, 'month': self.month, **kwargs})

    def test_persists_replaces_and_updates_all_savings_calculations(self):
        before = self.analysis()
        self.assertEqual(len(before['suggestions']), 12)
        self.assertEqual([s['name'] for s in before['suggestions'][:3]], ['Cafe 00', 'Cafe 01', 'Cafe 02'])
        self.assertEqual(self.skip('Cafe 00').status_code, 200)
        after = self.analysis()
        self.assertEqual(len(after['suggestions']), 11)
        self.assertEqual([s['name'] for s in after['suggestions'][:3]], ['Cafe 01', 'Cafe 02', 'Cafe 03'])
        self.assertEqual(after['potential_extra_saving'], before['potential_extra_saving'] - 48000)
        self.assertEqual(after['max_with_suggestions'],
                         after['max_by_deadline'] + after['potential_extra_saving'] * after['months_remaining'])
        self.assertEqual(after['average_available'], before['average_available'])
        initialize(self.db)  # Reopening/upgrading the database retains the decision.
        self.assertEqual(self.analysis()['skipped_suggestions'], [{'name': 'Cafe 00'}])
        dashboard = self.client.get('/api/dashboard?source=demo').json
        self.assertNotIn('Cafe 00', [s['name'] for s in dashboard['featured_goal']['analysis']['suggestions']])

    def test_backfills_beyond_the_old_top_ten_and_does_not_invent_suggestions(self):
        before = self.analysis()
        target = (before['average_available'] + before['potential_extra_saving']) * before['months_remaining']
        with connect(self.db) as db:
            db.execute('UPDATE savings_goals SET target_amount=? WHERE id=?', (target, self.goal))
        before = self.analysis()
        self.assertEqual(before['status'], 'possible_with_changes')
        for index in range(12):
            self.assertEqual(self.skip(f'Cafe {index:02d}').status_code, 200)
            if index == 9:
                self.assertEqual([s['name'] for s in self.analysis()['suggestions']], ['Cafe 10', 'Cafe 11'])
        after = self.analysis()
        self.assertEqual(after['suggestions'], [])
        self.assertEqual(after['potential_extra_saving'], 0)
        self.assertEqual(after['max_with_suggestions'], after['max_by_deadline'])
        self.assertEqual(after['projected_with_changes'], after['projected_deadline'])
        self.assertGreater(after['projected_with_changes'], before['projected_with_changes'])
        self.assertEqual(after['status'], 'not_realistic')

    def test_scoped_to_goal_currency_and_source_and_expires_next_month(self):
        other = self.create_goal()
        self.skip('Cafe 00')
        self.assertEqual(self.analysis(other)['suggestions'][0]['name'], 'Cafe 00')
        for scope in ('source=live', 'source=demo&currency=EUR'):
            response = self.client.put(f'/api/savings-goals/{self.goal}/suggestion-skips?{scope}',
                                       headers=self.headers,
                                       json={'merchant': 'Cafe 01', 'month': self.month})
            self.assertEqual(response.status_code, 404)
        next_month = (self.today.replace(day=28) + timedelta(days=4)).replace(day=1)
        with connect(self.db) as db:
            for visit in range(2):
                db.execute('''INSERT INTO transactions(account_id,external_id,booked_on,
                              description,amount,currency,category,merchant)
                              VALUES ('demo-daily',?,?,'Cafe',-24000,'DKK','Fritid','Cafe 00')''',
                           (f'new-month-{visit}', self.today.replace(day=1).isoformat()))
        result = build_insights(self.db, 'demo', 'DKK', today=next_month,
                                period_start=self.start, period_end=self.end)
        analysis = next(g['analysis'] for g in result['goals'] if g['id'] == self.goal)
        self.assertEqual(analysis['skipped_suggestions'], [])
        self.assertEqual(analysis['suggestions'][0]['name'], 'Cafe 00')

    def test_idempotent_normalized_skip_and_undo(self):
        self.skip('Cafe 00')
        self.skip('CAFE-00')
        self.assertEqual(len(self.analysis()['skipped_suggestions']), 1)
        self.assertNotIn('Cafe 00', [s['name'] for s in self.analysis()['suggestions']])
        self.assertEqual(self.skip('cafe 00', method='delete').status_code, 200)
        self.assertEqual(self.analysis()['suggestions'][0]['name'], 'Cafe 00')
        self.assertEqual(self.skip('cafe 00', method='delete').status_code, 200)
        self.skip('Cafe 00')
        self.client.delete(f'/api/savings-goals/{self.goal}?source=demo', headers=self.headers)
        with connect(self.db) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM savings_suggestion_skips').fetchone()[0], 0)

    def test_csrf_validation_and_stale_month(self):
        url = f'/api/savings-goals/{self.goal}/suggestion-skips?source=demo'
        self.assertEqual(self.client.put(url, json={'merchant': 'Cafe 00', 'month': self.month}).status_code, 403)
        for name in ('', '   ', '---', 'bad\nname', 'x' * 201, None, []):
            self.assertEqual(self.skip(name).status_code, 400)
        self.assertEqual(self.client.put(url, headers=self.headers, json=[]).status_code, 400)
        self.assertEqual(self.skip('Cafe 00', month='2000-01').status_code, 409)
        self.assertEqual(self.analysis()['skipped_suggestions'], [])


if __name__ == '__main__':
    unittest.main()
