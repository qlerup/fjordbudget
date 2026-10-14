/* Keep an open tab synchronized with server-side FjordHub app access. */
(() => {
  'use strict';
  if (window.fjordHubSessionMonitor) return;
  window.fjordHubSessionMonitor = true;
  const script = document.currentScript;
  const loginUrl = script?.dataset.loginUrl || '/login';
  const originalFetch = window.fetch.bind(window);
  const noticeKey = 'fjordhub-access-notice';
  let notified = false;
  try { notified = sessionStorage.getItem(noticeKey) === 'shown'; } catch { /* Storage may be disabled. */ }
  function rememberNotice(value) {
    notified = value;
    try {
      if (value) sessionStorage.setItem(noticeKey, 'shown');
      else sessionStorage.removeItem(noticeKey);
    } catch { /* The current page still suppresses duplicate notices. */ }
  }
  let removed = false, pending = false;
  let emailDialog = null, emailCsrf = '';
  const channel = typeof BroadcastChannel === 'function' ? new BroadcastChannel('fjordhub-access') : null;
  if (channel) channel.onmessage = event => {
    if (event.data === 'revoked') showRemoved(false);
    if (event.data === 'email-updated') check(true);
  };
  function finishEmail() {
    emailDialog?.close(); emailDialog?.remove(); emailDialog = null;
    window.location.reload();
  }
  function showEmail(data) {
    if (removed) return;
    rememberNotice(false);
    emailCsrf = typeof data.csrf_token === 'string' ? data.csrf_token : '';
    if (emailDialog) return;
    document.querySelectorAll('video,audio').forEach(media => media.pause());
    const dialog = document.createElement('dialog');
    emailDialog = dialog;
    dialog.className = 'hub-access-removed hub-email-required';
    dialog.setAttribute('aria-labelledby', 'hubEmailTitle');
    dialog.innerHTML = '<h2 id="hubEmailTitle">Opdater din email</h2><p>Din administrator kræver, at du bekræfter eller opdaterer din email, før du fortsætter.</p><form><label for="hubEmailInput">Email</label><input id="hubEmailInput" name="email" type="email" autocomplete="email" maxlength="254" required><p class="hub-email-error" role="alert"></p><button type="submit">Gem email og fortsæt</button></form>';
    dialog.addEventListener('cancel', event => event.preventDefault());
    dialog.querySelector('form').onsubmit = async event => {
      event.preventDefault();
      const button = dialog.querySelector('button'), error = dialog.querySelector('.hub-email-error');
      button.disabled = true; error.textContent = '';
      try {
        const response = await originalFetch('/api/auth/email', {method:'POST', credentials:'same-origin',
          headers:{'Content-Type':'application/json', 'X-CSRF-Token':emailCsrf},
          body:JSON.stringify({email:dialog.querySelector('input').value.trim()}), signal:AbortSignal.timeout(12000)});
        const result = await response.json();
        if (!response.ok || result.accepted === false || result.ok === false)
          throw new Error(typeof result.error === 'string' ? result.error : 'Emailen kunne ikke gemmes. Prøv igen.');
        channel?.postMessage('email-updated');
        finishEmail();
      } catch (failure) { error.textContent = failure.message || 'Emailen kunne ikke gemmes. Prøv igen.'; button.disabled = false; }
    };
    document.body.append(dialog); dialog.showModal(); dialog.querySelector('input').focus();
  }
  function showRemoved(broadcast = true) {
    if (removed || notified) return;
    removed = true;
    emailDialog?.close(); emailDialog?.remove(); emailDialog = null;
    rememberNotice(true);
    if (broadcast) channel?.postMessage('revoked');
    document.querySelectorAll('video,audio').forEach(media => media.pause());
    const dialog = document.createElement('dialog');
    dialog.className = 'hub-access-removed';
    dialog.setAttribute('aria-labelledby', 'hubAccessRemovedTitle');
    dialog.innerHTML = '<h2 id="hubAccessRemovedTitle">Adgang fjernet</h2><p>Din adgang er blevet fjernet. Du bliver automatisk logget ud.</p><button type="button">Gå til login</button>';
    let timer;
    const leave = () => {
      window.clearTimeout(timer);
      dialog.close();
      window.location.replace(loginUrl);
    };
    dialog.querySelector('button').onclick = leave;
    dialog.addEventListener('cancel', event => { event.preventDefault(); leave(); });
    document.body.append(dialog);
    dialog.showModal();
    timer = window.setTimeout(leave, 4000);
  }
  async function inspect(response) {
    if (response.status === 403) {
      try {
        const data = await response.clone().json();
        const detail = typeof data.detail === 'object' && data.detail ? data.detail : data;
        if (detail.error_code === 'email_update_required') showEmail(detail);
      } catch { /* Unrelated 403 responses do not trigger an email requirement. */ }
      return;
    }
    if (response.status !== 401) return;
    if (response.headers.get('X-FjordHub-Access') === 'revoked') {showRemoved(); return;}
    try {
      const data = await response.clone().json();
      if (data.error_code === 'access_revoked') showRemoved();
    } catch { /* Non-JSON or expired local sessions are not revocations. */ }
  }
  window.fetch = async (...args) => {
    const response = await originalFetch(...args);
    await inspect(response);
    return response;
  };
  async function check(fresh = false) {
    if (pending || removed) return;
    pending = true;
    try {
      const response = await originalFetch('/api/auth/access' + (fresh === true ? '?fresh=1' : ''), {cache:'no-store', credentials:'same-origin', signal:AbortSignal.timeout(8000)});
      await inspect(response);
      // A fresh authenticated session must receive its own future revocation notice.
      if (response.ok && !removed) {
        const data = await response.clone().json();
        if (data.authenticated === true) rememberNotice(false);
        if (data.authenticated === true && emailDialog) finishEmail();
      }
    } catch { /* Retry on the next heartbeat; an outage is not a revocation. */ }
    finally { pending = false; }
  }
  const start = () => {
    const url = new URL(window.location.href);
    if (url.searchParams.get('access_removed') === '1') {
      url.searchParams.delete('access_removed');
      window.history.replaceState(window.history.state, '', url);
      showRemoved();
    }
    check(true);
    window.setInterval(check, 5000);
    window.addEventListener('focus', () => check(true));
    window.addEventListener('pageshow', () => check(true));
    document.addEventListener('visibilitychange', () => {if (!document.hidden) check(true);});
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, {once:true});
  else start();
})();
