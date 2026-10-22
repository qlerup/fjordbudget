import threading
import unittest
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
from werkzeug.serving import make_server
import test_local_auth as fixtures


class LocalAuthBrowserTests(unittest.TestCase):
    def test_first_setup_login_logout_and_mobile(self):
        fixture=fixtures.LocalAuthTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        server=make_server('127.0.0.1',0,fixture.app,threaded=True)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.shutdown)
        output=Path(__file__).resolve().parents[1]/'test-results';output.mkdir(exist_ok=True)
        with sync_playwright() as pw:
            browser=pw.chromium.launch()
            try:
                page=browser.new_page(viewport={'width':1440,'height':1000})
                errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(f'http://localhost:{server.server_port}')
                page.wait_for_url('**/setup')
                expect(page.get_by_role('heading',name='Første opsætning')).to_be_visible()
                self.assertEqual(page.locator('input[name="token"]').count(),0)
                page.screenshot(path=str(output/'tommy-first-setup-desktop.png'))
                page.locator('#username').fill('tommy')
                page.locator('#password').fill(fixture.password)
                page.locator('#password2').fill('Not-the-same-password')
                page.get_by_role('button',name='Opret konto',exact=True).click()
                expect(page.locator('[role="alert"]')).to_have_text('Adgangskoderne er ikke ens.')
                page.locator('#password').fill(fixture.password)
                page.locator('#password2').fill(fixture.password)
                page.get_by_role('button',name='Opret konto',exact=True).click()
                page.wait_for_url('**/login?created=1')
                expect(page.locator('[role="status"]')).to_contain_text('Din konto er oprettet')
                page.set_viewport_size({'width':320,'height':850})
                self.assertFalse(page.evaluate('document.documentElement.scrollWidth>innerWidth'))
                page.screenshot(path=str(output/'tommy-login-mobile.png'))
                page.locator('#username').fill('tommy');page.locator('#password').fill(fixture.password)
                page.get_by_role('button',name='Log ind',exact=True).click()
                expect(page.locator('#appContent')).to_be_visible()
                page.set_viewport_size({'width':1440,'height':1000})
                expect(page.locator('.workspace')).to_contain_text('Tommy Budget')
                page.locator('#logoutButton').click();page.wait_for_url('**/login')
                self.assertEqual(page.request.get(f'http://localhost:{server.server_port}/api/dashboard').status,401)
                page.goto(f'http://localhost:{server.server_port}/setup')
                page.wait_for_url('**/login')
                self.assertFalse(errors,errors)
            finally:
                browser.close()
