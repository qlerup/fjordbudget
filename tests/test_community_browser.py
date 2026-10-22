"""Real app -> encrypted HTTP -> standalone server -> admin -> origin roundtrip."""
import threading
import unittest
from pathlib import Path
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright, expect
import test_community as fixtures
from community import CommunitySync


class CommunityBrowserTests(unittest.TestCase):
    def test_opt_in_review_name_roundtrip_and_mobile(self):
        fixture=fixtures.CommunityTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        remote=make_server('127.0.0.1',0,fixture.server,threaded=True)
        remote_url=f'http://localhost:{remote.server_port}'
        fixture.server.config['SERVER_URL']=remote_url
        threading.Thread(target=remote.serve_forever,daemon=True).start()
        self.addCleanup(remote.shutdown)
        app,sync=fixture.participant()
        sync.url=remote_url
        sync._call=CommunitySync._call.__get__(sync)
        transaction=fixture.seed(app)
        local=make_server('127.0.0.1',0,app,threaded=True)
        threading.Thread(target=local.serve_forever,daemon=True).start()
        self.addCleanup(local.shutdown)
        output=Path(__file__).resolve().parents[1]/'test-results'
        output.mkdir(exist_ok=True)
        with sync_playwright() as pw:
            browser=pw.chromium.launch()
            try:
                context=browser.new_context(viewport={'width':1440,'height':1050})
                page=context.new_page()
                errors=[]
                page.on('pageerror',lambda error: errors.append(str(error)))
                page.goto(f'http://localhost:{local.server_port}')
                page.get_by_role('button',name='Indstillinger',exact=True).click()
                dialog=page.locator('#communityDialog')
                expect(dialog).to_be_visible()
                expect(page.locator('#communityEnabled')).not_to_be_checked()
                page.locator('.community-preview summary').click()
                expect(page.locator('#communityPreview')).to_contain_text(fixtures.NORMAL)
                expect(page.locator('#communityPreview')).to_contain_text('Kebab alias')
                dialog.screenshot(path=str(output/'community-consent-desktop.png'))
                page.locator('#communityEnabled').check()
                page.locator('#communitySave').click()
                expect(page.locator('#communityState')).to_contain_text('Deling er slået til')
                self.assertTrue(sync.sync_once())
                admin=context.new_page()
                admin.on('pageerror',lambda error: errors.append(str(error)))
                admin.on('console',lambda message:errors.append(message.text) if message.type=='error' else None)
                admin.goto(remote_url)
                admin.locator('#password').fill(fixtures.PASSWORD)
                with admin.expect_response(lambda response:response.request.method=='POST' and '/login' in response.url) as login_result:
                    admin.get_by_role('button',name='Log ind',exact=True).click()
                login_response=login_result.value
                self.assertLess(login_response.status,400,(login_response.url,login_response.request.headers.get('origin'),fixture.server.config['SERVER_URL']))
                admin.wait_for_load_state('networkidle')
                expect(admin.get_by_role('heading',name='Fælles forhandlere.')).to_be_visible()
                expect(admin.locator('.entry')).to_contain_text('Kebab alias')
                admin.screenshot(path=str(output/'community-admin-desktop.png'))
                admin.get_by_role('button',name='Godkend / merge',exact=True).click()
                self.assertEqual(errors,[])
                expect(admin.locator('#reviewDialog')).to_be_visible()
                admin.locator('#merchant').fill('Stjerne Kebab & Grill')
                admin.locator('#reviewDialog').screenshot(path=str(output/'community-merge-name.png'))
                admin.get_by_role('button',name='Godkend og synkroniser navn').click()
                expect(admin.locator('#reviewDialog')).not_to_be_visible()
                self.assertTrue(sync.sync_once())
                self.assertEqual(fixture.row(app,transaction)['merchant'],'Stjerne Kebab & Grill')
                page.evaluate('loadCommunity()')
                expect(page.locator('#communityPreview')).to_contain_text('Stjerne Kebab & Grill')
                admin.get_by_role('link',name='Godkendt / merget').click()
                expect(admin.locator('.entry')).to_contain_text('Stjerne Kebab & Grill')
                for target in (page,admin):
                    target.set_viewport_size({'width':320,'height':850})
                    self.assertTrue(target.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                admin.screenshot(path=str(output/'community-admin-mobile.png'))
                dialog.evaluate('element=>element.scrollTop=0')
                dialog.screenshot(path=str(output/'community-consent-mobile.png'))
                # Opting out remains a local action and removes shared access.
                page.locator('#communityEnabled').uncheck()
                page.locator('#communitySave').click()
                expect(page.locator('#communityState')).to_contain_text('Deling er slået fra')
                self.assertTrue(sync.sync_once())
                self.assertFalse(sync.status()['enabled'])
                self.assertEqual(sync.status()['cached'],0)
                dialog.get_by_role('button',name='Luk',exact=True).click()
                page.get_by_role('button',name='Indstillinger',exact=True).click()
                expect(dialog).to_be_visible()
                self.assertFalse(errors,errors)
            finally:
                browser.close()
