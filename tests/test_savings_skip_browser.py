"""Exercise both skip choices against the real app and an isolated database."""
import threading
import unittest
from pathlib import Path
from werkzeug.serving import make_server
import test_savings_skips as fixtures

try:
    from playwright.sync_api import sync_playwright, expect
except ImportError:
    sync_playwright = None


@unittest.skipIf(sync_playwright is None, 'Install Playwright for browser checks')
class SavingsSkipBrowserTests(unittest.TestCase):
    def test_modal_choices_cancel_persistence_undo_and_error_recovery(self):
        fixture = fixtures.SavingsSkipTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        server = make_server('127.0.0.1', 0, fixture.app, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        output = Path(__file__).resolve().parents[1] / 'test-results'
        output.mkdir(exist_ok=True)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                page = browser.new_page(viewport={'width':1440,'height':1000})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(f'http://127.0.0.1:{server.server_port}')
                page.locator('.nav-item[data-view="savings"]').click()
                first = page.locator('[data-skip-suggestion]').first
                expect(first).to_have_text('Spring over')
                original = fixture.analysis()['potential_extra_saving']
                first.click()
                dialog = page.locator('#savingsSkipDialog')
                expect(dialog).to_be_visible()
                expect(dialog.locator('[value="month"]')).to_be_focused()
                expect(dialog.locator('#savingsSkipText')).to_contain_text('Cafe 00')
                self.assertEqual(len(fixture.analysis()['suggestions']), 12)
                dialog.screenshot(path=str(output/'savings-skip-options-desktop.png'))
                page.keyboard.press('Escape')
                expect(dialog).not_to_be_visible()
                expect(first).to_be_focused()
                self.assertEqual(len(fixture.analysis()['suggestions']), 12)
                first.click()
                dialog.locator('[value="month"]').click()
                expect(dialog).not_to_be_visible()
                expect(first).to_have_attribute('data-merchant', 'Cafe 01')
                self.assertEqual(fixture.analysis()['potential_extra_saving'], original-48000)
                monthly = page.locator('.goal-skipped').filter(has=page.locator('[data-duration="month"]'))
                monthly.locator('summary').click()
                monthly.get_by_role('button', name='Tag Cafe 00 med igen').click()
                expect(first).to_have_attribute('data-merchant', 'Cafe 00')
                self.assertEqual(fixture.analysis()['potential_extra_saving'], original)
                first.click()
                dialog.locator('[value="permanent"]').click()
                expect(dialog).not_to_be_visible()
                expect(first).to_have_attribute('data-merchant', 'Cafe 01')
                page.reload()
                page.locator('.nav-item[data-view="savings"]').click()
                expect(page.locator('.goal-skipped summary')).to_have_text('Sprunget over permanent (1)')
                self.assertEqual(fixture.analysis()['permanently_skipped_suggestions'], [{'name':'Cafe 00'}])
                # The dialog fits on a narrow phone and a failed save stays actionable.
                page.set_viewport_size({'width':320,'height':800})
                first.click()
                expect(dialog).to_be_visible()
                self.assertTrue(dialog.evaluate('el=>el.scrollWidth<=el.clientWidth'))
                bounds = dialog.bounding_box()
                self.assertGreaterEqual(bounds['x'], 0)
                self.assertLessEqual(bounds['x']+bounds['width'], 320)
                dialog.screenshot(path=str(output/'savings-skip-options-mobile.png'))
                endpoint = '**/api/savings-goals/*/suggestion-skips?*'
                page.route(endpoint, lambda route: route.fulfill(status=503, json={'error':'Kunne ikke gemme lige nu'}))
                dialog.locator('[value="permanent"]').click()
                expect(dialog.locator('#savingsSkipError')).to_have_text('Kunne ikke gemme lige nu')
                expect(dialog.locator('[value="month"]')).to_be_enabled()
                expect(dialog.locator('[value="permanent"]')).to_be_enabled()
                self.assertEqual(len(fixture.analysis()['suggestions']), 11)
                page.unroute(endpoint)
                dialog.get_by_role('button', name='Annuller', exact=True).click()
                permanent = page.locator('.goal-skipped')
                permanent.locator('summary').click()
                permanent.get_by_role('button', name='Tag Cafe 00 med igen').click()
                expect(first).to_have_attribute('data-merchant', 'Cafe 00')
                self.assertEqual(fixture.analysis()['permanently_skipped_suggestions'], [])
                self.assertEqual(fixture.analysis()['potential_extra_saving'], original)
                self.assertFalse(errors, errors)
            finally:
                browser.close()
