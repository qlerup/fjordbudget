"""Exercise the shared browser monitor with real dialog/fetch behavior."""
import unittest
from pathlib import Path
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


def static_directory():
    root = Path(__file__).resolve().parents[1]
    return next(path for path in (root/'static', root/'app/static', root/'app/public')
                if (path/'hub-session.js').exists())


@unittest.skipIf(sync_playwright is None, "Install Playwright to run browser regression tests")
class HubSessionBrowserTests(unittest.TestCase):
    def test_revocation_displays_once_then_returns_to_login_and_outage_does_not_log_out(self):
        root=static_directory()
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch()
            try:
                for width in (390,1440):
                    context=browser.new_context(viewport={'width':width,'height':900})
                    page=context.new_page()
                    status={'revoked':False,'outage':False}
                    def route(request):
                        path=request.request.url.split('test')[-1].split('?')[0]
                        if path.endswith('.js'): request.fulfill(path=str(root/'hub-session.js'),content_type='text/javascript; charset=utf-8')
                        elif path.endswith('.css'): request.fulfill(path=str(root/'hub-session.css'),content_type='text/css; charset=utf-8')
                        elif path.startswith('/api/'):
                            code=503 if status['outage'] else 401 if status['revoked'] else 200
                            request.fulfill(status=code,json={'authenticated':code==200, 'error_code':'access_revoked' if code==401 else 'hub_unavailable' if code==503 else None})
                        else: request.fulfill(content_type='text/html; charset=utf-8',body='<html><head><meta charset="utf-8"><link rel="stylesheet" href="/static/hub-session.css"><script defer src="/static/hub-session.js"></script></head><body><main>Private content</main></body></html>')
                    page.route('**/*',route)
                    page.goto('https://session.test/',wait_until='networkidle')
                    other=page.context.new_page()
                    other.route('**/*',route)
                    other.goto('https://session.test/',wait_until='networkidle')
                    status['outage']=True
                    page.evaluate("fetch('/api/auth/access')")
                    self.assertEqual(page.locator('.hub-access-removed').count(),0)
                    status.update(outage=False,revoked=True)
                    page.evaluate("fetch('/api/private'); fetch('/api/auth/access')")
                    popup=page.locator('.hub-access-removed')
                    popup.wait_for()
                    self.assertEqual(popup.count(),1)
                    self.assertIn('Din adgang er blevet fjernet.',popup.inner_text())
                    box=popup.bounding_box()
                    self.assertGreaterEqual(box['x'],0)
                    self.assertLessEqual(box['x']+box['width'],width)
                    # Revocation propagates to a second open tab even if its cookie was cleared.
                    other.locator('.hub-access-removed').wait_for()
                    popup.get_by_role('button',name='Gå til login').click()
                    page.wait_for_url('**/login')
                    other.wait_for_url('**/login')  # No click: the automatic logout timer redirects.
                    # The server can keep returning revoked after logout. No new dialog or loop.
                    for tab in (page, other):
                        tab.reload(wait_until='networkidle')
                        tab.evaluate("fetch('/api/auth/access')")
                        self.assertEqual(tab.locator('.hub-access-removed').count(), 0)
                    # A stale redirect query must be consumed, including when already notified.
                    page.goto('https://session.test/login?access_removed=1&next=library', wait_until='networkidle')
                    self.assertNotIn('access_removed', page.url)
                    self.assertIn('next=library', page.url)
                    self.assertEqual(page.locator('.hub-access-removed').count(), 0)
                    # A new login re-arms the monitor; Escape also completes logout.
                    status['revoked'] = False
                    page.goto('https://session.test/', wait_until='networkidle')
                    status['revoked'] = True
                    page.evaluate("fetch('/api/private')")
                    page.locator('.hub-access-removed').wait_for()
                    page.keyboard.press('Escape')
                    page.wait_for_url('**/login')
                    page.reload(wait_until='networkidle')
                    self.assertEqual(page.locator('.hub-access-removed').count(), 0)
                    other.close()
                    page.close()
                    context.close()
            finally: browser.close()

    def test_same_page_login_and_header_revocation(self):
        root = static_directory()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page()
                revoked = [False]
                def route(request):
                    if request.request.url.endswith('.js'):
                        request.fulfill(path=str(root/'hub-session.js'), content_type='text/javascript')
                    elif '/api/' in request.request.url:
                        request.fulfill(status=401 if revoked[0] else 200,
                                        headers={'X-FjordHub-Access': 'revoked'} if revoked[0] else {},
                                        json={'authenticated': not revoked[0]})
                    else:
                        request.fulfill(content_type='text/html', body='<script defer src="/hub-session.js" data-login-url="/"></script>')
                page.route('**/*', route)
                page.goto('https://session.test/', wait_until='networkidle')
                revoked[0] = True
                page.evaluate("fetch('/api/private')")
                page.locator('.hub-access-removed button').click()
                page.wait_for_load_state('networkidle')
                page.reload(wait_until='networkidle')
                self.assertEqual(page.locator('.hub-access-removed').count(), 0)
            finally:
                browser.close()
