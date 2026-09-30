import logging
import threading
import unittest

from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server
import test_hub_auth


class HubLoginBrowserTest(unittest.TestCase):
    def test_password_form_logout_and_mobile(self):
        logging.getLogger('werkzeug').setLevel(logging.ERROR)
        fixture=test_hub_auth.HubAuthTests();fixture.setUp()
        server=make_server('127.0.0.1',0,fixture.app,threaded=True)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch()
                page=browser.new_page(viewport={'width':1280,'height':900})
                page.goto(f'http://localhost:{server.server_port}/login')
                page.locator('#username').fill('owner');page.locator('#password').fill('correct')
                page.locator('button[type=submit]').click()
                expect(page.locator('#appContent')).to_be_visible()
                page.locator('#logoutButton').click();page.wait_for_url('**/login')
                self.assertEqual(page.request.get(f'http://localhost:{server.server_port}/api/dashboard').status,401)
                page.set_viewport_size({'width':390,'height':844})
                self.assertFalse(page.evaluate('document.documentElement.scrollWidth>innerWidth'))
                browser.close()
        finally:
            server.shutdown();thread.join();fixture.doCleanups()
