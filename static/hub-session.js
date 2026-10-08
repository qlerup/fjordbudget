/* Keep an open tab synchronized with server-side FjordHub app access. */
(() => {
  'use strict';
  if (window.fjordHubSessionMonitor) return;
  window.fjordHubSessionMonitor = true;
  const script = document.currentScript;
  const loginUrl = script?.dataset.loginUrl || '/login';
  const originalFetch = window.fetch.bind(window);
  let removed = false, pending = false;
  const channel = typeof BroadcastChannel === 'function' ? new BroadcastChannel('fjordhub-access') : null;
  if (channel) channel.onmessage = event => {if (event.data === 'revoked') showRemoved(false);};
  function showRemoved(broadcast = true) {
    if (removed) return;
    removed = true;
    if (broadcast) channel?.postMessage('revoked');
    document.querySelectorAll('video,audio').forEach(media => media.pause());
    const dialog = document.createElement('dialog');
    dialog.className = 'hub-access-removed';
    dialog.setAttribute('aria-labelledby', 'hubAccessRemovedTitle');
    dialog.innerHTML = '<h2 id="hubAccessRemovedTitle">Adgang fjernet</h2><p>Din adgang er blevet fjernet. Du bliver automatisk logget ud.</p><button type="button">Gå til login</button>';
    const leave = () => window.location.replace(loginUrl);
    dialog.querySelector('button').onclick = leave;
    dialog.addEventListener('cancel', event => event.preventDefault());
    document.body.append(dialog);
    dialog.showModal();
    window.setTimeout(leave, 4000);
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
    } catch { /* Retry on the next heartbeat; an outage is not a revocation. */ }
    finally { pending = false; }
  }
  const start = () => {
    if (new URLSearchParams(window.location.search).get('access_removed') === '1') showRemoved();
    else check();
    window.setInterval(check, 5000);
    window.addEventListener('focus', check);
    document.addEventListener('visibilitychange', () => {if (!document.hidden) check();});
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, {once:true});
  else start();
})();
