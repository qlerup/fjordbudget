import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from insights import _goal_analysis


class SavingsGoalImpactTests(unittest.TestCase):
    def analysis(self, available=10000, cut=3000, **changes):
        goal={'target_amount':100000,'saved_amount':40000,'deadline':'2027-02',**changes}
        profile={'average_available':available,'average_income':20000,'average_expenses':20000-available,
                 'months_analyzed':12,'opportunities':[{'suggested_cut':cut}]}
        return _goal_analysis(goal,profile,date(2026,10,22))

    def test_individual_saving_contributes_toward_remaining_goal_and_moves_date(self):
        result=self.analysis()
        impact=result['suggestions'][0]['goal_impact']
        self.assertEqual(result['months_remaining'],5)
        self.assertEqual(impact['extra_by_deadline'],15000)
        self.assertEqual(impact['remaining_percent'],25)
        self.assertEqual(impact['months_sooner'],1)
        self.assertEqual(impact['projected_deadline'],'2027-02')
        self.assertEqual(self.analysis(target_amount=160000)['suggestions'][0]['goal_impact']['remaining_percent'],12.5)

    def test_deficit_must_be_covered_before_savings_advance_the_goal(self):
        result=self.analysis(available=-5000)
        impact=result['suggestions'][0]['goal_impact']
        self.assertEqual(impact['extra_by_deadline'],0)
        self.assertIsNone(impact['projected_deadline'])
        self.assertIsNone(result['projected_with_changes'])
        self.assertEqual(result['max_with_suggestions'],40000)
        partial=self.analysis(available=-1000)['suggestions'][0]['goal_impact']
        self.assertEqual(partial['extra_by_deadline'],10000)

    def test_zero_disposable_income_does_not_invent_a_previous_goal_date(self):
        impact=self.analysis(available=0)['suggestions'][0]['goal_impact']
        self.assertEqual(impact['extra_by_deadline'],15000)
        self.assertEqual(impact['months_sooner'],0)
        self.assertIsNotNone(impact['projected_deadline'])

    def test_progress_cannot_exceed_the_remaining_goal(self):
        impact=self.analysis(cut=1000000)['suggestions'][0]['goal_impact']
        self.assertEqual(impact['extra_by_deadline'],60000)
        self.assertEqual(impact['remaining_percent'],100)
        self.assertEqual(impact['projected_deadline'],'2026-10')

    def test_no_future_progress_claim_for_complete_or_expired_goals(self):
        for changes in ({'saved_amount':100000},{'deadline':'2026-09'}):
            self.assertIsNone(self.analysis(**changes)['suggestions'][0]['goal_impact'])
