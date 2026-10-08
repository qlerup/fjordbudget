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
  const channel = typeof BroadcastChannel === 'function' ? new BroadcastChannel('fjordhub-access') : null;
  if (channel) channel.onmessage = event => {if (event.data === 'revoked') showRemoved(false);};
  function showRemoved(broadcast = true) {
    if (removed || notified) return;
    removed = true;
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
  async function check() {
    if (pending || removed) return;
    pending = true;
    try {
      const response = await originalFetch('/api/auth/access', {cache:'no-store', credentials:'same-origin', signal:AbortSignal.timeout(8000)});
      await inspect(response);
      // A fresh authenticated session must receive its own future revocation notice.
      if (response.ok && !removed) {
        const data = await response.clone().json();
        if (data.authenticated === true) rememberNotice(false);
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
    check();
    window.setInterval(check, 5000);
    window.addEventListener('focus', check);
    document.addEventListener('visibilitychange', () => {if (!document.hidden) check();});
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, {once:true});
  else start();
})();
