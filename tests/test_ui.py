"""Browser regressions against a temporary database; pip install playwright."""
import re
import logging
import sys
import tempfile
import threading
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app
from db import connect
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

    def test_connected_bank_hides_demo_and_overrides_saved_source(self):
        from db import connect
        db_path=self.app.extensions['db_path']
        expect(self.page.locator('.mode-switch')).to_be_visible()
        with connect(db_path) as db:
            db.execute("INSERT INTO connections VALUES ('demo-hide-test','Test Bank','unused','2027-12-31','2026-10-02')")
        try:
            # A bank connected in another tab is detected on refresh too.
            self.page.locator('#currency').select_option('EUR')
            expect(self.page.locator('.mode-switch')).to_be_hidden()
            expect(self.page.locator('#demoNotice')).to_be_hidden()
            self.assertEqual(self.page.evaluate("localStorage.getItem('fjordbudget-source')"),'live')
            self.page.evaluate("localStorage.setItem('fjordbudget-source','demo')")
            self.page.reload()
            expect(self.page.locator('#appContent')).to_be_visible()
            expect(self.page.locator('.mode-switch')).to_be_hidden()
            expect(self.page.locator('#demoNotice')).to_be_hidden()
            self.assertEqual(self.page.evaluate("localStorage.getItem('fjordbudget-source')"),'live')
            expect(self.page.locator('#accountCount')).to_have_text('0')
        finally:
            with connect(db_path) as db:
                db.execute("DELETE FROM connections WHERE id='demo-hide-test'")
        self.page.reload()
        expect(self.page.locator('#appContent')).to_be_visible()
        expect(self.page.locator('.mode-switch')).to_be_visible()

    def test_savings_goals_create_edit_source_switch_and_delete(self):
        self.page.set_viewport_size({'width':390,'height':844})
        self.page.get_by_role('button',name='Opsparingsmål',exact=True).click()
        expect(self.page.locator('#savingsList')).to_contain_text('Hvad drømmer du om?')
        expect(self.page.locator('#month')).to_be_hidden()
        self.page.locator('#newSavingsGoal').click()
        self.page.locator('#savingsName').fill('Ferie <familie>')
        self.page.locator('#savingsAmount').press_sequentially('25000,50')
        expect(self.page.locator('#savingsAmount')).to_have_value('25.000,50')
        expect(self.page.locator('#savingsDeadline')).to_have_attribute('type','month')
        self.page.locator('#savingsDeadline').fill('2027-07')
        self.page.locator('#savingsForm button[type=submit]').click()
        expect(self.page.locator('.savings-card')).to_contain_text('Ferie <familie>')
        expect(self.page.locator('.savings-target')).to_contain_text('25.000,50')
        expect(self.page.locator('.savings-deadline')).to_have_text('Senest juli 2027')
        self.page.reload()
        self.page.get_by_role('button',name='Opsparingsmål',exact=True).click()
        self.page.locator('[data-edit-savings]').click()
        expect(self.page.locator('#savingsAmount')).to_have_value('25.000,50')
        expect(self.page.locator('#savingsDeadline')).to_have_value('2027-07')
        self.page.locator('#savingsName').fill('Ny bil')
        self.page.locator('#savingsForm button[type=submit]').click()
        expect(self.page.locator('.savings-card h3')).to_have_text('Ny bil')
        self.page.locator('[data-source="live"]').click()
        expect(self.page.locator('#savingsList')).to_contain_text('Hvad drømmer du om?')
        self.page.locator('[data-source="demo"]').click()
        expect(self.page.locator('.savings-card h3')).to_have_text('Ny bil')
        self.page.locator('[data-delete-savings]').click()
        self.page.locator('[data-close="savingsDeleteDialog"]').click()
        expect(self.page.locator('.savings-card')).to_have_count(1)
        self.page.locator('[data-delete-savings]').click()
        self.page.locator('#savingsDeleteForm button[type=submit]').click()
        expect(self.page.locator('#savingsList')).to_contain_text('Hvad drømmer du om?')
        self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'),390)
        self.page.get_by_role('button',name='Mit budget',exact=True).click()
        expect(self.page.locator('#month')).to_be_visible()

    def test_savings_goal_can_follow_account_balance_live(self):
        self.page.set_viewport_size({'width':390,'height':844})
        self.page.get_by_role('button',name='Opsparingsmål',exact=True).click()
        self.page.locator('#newSavingsGoal').click()
        self.page.locator('#savingsName').fill('Live konto mål')
        self.page.locator('#savingsAmount').fill('100000')
        self.page.locator('#savingsAccount').select_option('demo-save')
        expect(self.page.locator('#savingsManualAmountFields')).to_be_hidden()
        expect(self.page.locator('#savingsAccountHint')).to_contain_text('Aktuel saldo:')
        expect(self.page.locator('#savingsAccountHint')).to_contain_text('Målet følger saldoen automatisk')
        self.page.locator('#savingsDeadline').fill('2027-12')
        self.page.locator('#savingsForm button[type=submit]').click()

        card=self.page.locator('.savings-card').filter(has_text='Live konto mål')
        expect(card).to_be_visible()
        expect(card.locator('.savings-account-source')).to_contain_text('Følger Opsparing live')
        expect(card.locator('.savings-target')).to_contain_text('68.400,00')

        card.locator('[data-edit-savings]').click()
        expect(self.page.locator('#savingsAccount')).to_have_value('demo-save')
        self.page.locator('#savingsAccount').select_option('')
        expect(self.page.locator('#savingsManualAmountFields')).to_be_visible()
        expect(self.page.locator('#savingsSavedAmount')).to_have_value('0,00')
        self.page.locator('#savingsForm button[type=submit]').click()

        card=self.page.locator('.savings-card').filter(has_text='Live konto mål')
        expect(card.locator('.savings-account-source')).to_have_count(0)
        card.locator('[data-delete-savings]').click()
        self.page.locator('#savingsDeleteForm button[type=submit]').click()
        expect(self.page.locator('.savings-card').filter(has_text='Live konto mål')).to_have_count(0)

    def test_budget_category_create_delete_and_reload(self):
        self.page.set_viewport_size({'width':390,'height':844})
        self.page.locator('[data-view="budget"]').first.click()
        self.page.locator('#newBudgetCategoryName').fill('Ferie <familie>')
        self.page.locator('#budgetCategoryCreateForm button').click()
        expect(self.page.locator('#budgetFull')).to_contain_text('Ferie <familie>')
        self.page.locator('.full-budget .edit-budget').click()
        expect(self.page.get_by_label('Budget for Ferie <familie>', exact=True)).to_be_visible()
        self.page.get_by_label('Budget for Ferie <familie>', exact=True).fill('1234')
        self.page.locator('#budgetForm button[type=submit]').click()
        expect(self.page.locator('#budgetDialog')).not_to_be_visible()
        self.page.reload()
        self.page.locator('[data-view="budget"]').first.click()
        button=self.page.get_by_role('button',name='Fjern budgetkategori Ferie <familie>',exact=True)
        button.click()
        self.page.locator('[data-close="budgetCategoryDeleteDialog"]').click()
        expect(button).to_be_visible()
        button.click()
        self.page.locator('#budgetCategoryDeleteForm button[type=submit]').click()
        expect(self.page.locator('#budgetCategoryDeleteDialog')).not_to_be_visible()
        expect(button).to_have_count(0)
        self.page.reload()
        self.page.locator('[data-view="budget"]').first.click()
        expect(button).to_have_count(0)
        self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'),390)

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
        self.page.route('**/api/accounts/manage?pending=1',lambda route:route.fulfill(json={'items':[]}))
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

    def test_last_sync_timestamp_is_shown_below_refresh_button(self):
        data=self.app.test_client().get('/api/dashboard?source=demo').json
        data['has_bank_connections']=False
        data['accounts'][0]['synced_at']='2026-10-07T13:22:00+00:00'
        self.page.route('**/api/dashboard?source=live*',lambda route:route.fulfill(json=data))
        self.page.locator('[data-source="live"]').click()
        expect(self.page.locator('#lastSyncText')).to_be_visible()
        expect(self.page.locator('#lastSyncText')).to_contain_text('Senest opdateret:')
        expect(self.page.locator('#lastSyncText')).to_contain_text('7. okt. 2026')
        expect(self.page.locator('#lastSyncText')).to_contain_text('kl.')

    def test_account_manager_can_toggle_accounts_on_mobile(self):
        accounts=[
            {'id':'a1','name':'Lønkonto','custom_name':None,'bank':'Test Bank','last4':'1111','currency':'DKK','balance':10000,'synced_at':None,'included':1,'selection_pending':0,'connected':1},
            {'id':'a2','name':'Ekstra konto','custom_name':None,'bank':'Test Bank','last4':'2222','currency':'DKK','balance':20000,'synced_at':None,'included':0,'selection_pending':0,'connected':1},
        ]
        saved=[]
        def manage(route):
            if route.request.method=='PUT':
                saved.append(route.request.post_data_json)
                route.fulfill(json={'ok':True,'active':1})
            else:
                route.fulfill(json={'items':accounts})
        self.page.route('**/api/accounts/manage',manage)
        self.page.set_viewport_size({'width':390,'height':844})
        self.page.locator('[data-source="live"]').click()
        expect(self.page.locator('#manageAccountsButton')).to_be_hidden()
        self.page.locator('nav [data-view="accounts"]').click()
        expect(self.page.locator('#manageAccountsButton')).to_be_visible()
        self.page.locator('#manageAccountsButton').click()
        expect(self.page.locator('#accountVisibilityDialog')).to_be_visible()
        expect(self.page.locator('#accountVisibilityFields [data-account-visible]')).to_have_count(2)
        self.page.locator('label[for="visibleAccount0"]').click()
        self.page.locator('label[for="visibleAccount1"]').click()
        expect(self.page.locator('[data-account-visible="a1"]')).not_to_be_checked()
        expect(self.page.locator('[data-account-visible="a2"]')).to_be_checked()
        self.page.locator('#saveAccountVisibility').click()
        expect(self.page.locator('#accountVisibilityDialog')).not_to_be_visible()
        self.assertTrue(saved)
        self.assertEqual(saved[-1]['included'],{'a1':False,'a2':True})
    def test_category_create_budget_delete_and_mobile(self):
        self.page.set_viewport_size({'width':390,'height':844})
        self.page.locator('nav [data-view="categories"]').click()
        self.page.locator('#newCategoryName').fill('Holiday <test>')
        self.page.get_by_role('button',name='Tilføj kategori',exact=True).click()
        expect(self.page.locator('#categoryList')).to_contain_text('Holiday <test>')
        merchant_required=self.page.get_by_label('Kræver forhandler for Holiday <test>',exact=True)
        expect(merchant_required).to_be_checked()
        merchant_required.click(force=True)
        expect(merchant_required).not_to_be_checked()
        expect(self.page.locator('#toast')).to_have_text('Forhandler er ikke længere påkrævet')
        link=self.page.get_by_label('Budgetkategori for Holiday <test>',exact=True)
        expect(link).to_have_value('')
        link.select_option('Fritid')
        expect(self.page.locator('#toast')).to_have_text('Budgettilknytning gemt')
        self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'),390)
        self.page.locator('[data-view="budget"]').first.click()
        self.page.locator('.full-budget .edit-budget').click()
        expect(self.page.get_by_label('Budget for Holiday <test>')).to_have_count(0)
        expect(self.page.get_by_label('Budget for Fritid',exact=True)).to_be_visible()
        self.page.keyboard.press('Escape')
        self.page.locator('nav [data-view="categories"]').click()
        self.page.get_by_role('button',name='Slet Holiday <test>',exact=True).click()
        self.page.get_by_role('button',name='Flyt og slet',exact=True).click()
        expect(self.page.locator('#categoryDeleteDialog')).not_to_be_visible()
        expect(self.page.locator('#categoryList')).not_to_contain_text('Holiday <test>')
        self.page.reload()
        self.page.locator('nav [data-view="categories"]').click()
        expect(self.page.locator('#categoryList')).not_to_contain_text('Holiday <test>')

    def test_custom_category_can_be_renamed_from_category_menu(self):
        self.page.set_viewport_size({'width':390,'height':844})
        self.page.locator('nav [data-view="categories"]').click()
        self.page.locator('#newCategoryName').fill('Omdøb mig')
        self.page.get_by_role('button',name='Tilføj kategori',exact=True).click()
        rename=self.page.get_by_role('button',name='Omdøb Omdøb mig',exact=True)
        expect(rename).to_be_visible()
        rename.click()
        expect(self.page.locator('#categoryRenameDialog')).to_be_visible()
        expect(self.page.locator('#categoryRenameInput')).to_have_value('Omdøb mig')
        self.page.locator('#categoryRenameInput').fill('Nyt kategorinavn')
        self.page.locator('#saveCategoryRename').click()
        expect(self.page.locator('#categoryRenameDialog')).not_to_be_visible()
        expect(self.page.locator('#categoryList')).to_contain_text('Nyt kategorinavn')
        expect(self.page.locator('#categoryList')).not_to_contain_text('Omdøb mig')
        expect(self.page.get_by_role('button',name='Omdøb Nyt kategorinavn',exact=True)).to_be_visible()
        self.page.reload()
        self.page.locator('nav [data-view="categories"]').click()
        expect(self.page.locator('#categoryList')).to_contain_text('Nyt kategorinavn')
        expect(self.page.get_by_role('button',name='Omdøb Andet',exact=True)).to_have_count(0)

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
        # A completed demo month always contains Netto, including early in a month.
        month=self.app.test_client().get('/api/dashboard?source=demo').json['months'][1]
        self.page.locator('#month').fill(month)
        self.page.locator('#month').dispatch_event('change')
        self.page.locator('[data-account="demo-daily"]').click()
        expect(self.page.locator('#accountFilter')).to_have_value('demo-daily')
        self.page.locator('#search').fill('Netto')
        expect(self.page.locator('#transactionRows')).to_contain_text('Netto')
        for text in self.page.locator('.merchant-kind').all_text_contents():
            self.assertIn(' \u00b7 ',text)
            self.assertNotIn('?',text)
        expect(self.page.locator('#transactionRows')).not_to_contain_text('Spotify')
        self.assertEqual(self.page.evaluate("transactionState({amount:-6792,category:'Andet',merchant:null,requires_merchant:1}).className"),'transaction-partial')
        self.assertEqual(self.page.evaluate("transactionState({amount:-6792,category:'',merchant:null,requires_merchant:1}).className"),'transaction-incomplete')
        self.assertEqual(self.page.evaluate("transactionState({amount:-6792,category:'Andet',merchant:'McDonald\\'s',requires_merchant:1}).className"),'transaction-complete')
        self.assertEqual(self.page.evaluate("transactionState({amount:-6792,category:'Overførsler',merchant:null,requires_merchant:0}).className"),'transaction-complete')
        merchant_control=self.page.locator('#transactionRows .merchant-picker-button').first
        expect(merchant_control).to_be_visible()
        category=self.page.locator('#transactionRows .category-picker-button').first
        category.click()
        expect(self.page.locator('#categoryDialog')).to_be_visible()
        self.page.locator('#categorySearch').fill('Fri')
        expect(self.page.locator('#categoryOptions .choice-option')).to_have_count(1)
        self.page.locator('#categoryOptions .choice-option').click()
        expect(self.page.locator('#categorySearch')).to_have_value('Fritid')
        self.page.locator('#savePickedCategory').click()
        expect(self.page.locator('#toast')).to_contain_text('Huskes fremover.')

        self.page.locator('#transactionRows [data-edit-merchant]').first.click()
        expect(self.page.locator('#merchantDialog')).to_be_visible()
        self.page.locator('#merchantName').fill('Matas')
        expect(self.page.locator('#merchantOptions .choice-option')).to_have_count(1)
        self.page.locator('#merchantOptions .choice-option').click()
        expect(self.page.locator('#merchantName')).to_have_value('Matas')
        self.page.keyboard.press('Escape')

        self.page.reload()
        self.page.locator('#month').fill(month)
        self.page.locator('#month').dispatch_event('change')
        self.page.locator('.nav-item[data-view="transactions"]').click()
        self.page.locator('#search').fill('Netto')
        expect(self.page.locator('#transactionRows .category-picker-button').first).to_contain_text('Fritid')

    def test_merchant_picker_follows_category_requirement_and_category_is_first(self):
        month=self.app.test_client().get('/api/dashboard?source=demo').json['months'][1]
        self.page.locator('#month').fill(month)
        self.page.locator('#month').dispatch_event('change')
        self.page.locator('.nav-item[data-view="transactions"]').click()
        self.page.locator('#search').fill('Til budgetkonto')
        row=self.page.locator('#transactionRows tr').first
        expect(row).to_be_visible()
        expect(row.locator('.category-picker-button')).to_contain_text('Overførsler')
        expect(row.locator('.merchant-picker-button')).to_have_count(0)
        self.assertEqual(row.locator('td:nth-child(3)').evaluate("el=>el.cellIndex"),2)
        self.assertEqual(row.locator('td:nth-child(4)').evaluate("el=>el.cellIndex"),3)

        row.locator('.category-picker-button').click()
        self.page.locator('#categorySearch').fill('Fritid')
        self.page.locator('#categoryOptions .choice-option').click()
        self.page.locator('#savePickedCategory').click()
        expect(self.page.locator('#categoryDialog')).not_to_be_visible()
        row=self.page.locator('#transactionRows tr').first
        expect(row.locator('.category-picker-button')).to_contain_text('Fritid')
        expect(row.locator('.merchant-picker-button')).to_be_visible()

        row.locator('.category-picker-button').click()
        self.page.locator('#categorySearch').fill('Overførsler')
        self.page.locator('#categoryOptions .choice-option').click()
        self.page.locator('#savePickedCategory').click()
        row=self.page.locator('#transactionRows tr').first
        expect(row.locator('.category-picker-button')).to_contain_text('Overførsler')
        expect(row.locator('.merchant-picker-button')).to_have_count(0)

    def test_category_can_be_created_directly_from_transaction_picker(self):
        month=self.app.test_client().get('/api/dashboard?source=demo').json['months'][1]
        self.page.locator('#month').fill(month)
        self.page.locator('#month').dispatch_event('change')
        self.page.locator('.nav-item[data-view="transactions"]').click()
        self.page.locator('#search').fill('Netto')
        row=self.page.locator('#transactionRows tr').first
        row.locator('.category-picker-button').click()
        expect(self.page.locator('#categoryDialog')).to_be_visible()
        self.page.locator('#categorySearch').fill('Børn test')
        create=self.page.locator('[data-create-category="Børn test"]')
        expect(create).to_be_visible()
        create.click()
        expect(self.page.locator('#categorySearch')).to_have_value('Børn test')
        expect(self.page.locator('#categoryOptions .choice-option.selected')).to_have_text('Børn test')
        expect(self.page.locator('#categoryFilter option')).to_contain_text(['Børn test'])
        self.page.locator('#savePickedCategory').click()
        expect(self.page.locator('#categoryDialog')).not_to_be_visible()
        expect(self.page.locator('#toast')).to_contain_text('Kategori gemt')
        self.page.reload()
        expect(self.page.locator('#appContent')).to_be_visible()
        self.page.locator('#month').fill(month)
        self.page.locator('#month').dispatch_event('change')
        self.page.locator('.nav-item[data-view="transactions"]').click()
        self.page.locator('#search').fill('Netto')
        expect(self.page.locator('#transactionRows .category-picker-button').first).to_contain_text('Børn test')
        categories=self.app.test_client().get('/api/categories').json['items']
        created=next(item for item in categories if item['name']=='Børn test')
        self.assertEqual(created['requires_merchant'],1)

    def test_merchant_library_lists_and_deletes_saved_bank_texts_and_merchants(self):
        month=self.app.test_client().get('/api/dashboard?source=demo').json['months'][1]
        self.page.locator('#month').fill(month)
        self.page.locator('#month').dispatch_event('change')
        self.page.locator('.nav-item[data-view="transactions"]').click()
        self.page.locator('#search').fill('Netto')
        row=self.page.locator('#transactionRows tr').first
        row.locator('.merchant-picker-button').click()
        self.page.locator('#merchantName').fill('UI Merchant')
        self.page.locator('#saveMerchant').click()
        expect(self.page.locator('#merchantDialog')).not_to_be_visible()

        self.page.get_by_role('button',name='Forhandlere',exact=True).click()
        expect(self.page.locator('[data-section="merchants"]')).to_be_visible()
        card=self.page.locator('[data-library-merchant="UI Merchant"]')
        expect(card).to_be_visible()
        expect(card.locator('[data-delete-merchant-rule]')).to_have_count(1)

        card.locator('[data-delete-merchant-rule]').click()
        expect(self.page.locator('#merchantDeleteDialog')).to_be_visible()
        expect(self.page.locator('#merchantDeleteText')).to_contain_text('beholder deres forhandler')
        self.page.locator('#confirmMerchantDelete').click()
        expect(self.page.locator('#merchantDeleteDialog')).not_to_be_visible()
        card=self.page.locator('[data-library-merchant="UI Merchant"]')
        expect(card).to_be_visible()
        expect(card.locator('[data-delete-merchant-rule]')).to_have_count(0)

        card.locator('[data-delete-library-merchant]').click()
        expect(self.page.locator('#merchantDeleteText')).to_contain_text('fjernes også fra eksisterende posteringer')
        self.page.locator('#confirmMerchantDelete').click()
        expect(self.page.locator('[data-library-merchant="UI Merchant"]')).to_have_count(0)

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

    def test_overview_uses_incomplete_card_and_transactions_live_in_menu(self):
        with connect(self.app.extensions['db_path']) as db:
            db.execute("""INSERT INTO transactions(account_id,external_id,booked_on,description,amount,currency,category)
                          VALUES ('demo-daily','overview-missing',?,'Overview missing test',-1234,'DKK','Andet')""",
                       (date.today().isoformat(),))
        self.page.reload()
        expect(self.page.locator('#appContent')).to_be_visible()
        expect(self.page.locator('[data-section="transactions"]')).not_to_be_visible()
        card=self.page.locator('#incompleteTransactionsCard')
        expect(card).to_be_visible()
        before=int(self.page.locator('#incompleteTransactionsCount').inner_text())
        self.assertGreaterEqual(before,1)

        card.click()
        expect(self.page.locator('#incompleteTransactionsDialog')).to_be_visible()
        modal_row=self.page.locator('#incompleteTransactionRows tr').filter(has_text='Overview missing test')
        expect(modal_row).to_be_visible()
        modal_row.locator('.merchant-picker-button').click()
        expect(self.page.locator('#merchantDialog')).to_be_visible()
        self.page.locator('#merchantName').fill('Overview Merchant')
        self.page.locator('#saveMerchant').click()
        expect(self.page.locator('#merchantDialog')).not_to_be_visible()
        expect(self.page.locator('#incompleteTransactionRows tr').filter(has_text='Overview missing test')).to_have_count(0)
        expect(self.page.locator('#incompleteTransactionsCount')).to_have_text(str(before-1))

        self.page.locator('#viewAllTransactions').click()
        expect(self.page.locator('#incompleteTransactionsDialog')).not_to_be_visible()
        expect(self.page.locator('[data-section="transactions"]')).to_be_visible()
        expect(self.page.locator('#transactionRows')).to_contain_text('Overview missing test')

    def test_mobile_layout_dialog_and_navigation(self):
        self.page.set_viewport_size({'width':390,'height':844})
        expect(self.page.locator('#appContent')).to_be_visible()
        self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'),390)
        expect(self.page.locator('.summary-card .metric-icon svg')).to_have_count(3)
        self.assertEqual(self.page.locator('.summary-card .metric-icon').evaluate_all("els=>els.map(el=>el.textContent.trim())"),['','',''])
        self.assertEqual(self.page.locator('.nav-item[data-view="overview"] use').get_attribute('href'),'#icon-grid')
        self.assertEqual(self.page.locator('.nav-item[data-view="categories"] use').get_attribute('href'),'#icon-tag')
        self.page.locator('.nav-item[data-view="budget"]').click()
        expect(self.page.locator('.full-budget')).to_be_visible()
        self.page.locator('.full-budget .edit-budget').click()
        expect(self.page.get_by_role('button',name='Gem budget',exact=True)).to_be_visible()
        self.assertLessEqual(self.page.locator('#budgetDialog').bounding_box()['width'],390)
        self.page.keyboard.press('Escape')
        self.page.locator('.nav-item[data-view="transactions"]').click()
        expect(self.page.locator('[data-section="transactions"]')).to_be_visible()
        self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'),390)
        self.assertEqual(self.page.locator('#search').evaluate("el=>getComputedStyle(el).fontSize"),'16px')
        self.assertEqual(self.page.locator('#accountFilter').evaluate("el=>getComputedStyle(el).fontSize"),'16px')
        self.assertEqual(self.page.locator('#categoryFilter').evaluate("el=>getComputedStyle(el).fontSize"),'16px')
        self.assertEqual(self.page.locator('#merchantName').evaluate("el=>getComputedStyle(el).fontSize"),'16px')
        amount=self.page.locator('td.amount-cell').first.bounding_box()
        self.assertLessEqual(amount['x']+amount['width'],390)
        colored=self.page.locator('#transactionRows tr.transaction-complete, #transactionRows tr.transaction-partial, #transactionRows tr.transaction-incomplete').first
        expect(colored).to_be_visible()
        self.assertNotEqual(colored.evaluate("el=>getComputedStyle(el).backgroundColor"),'rgba(0, 0, 0, 0)')
        self.assertEqual(colored.locator('td').first.evaluate("el=>getComputedStyle(el).backgroundColor"),'rgba(0, 0, 0, 0)')

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
