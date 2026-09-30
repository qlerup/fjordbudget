"""Browser regressions against a temporary database; pip install playwright."""
import re
import logging
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server


class UITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.getLogger('werkzeug').setLevel(logging.ERROR)
        cls.temp=tempfile.TemporaryDirectory()
        cls.app=create_app({'DATA_DIR':cls.temp.name,'TESTING':True})
        cls.server=make_server('127.0.0.1',0,cls.app,threaded=True)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()
        cls.url=f'http://localhost:{cls.server.server_port}'
        cls.pw=sync_playwright().start()
        cls.browser=cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.pw.stop();cls.server.shutdown();cls.thread.join();cls.temp.cleanup()

    def setUp(self):
        self.context=self.browser.new_context(viewport={'width':1440,'height':1050})
        self.page=self.context.new_page()
        self.errors=[]
        self.page.on('pageerror',lambda error:self.errors.append(str(error)))
        self.page.goto(self.url)
        expect(self.page.locator('#appContent')).to_be_visible()

    def tearDown(self):
        self.assertEqual(self.errors,[])
        self.context.close()

    def test_account_setup_after_bank_sync_and_no_repeat_after_save(self):
        data=self.app.test_client().get('/api/dashboard?source=demo').json
        data['accounts'][0]['custom_name']='Already named'
        def dashboard(route):
            route.fulfill(json=data)
        def save(route):
            names=route.request.post_data_json['names']
            for account in data['accounts']:
                if account['id'] in names:
                    account['custom_name']=names[account['id']]
                    account['name']=names[account['id']]
            route.fulfill(json={'ok':True})
        self.page.route('**/api/dashboard?*',dashboard)
        self.page.route('**/api/accounts/setup',save)
        calls=[]
        def sync(route):
            calls.append(route.request.method)
            route.fulfill(json={'running':False,'message':'Fetched','error':False})
        self.page.route('**/api/sync',sync)
        self.page.set_viewport_size({'width':390,'height':844})
        self.page.goto(self.url+'/?bank_result=connected')
        expect(self.page.locator('#accountSetupDialog')).to_be_visible()
        self.assertIn('POST',calls)
        expect(self.page.locator('#accountSetupFields input')).to_have_count(2)
        expect(self.page.locator('#accountSetupFields')).to_contain_text('9036')
        self.assertEqual(self.page.locator('#accountSetupDialog').evaluate('(el)=>el.scrollWidth<=el.clientWidth'),True)
        self.page.locator('#setupAccount0').fill('Bills')
        self.page.locator('#setupAccount1').fill('Savings')
        self.page.locator('#saveAccountSetup').click()
        expect(self.page.locator('#accountSetupDialog')).not_to_be_visible()
        self.page.reload()
        expect(self.page.locator('#appContent')).to_be_visible()
        expect(self.page.locator('#accountSetupDialog')).not_to_be_visible()

    def test_category_create_budget_delete_and_mobile(self):
        self.page.set_viewport_size({'width':390,'height':844})
        self.page.locator('[data-view="categories"]').click()
        self.page.locator('#newCategoryName').fill('Holiday <test>')
        self.page.get_by_role('button',name='Tilføj kategori',exact=True).click()
        expect(self.page.locator('#categoryList')).to_contain_text('Holiday <test>')
        self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'),390)
        self.page.locator('[data-view="budget"]').first.click()
        self.page.locator('.full-budget .edit-budget').click()
        expect(self.page.get_by_label('Budget for Holiday <test>')).to_be_visible()
        self.page.keyboard.press('Escape')
        self.page.locator('[data-view="categories"]').click()
        self.page.get_by_role('button',name='Slet Holiday <test>',exact=True).click()
        self.page.get_by_role('button',name='Flyt og slet',exact=True).click()
        expect(self.page.locator('#categoryDeleteDialog')).not_to_be_visible()
        expect(self.page.locator('#categoryList')).not_to_contain_text('Holiday <test>')
        self.page.reload()
        self.page.locator('[data-view="categories"]').click()
        expect(self.page.locator('#categoryList')).not_to_contain_text('Holiday <test>')

    def test_api_handles_html_gateway_error(self):
        self.page.route('**/api/bank/connect', lambda route: route.fulfill(
            status=502, content_type='text/html', body='<!DOCTYPE html><h1>Bad gateway</h1>'))
        message=self.page.evaluate("""async () => {
            try { await api('/api/bank/connect', {method:'POST',body:'{}'}); }
            catch (error) { return error.message; }
        }""")
        self.assertIn('HTTP 502',message)
        self.assertNotIn('Unexpected token',message)

    def test_custom_account_name_survives_reload_and_updates_filter(self):
        self.page.locator('[data-rename="demo-daily"]').click()
        self.page.locator('#accountNameInput').fill('Food <test>')
        self.page.locator('#saveAccountName').click()
        expect(self.page.locator('#accountNameDialog')).not_to_be_visible()
        self.page.reload()
        expect(self.page.locator('[data-account="demo-daily"]')).to_contain_text('Food <test>')
        expect(self.page.locator('#accountFilter option[value="demo-daily"]')).to_have_text('Food <test>')
        self.page.locator('[data-account="demo-daily"]').click()
        expect(self.page.locator('#accountFilter')).to_have_value('demo-daily')
        self.page.locator('[data-view="accounts"]').click()
        self.page.locator('[data-rename="demo-daily"]').click()
        self.page.locator('#accountNameInput').fill('')
        self.page.locator('#saveAccountName').click()
        expect(self.page.locator('#accountNameDialog')).not_to_be_visible()

    def test_account_navigation_search_and_category_edit(self):
        self.page.locator('[data-account="demo-daily"]').click()
        expect(self.page.locator('#accountFilter')).to_have_value('demo-daily')
        self.page.locator('#search').fill('Netto')
        expect(self.page.locator('#transactionRows')).to_contain_text('Netto')
        expect(self.page.locator('#transactionRows')).not_to_contain_text('Spotify')
        category=self.page.locator('#transactionRows .category-select').first
        category.select_option('Fritid')
        expect(self.page.locator('#toast')).to_contain_text('Huskes fremover.')
        self.page.reload()
        self.page.locator('#search').fill('Netto')
        expect(self.page.locator('#transactionRows .category-select').first).to_have_value('Fritid')

    def test_budget_saved_after_reload_and_live_data_empty(self):
        self.page.locator('.edit-budget').first.click()
        self.page.get_by_label('Budget for Mad & indkøb').fill('4567.89')
        self.page.get_by_role('button',name='Gem budget',exact=True).click()
        expect(self.page.locator('#budgetDialog')).not_to_be_visible()
        self.page.reload()
        self.page.locator('.edit-budget').first.click()
        expect(self.page.get_by_label('Budget for Mad & indkøb')).to_have_value('4567.89')
        self.page.get_by_role('button',name='Annuller',exact=True).click()
        self.page.locator('[data-source="live"]').click()
        expect(self.page.locator('#accounts')).to_contain_text('Din første konto starter her')
        expect(self.page.locator('#transactionRows tr')).to_have_count(0)
        self.page.locator('[data-connect]').first.click()
        expect(self.page.locator('#bankSetup')).to_be_visible()
        expect(self.page.locator('.permission-box')).to_contain_text('kan ikke betale')

    def test_mobile_layout_dialog_and_navigation(self):
        self.page.set_viewport_size({'width':390,'height':844})
        expect(self.page.locator('#appContent')).to_be_visible()
        self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'),390)
        self.page.locator('.nav-item[data-view="budget"]').click()
        expect(self.page.locator('.full-budget')).to_be_visible()
        self.page.locator('.full-budget .edit-budget').click()
        expect(self.page.get_by_role('button',name='Gem budget',exact=True)).to_be_visible()
        self.assertLessEqual(self.page.locator('#budgetDialog').bounding_box()['width'],390)
        self.page.keyboard.press('Escape')
        self.page.locator('.nav-item[data-view="transactions"]').click()
        expect(self.page.locator('.transactions-panel')).to_be_visible()
        self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'),390)
        amount=self.page.locator('td.amount-cell').first.bounding_box()
        self.assertLessEqual(amount['x']+amount['width'],390)

    def test_credentials_form_errors_save_and_mobile_layout(self):
        self.page.locator('[data-connect]').first.click()
        self.page.locator('#bankAppId').fill('invalid')
        self.page.locator('#bankPrivateKey').fill('invalid private key')
        self.page.locator('#saveBankCredentials').click()
        expect(self.page.locator('#bankError')).to_contain_text('UUID')
        expect(self.page.locator('#bankPrivateKey')).to_have_value('invalid private key')
        self.page.set_viewport_size({'width':390,'height':844})
        self.assertFalse(self.page.locator('#bankDialog').evaluate('(el)=>el.scrollWidth>el.clientWidth'))
        self.page.route('**/api/bank/credentials', lambda route: route.fulfill(
            content_type='application/json', body='{"ok":true,"provider_configured":true}'))
        self.page.route('**/api/banks', lambda route: route.fulfill(
            content_type='application/json', body='{"banks":[{"name":"Test Bank"}]}'))
        self.page.locator('#saveBankCredentials').click()
        expect(self.page.locator('#bankPicker')).to_be_visible()
        expect(self.page.locator('#bankPrivateKey')).to_have_value('')
        expect(self.page.locator('#bankSelect')).to_have_value('Test Bank')
        self.page.locator('#editBankCredentials').click()
        expect(self.page.locator('#bankSetup')).to_be_visible()
        self.page.locator('#bankPrivateKey').fill('temporary secret')
        self.page.keyboard.press('Escape')
        expect(self.page.locator('#bankPrivateKey')).to_have_value('')

    def test_custom_bank_search_keyboard_and_kreditbanken_available(self):
        self.page.evaluate('config.provider_configured=true')
        self.page.route('**/api/banks',lambda route:route.fulfill(content_type='application/json',
            body='{"banks":[{"name":"Nordea"},{"name":"Kreditbanken"}]}'))
        self.page.locator('[data-connect]').first.click()
        self.page.locator('#bankDropdownButton').click()
        self.page.locator('#bankSearch').fill('kredit')
        expect(self.page.locator('#bankOptions').get_by_role('option')).to_have_count(1)
        self.page.locator('#bankSearch').press('ArrowDown')
        self.page.locator('#bankSearch').press('Enter')
        expect(self.page.locator('#bankSelectedName')).to_have_text('Kreditbanken')
        expect(self.page.locator('#bankSelect')).to_have_value('Kreditbanken')
        expect(self.page.locator('#bankAvailability')).not_to_be_visible()
        expect(self.page.locator('#bankDropdownPanel')).not_to_be_visible()
        self.page.locator('#bankDropdownButton').click()
        self.page.locator('#bankSearch').press('Escape')
        expect(self.page.locator('#bankDialog')).to_be_visible()
        expect(self.page.locator('#bankDropdownPanel')).not_to_be_visible()

    def test_unavailable_kreditbanken_and_empty_search_on_mobile(self):
        self.page.evaluate('config.provider_configured=true')
        self.page.route('**/api/banks',lambda route:route.fulfill(content_type='application/json',
            body='{"banks":[{"name":"Nordea"}]}'))
        self.page.locator('[data-connect]').first.click()
        self.page.set_viewport_size({'width':390,'height':844})
        self.page.locator('#bankDropdownButton').click()
        self.page.locator('#bankSearch').fill('kredit')
        expect(self.page.locator('#bankOptions').get_by_role('option')).to_have_attribute('aria-disabled','true')
        self.page.locator('#bankSearch').press('ArrowDown')
        self.page.locator('#bankSearch').press('Enter')
        expect(self.page.locator('#bankSelect')).to_have_value('Nordea')
        expect(self.page.locator('#bankAvailability')).to_be_visible()
        self.assertFalse(self.page.locator('#bankDialog').evaluate('(el)=>el.scrollWidth>el.clientWidth'))
        self.page.locator('#bankSearch').fill('no such bank')
        expect(self.page.locator('#bankSearchEmpty')).to_be_visible()
        self.page.locator('#bankSearch').fill('nord')
        self.page.locator('#bankOptions').get_by_role('option').click()
        expect(self.page.locator('#bankDropdownPanel')).not_to_be_visible()
        expect(self.page.locator('#bankSelectedName')).to_have_text('Nordea')

    def test_server_error_is_visible_and_reload_recovers(self):
        self.page.route('**/api/dashboard?**',lambda route:route.fulfill(status=500,content_type='application/json',body='{"error":"Testfejl"}'))
        self.page.reload()
        expect(self.page.locator('#loadError')).to_have_text('Testfejl')
        self.page.unroute('**/api/dashboard?**')
        self.page.reload()
        expect(self.page.locator('#appContent')).to_be_visible()
        expect(self.page.locator('#loadError')).not_to_be_visible()

if __name__=='__main__':
    unittest.main()
