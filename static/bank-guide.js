'use strict';
window.FjordBankGuide = (() => {
  const element = id => document.getElementById(id);
  const guide = element('bankGuide');
  const steps = [...guide.querySelectorAll('[data-bank-step]')];
  let fileRead = 0;

  function showStep(number) {
    guide.open = true;
    steps.forEach(step => { step.open = Number(step.dataset.bankStep) === number; });
    const step = steps.find(step => Number(step.dataset.bankStep) === number);
    step?.querySelector('summary').focus();
    step?.scrollIntoView({ block: 'nearest' });
  }

  function credentials() {
    guide.open = false;
    element('bankSetup').hidden = false;
    element('bankPicker').hidden = true;
    element('editBankCredentials').hidden = true;
    element('bankAppId').focus();
  }

  function prepare(config) {
    element('callbackUrl').textContent = config.callback_url;
    element('privacyUrl').textContent = config.privacy_url || location.origin + '/privacy';
    element('termsUrl').textContent = config.terms_url || location.origin + '/terms';
    let publicHttps = false;
    try {
      const address = new URL(config.callback_url);
      publicHttps = address.protocol === 'https:' && address.hostname !== 'localhost'
        && !address.hostname.startsWith('[') && !/^\d+\.\d+\.\d+\.\d+$/.test(address.hostname);
    } catch { /* An invalid callback is explained by the same notice. */ }
    element('bankGuideHttpsWarning').hidden = publicHttps;
    guide.open = !config.provider_configured;
    steps.forEach((step, index) => { step.open = index === 0; });
  }

  guide.addEventListener('click', async event => {
    const next = event.target.closest('[data-bank-next]');
    if (next) { showStep(Number(next.dataset.bankNext)); return; }
    const target = event.target.closest('[data-bank-target]');
    if (target) {
      if (target.dataset.bankTarget === 'credentials' || element('bankPicker').hidden) credentials();
      else { guide.open = false; element('bankDropdownButton').focus(); }
      return;
    }
    const copy = event.target.closest('[data-bank-copy]');
    if (!copy) return;
    const field = element(copy.dataset.bankCopy);
    const text = 'value' in field ? field.value : field.textContent;
    if (!text.trim()) { field.focus(); toast('Indtast din egen e-mailadresse først.'); return; }
    if (field.type === 'email' && !field.reportValidity()) return;
    try {
      if (!navigator.clipboard) throw new Error('Clipboard unavailable');
      await navigator.clipboard.writeText(text);
      toast('Kopieret. Indsæt teksten hos Enable Banking.');
    } catch {
      field.focus();
      if (typeof field.select === 'function') field.select();
      else {
        const range = document.createRange(); range.selectNodeContents(field);
        const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
      }
      toast('Teksten er markeret. Kopiér den manuelt.');
    }
  });

  element('bankPrivateKeyFile').addEventListener('change', async event => {
    const version = ++fileRead;
    const file = event.target.files[0];
    if (!file) return;
    element('bankPrivateKey').value = '';
    try {
      if (file.size > 12000) throw new Error('Nøglefilen er for stor. Vælg den private .pem-fil fra Enable Banking.');
      const text = await file.text();
      if (version !== fileRead || !element('bankDialog').open) return;
      if (!/-----BEGIN (RSA )?PRIVATE KEY-----/.test(text)) throw new Error('Filen skal indeholde en privat PEM-nøgle med BEGIN og END. Vælg den private nøgle, ikke certifikatet.');
      element('bankPrivateKey').value = text.trim();
      showError('bankError', '');
      toast('Nøglefilen er læst. Kontrollér app-id, og tryk Gem og fortsæt.');
    } catch (error) {
      if (version === fileRead && element('bankDialog').open) showError('bankError', error.message || 'Nøglefilen kunne ikke læses.');
    }
  });
  element('bankDialog').addEventListener('close', () => { fileRead++; element('bankGuideEmail').value = ''; });

  return { prepare, showStep, credentials };
})();
