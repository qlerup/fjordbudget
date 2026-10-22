"""Bank setup walkthrough and credential-file handling against a temporary app."""
import logging
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server
from app import create_app


class BankGuideBrowserTests(unittest.TestCase):
    def setUp(self):
        logging.getLogger('werkzeug').setLevel(logging.ERROR)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.public = 'https://budget-for-a-new-owner.example.test'
        self.app = create_app({'TESTING': True, 'AUTH_MODE': 'local', 'DATA_DIR': self.temp.name,
                               'APP_URL': self.public})
        self.server = make_server('127.0.0.1', 0, self.app, threaded=True)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.url = 'http://localhost:' + str(self.server.server_port)
        self.pw = sync_playwright().start()
        self.addCleanup(self.pw.stop)
        self.browser = self.pw.chromium.launch()
        self.addCleanup(self.browser.close)

    def test_guide_fields_copy_navigation_and_mobile_for_new_and_existing_setups(self):
        for width in (1440, 390, 320):
            with self.subTest(width=width):
                context = self.browser.new_context(viewport={'width': width, 'height': 950})
                context.add_init_script("Object.defineProperty(navigator,'clipboard',{value:{writeText:async value=>{window.guideCopied=value;}}});")
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(self.url)
                page.locator('[data-connect]:visible').first.click()
                guide = page.locator('#bankGuide')
                expect(guide).to_have_attribute('open', '')
                expect(page.locator('[data-bank-step="1"]')).to_have_attribute('open', '')
                expect(page.locator('#bankGuideHttpsWarning')).to_be_hidden()
                page.locator('[data-bank-next="2"]').click()
                step = page.locator('[data-bank-step="2"]')
                expect(step).to_be_visible()
                expect(step).to_contain_text('PRODUCTION')
                expect(step).to_contain_text('Account Information (AIS)')
                for field, expected in [('bankGuideAppName', 'FjordBudget'), ('privacyUrl', self.public + '/privacy'),
                                        ('termsUrl', self.public + '/terms'), ('callbackUrl', self.public + '/bank/callback')]:
                    expect(page.locator('#' + field)).to_have_text(expected)
                    page.locator('[data-bank-copy="' + field + '"]').click()
                    page.wait_for_function('(expected)=>window.guideCopied===expected', arg=expected)
                page.locator('#bankGuideEmail').fill('owner@example.test')
                page.locator('[data-bank-copy="bankGuideEmail"]').click()
                page.wait_for_function("window.guideCopied==='owner@example.test'")
                # A blocked clipboard still selects the exact complete value.
                page.evaluate("() => {navigator.clipboard.writeText=async()=>{throw new Error('blocked');};}")
                page.locator('[data-bank-copy="callbackUrl"]').click()
                expect(page.locator('#toast')).to_contain_text('markeret')
                self.assertEqual(page.evaluate('window.getSelection().toString()'), self.public + '/bank/callback')
                output = Path(__file__).resolve().parents[1] / 'test-results'
                output.mkdir(exist_ok=True)
                page.screenshot(path=str(output / f'bank-guide-fields-{width}.png'), full_page=True)
                self.assertFalse(page.locator('#bankDialog').evaluate('el=>el.scrollWidth>el.clientWidth'))
                page.locator('[data-bank-next="3"]').click()
                expect(page.locator('[data-bank-step="3"]')).to_contain_text('Activate by linking accounts')
                expect(page.locator('[data-bank-step="3"]')).to_contain_text('Link accounts')
                expect(page.locator('[data-bank-step="3"]')).to_contain_text('Restricted')
                page.locator('[data-bank-next="4"]').click()
                page.get_by_role('button', name='Indsæt API-oplysninger', exact=True).click()
                expect(page.locator('#bankAppId')).to_be_focused()
                page.keyboard.press('Escape')
                expect(page.locator('#bankGuideEmail')).to_have_value('')
                # A configured owner can reopen the full guide to add more banks.
                page.evaluate('config.provider_configured=true')
                page.route('**/api/banks', lambda route: route.fulfill(json={'banks': [{'name': 'Kreditbanken'}]}))
                page.locator('[data-connect]:visible').first.click()
                self.assertFalse(guide.evaluate('el=>el.open'))
                expect(page.locator('.bank-guide-picker-note')).to_contain_text('Link accounts')
                guide.locator(':scope > summary').click()
                page.locator('[data-bank-step="5"] > summary').click()
                page.get_by_role('button', name='Vælg bank i FjordBudget').click()
                expect(page.locator('#bankDropdownButton')).to_be_focused()
                page.locator('#editBankCredentials').click()
                expect(page.locator('#bankSetup')).to_be_visible()
                self.assertEqual(errors, [])
                context.close()

    def test_key_file_save_keeps_secrets_private_and_advances_to_bank_consent(self):
        page = self.browser.new_page()
        page.route('**/api/banks', lambda route: route.fulfill(json={'banks': [{'name': 'Kreditbanken'}]}))
        page.goto(self.url)
        page.locator('[data-connect]:visible').first.click()
        page.locator('[data-bank-step="4"] > summary').click()
        page.get_by_role('button', name='Indsæt API-oplysninger', exact=True).click()
        page.locator('#bankPrivateKeyFile').set_input_files({'name': 'wrong.pem', 'mimeType': 'text/plain', 'buffer': b'not a private key'})
        expect(page.locator('#bankError')).to_contain_text('privat PEM')
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        app_id = str(uuid.uuid4())
        page.locator('#bankAppId').fill(app_id)
        page.locator('#bankPrivateKeyFile').set_input_files({'name': app_id + '.pem', 'mimeType': 'text/plain', 'buffer': key})
        expect(page.locator('#bankPrivateKey')).to_have_value(key.decode().strip())
        expect(page.locator('#bankError')).to_be_hidden()
        page.locator('#saveBankCredentials').click()
        expect(page.locator('#bankPicker')).to_be_visible()
        expect(page.locator('[data-bank-step="5"]')).to_have_attribute('open', '')
        expect(page.locator('#bankPrivateKey')).to_have_value('')
        self.assertNotIn(b'PRIVATE KEY', (Path(self.temp.name) / 'bank-credentials.enc').read_bytes())
        self.assertNotIn('PRIVATE KEY', page.request.get(self.url + '/api/config').text())
        page.locator('#editBankCredentials').click()
        # Closing while a file read is pending must not repopulate the secret.
        page.evaluate("() => {File.prototype.text=function(){return new Promise(resolve=>window.finishGuideFile=resolve);};}")
        page.locator('#bankPrivateKeyFile').set_input_files({'name': 'late.pem', 'mimeType': 'text/plain', 'buffer': key})
        page.keyboard.press('Escape')
        page.evaluate('(key)=>window.finishGuideFile(key)', key.decode())
        expect(page.locator('#bankPrivateKey')).to_have_value('')
        page.close()

    def test_local_address_explains_https_before_creating_an_application(self):
        self.app.config['APP_URL'] = 'http://192.168.1.254:8060'
        page = self.browser.new_page()
        page.goto(self.url)
        page.locator('[data-connect]:visible').first.click()
        expect(page.locator('#bankGuideHttpsWarning')).to_be_visible()
        expect(page.locator('#bankGuideHttpsWarning')).to_contain_text('https://-domæne')
        self.assertIn('192.168.1.254', page.locator('#callbackUrl').text_content())
        page.close()


if __name__ == '__main__':
    unittest.main()
