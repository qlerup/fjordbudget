'use strict';
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
const svg = (name, cls='') => `<svg class="${cls}" aria-hidden="true"><use href="#icon-${name}"/></svg>`;
const csrf = document.querySelector('meta[name="csrf-token"]').content;
let config, dashboard, page=1, pages=1, view='overview', source=localStorage.getItem('fjordbudget-source') || 'demo';
let dashboardGeneration=0, transactionsGeneration=0, toastTimer, searchTimer, syncPolling=false;
if (!['demo','live'].includes(source)) source='demo';
const money = (value, currency=$('currency').value || 'DKK', decimals=0) => new Intl.NumberFormat('da-DK', {style:'currency', currency, minimumFractionDigits:decimals, maximumFractionDigits:decimals}).format(value / 100);
const monthName = month => new Intl.DateTimeFormat('da-DK', {month:'long', year:'numeric'}).format(new Date(month+'-15T12:00:00'));
const dateName = date => new Intl.DateTimeFormat('da-DK', {day:'numeric', month:'short'}).format(new Date(date+'T12:00:00'));

async function api(url, options={}) {
  const response = await fetch(url, {...options, headers:{'Content-Type':'application/json', 'X-CSRF-Token':csrf, ...options.headers}});
  if(response.status===401){window.location.assign('/login');throw new Error('Log ind for at fortsætte.');}
  let data;
  try { data = await response.json(); }
  catch { throw new Error(`Serveren gav et ugyldigt svar (HTTP ${response.status}). Prøv igen om lidt.`); }
  if (!response.ok) throw new Error(data.error || 'Forespørgslen mislykkedes.');
  return data;
}
function query(extra={}) { return new URLSearchParams({source, month:$('month').value, currency:$('currency').value, ...extra}); }
function showError(id, message) { $(id).textContent=message; $(id).hidden=!message; }
function toast(message) { clearTimeout(toastTimer); $('toast').textContent=message; $('toast').hidden=false; toastTimer=setTimeout(()=>{$('toast').hidden=true;},5000); }
function setView(next) {
  view=next;
  const titles={overview:['Din økonomi, samlet.','Alle dine konti. Ét enkelt overblik.','Overblik'], accounts:['Alle konti. Helt enkelt.','Se din saldo, og gå på opdagelse i dine posteringer.','Mine konti'], transactions:['De små tal fortæller.','Find og kategorisér dine bogførte posteringer.','Posteringer'], budget:['Plads til dine planer.','Sæt et budget, der passer til din hverdag.','Mit budget']};
  $('pageTitle').textContent=titles[next][0]; $('pageSubtitle').textContent=titles[next][1]; $('breadcrumb').textContent=titles[next][2];
  document.querySelectorAll('.nav-item[data-view]').forEach(b=>{b.classList.toggle('active',b.dataset.view===next); b.setAttribute('aria-current',b.dataset.view===next?'page':'false');});
  document.querySelectorAll('[data-section]').forEach(el=>{ const section=el.dataset.section; el.hidden=next==='overview'? section==='budget' : next==='budget'?!['summary','budget'].includes(section):section!==next; });
}
function switchSource(next) {
  source=next; localStorage.setItem('fjordbudget-source',source);
  document.querySelectorAll('[data-source]').forEach(b=>{b.classList.toggle('selected',b.dataset.source===source); b.setAttribute('aria-pressed',String(b.dataset.source===source));});
  $('demoNotice').hidden=source!=='demo';
  $('syncButton').hidden=source==='demo';
  $('syncMessage').textContent='';
  $('accountFilter').value=''; $('search').value=''; $('categoryFilter').value=''; page=1;
  return refresh();
}
function renderAccounts() {
  const accounts=dashboard.accounts;
  $('accountCount').textContent=accounts.length; $('accountsLabel').textContent=accounts.length;
  $('accounts').innerHTML=accounts.length?accounts.map(a=>`<div class="account-tile"><button class="account-card" data-account="${esc(a.id)}" data-currency="${esc(a.currency)}"><div class="account-top"><span class="account-bank-icon">${svg('bank')}</span><span class="account-name">${esc(a.name)}<small>${esc(a.bank)}${source==='demo'?' · Demokonto':''}</small></span>${svg('chevron','chevron')}</div><div class="account-bottom"><span class="account-balance">${a.balance===null?'Afventer saldo':esc(money(a.balance,a.currency,2))}</span><span class="account-number">${a.last4?'•• '+esc(a.last4):esc(a.currency)}</span></div></button><button class="text-button rename-account" data-rename="${esc(a.id)}" aria-label="Navngiv konto ${esc(a.name)}">Navngiv konto</button></div>`).join(''):`<div class="empty-state">${svg('bank')}<h3>Din første konto starter her</h3><p>Forbind din bank for at se dine egne konti og saldi.</p><button class="button primary" data-connect>Forbind bank</button></div>`;
  const previous=$('accountFilter').value;
  $('accountFilter').innerHTML='<option value="">Alle konti</option>'+accounts.filter(a=>a.currency===$('currency').value).map(a=>`<option value="${esc(a.id)}">${esc(a.name)}</option>`).join('');
  if ([...$('accountFilter').options].some(o=>o.value===previous)) $('accountFilter').value=previous;
  const dates=accounts.map(a=>a.synced_at).filter(Boolean).sort();
  if(source==='live' && dates.length) $('syncMessage').textContent='Senest hentet: '+new Intl.DateTimeFormat('da-DK',{dateStyle:'short',timeStyle:'short'}).format(new Date(dates[dates.length-1]));
}
function budgetRows(categories) {
  return categories.map(c=>`<div class="budget-row"><span class="budget-category"><i class="category-dot" style="background:${c.color}"></i>${esc(c.name)}</span><span class="budget-value"><b>${esc(money(c.spent))}</b> / ${c.budget?esc(money(c.budget)):'Intet budget'}</span><div class="progress"><div class="progress-fill ${c.budget&&c.spent>c.budget?'over':''}" style="width:${c.budget?Math.min(100,c.spent/c.budget*100):0}%"></div></div></div>`).join('');
}
function renderDashboard() {
  const d=dashboard;
  $('balance').textContent=money(d.balance);
  $('balanceNote').textContent=d.missing_balances?`${d.missing_balances} saldi afventer hentning`:`På tværs af konti i ${$('currency').value}`;
  $('income').textContent=money(d.income); $('expenses').textContent=money(d.expenses);
  $('remaining').textContent=d.budget?money(d.budget-d.expenses):'Intet budget';
  $('remaining').classList.toggle('negative',!!d.budget && d.expenses>d.budget);
  $('remainingNote').textContent=d.budget?'Af '+money(d.budget)+' planlagt':'Sæt dit første månedsbudget';
  $('budgetProgress').innerHTML=`<div class="budget-progress-label"><strong>${esc(money(d.expenses))} brugt</strong><span>${d.budget?esc(money(d.budget))+' i budget':'Intet budget endnu'}</span></div><div class="progress"><div class="progress-fill ${d.budget&&d.expenses>d.budget?'over':''}" style="width:${d.budget?Math.min(100,d.expenses/d.budget*100):0}%"></div></div>`;
  $('budgetPreview').innerHTML=budgetRows(d.categories.slice(0,3)); $('budgetFull').innerHTML=budgetRows(d.categories);
  const max=Math.max(1,...d.history.flatMap(h=>[h.income,h.expenses]));
  $('cashflowChart').innerHTML=d.history.length?d.history.map(h=>`<div class="chart-group"><div class="bars"><div class="bar" style="height:${h.income/max*100}%" title="Indtægter: ${esc(money(h.income))}"></div><div class="bar expenses" style="height:${h.expenses/max*100}%" title="Udgifter: ${esc(money(h.expenses))}"></div></div><span class="chart-label">${esc(new Intl.DateTimeFormat('da-DK',{month:'short'}).format(new Date(h.month+'-15T12:00:00')))}</span></div>`).join(''):'<div class="empty-state"><p>Dit overblik vokser med dine posteringer.</p></div>';
  $('cashflowChart').setAttribute('role','img'); $('cashflowChart').setAttribute('aria-label',d.history.map(h=>`${monthName(h.month)}: indtægter ${money(h.income)}, udgifter ${money(h.expenses)}`).join('. ')||'Ingen posteringer');
  $('netCashflow').textContent=(d.income-d.expenses>=0?'+':'')+money(d.income-d.expenses)+' denne måned';
  renderAccounts();
}
async function loadTransactions() {
  const generation=++transactionsGeneration;
  const data=await api('/api/transactions?'+query({page,account:$('accountFilter').value,q:$('search').value,category:$('categoryFilter').value}));
  if(generation!==transactionsGeneration)return;
  pages=data.pages;
  $('transactionRows').innerHTML=data.items.map(t=>`<tr><td><div class="merchant"><span class="merchant-logo">${esc(t.description.slice(0,1).toUpperCase())}</span><span><span class="merchant-name" title="${esc(t.description)}">${esc(t.description)}</span><span class="merchant-kind">${esc(dateName(t.booked_on))} ? ${t.amount>=0?'Indbetaling':'Udbetaling'}</span></span></div></td><td>${esc(t.account)}</td><td><select class="category-select" data-transaction="${t.id}" aria-label="Kategori for ${esc(t.description)}">${config.categories.map(c=>`<option ${c===t.category?'selected':''}>${esc(c)}</option>`).join('')}</select></td><td>${esc(dateName(t.booked_on))}</td><td class="amount-cell ${t.amount>0?'positive':''}">${t.amount>0?'+':''}${esc(money(t.amount,t.currency,2))}</td></tr>`).join('');
  $('emptyTransactions').hidden=!!data.items.length;
  $('transactionCount').textContent=data.total?`${(page-1)*30+1}–${Math.min(page*30,data.total)} af ${data.total} posteringer`:'0 posteringer';
  $('pageLabel').textContent=`${page} / ${pages}`;
  $('previousPage').disabled=page<=1; $('nextPage').disabled=page>=pages;
  $('transactionSubtitle').textContent=monthName($('month').value)+' · '+$('currency').value;
}
async function refresh() {
  const generation=++dashboardGeneration;
  transactionsGeneration++;
  showError('loadError','');
  try {
    const result=await api('/api/dashboard?'+query());
    if(generation!==dashboardGeneration)return;
    dashboard=result; renderDashboard();
    await loadTransactions();
    if(generation!==dashboardGeneration)return;
    $('loading').hidden=true; $('appContent').hidden=false;
    setView(view);
  } catch(error) { if(generation===dashboardGeneration){$('loading').hidden=true;showError('loadError',error.message);} }
}
let bankChoices = [], bankActive = -1;
function closeBankDropdown() {
  $('bankDropdownPanel').hidden=true;
  $('bankDropdownButton').setAttribute('aria-expanded','false');
  $('bankSearch').setAttribute('aria-expanded','false');
  $('bankSearch').removeAttribute('aria-activedescendant');
}
function renderBankChoices() {
  const search=$('bankSearch').value.trim().toLocaleLowerCase('da');
  const choices=bankChoices.filter(bank=>bank.name.toLocaleLowerCase('da').includes(search));
  $('bankOptions').replaceChildren(); bankActive=-1;
  $('bankSearch').removeAttribute('aria-activedescendant');
  choices.forEach((bank,i)=>{
    const option=document.createElement('div'); option.className='bank-option'; option.id=`bank-option-${i}`;
    option.setAttribute('role','option'); option.setAttribute('aria-selected',String(bank.available && bank.name===$('bankSelect').value));
    option.setAttribute('aria-disabled',String(!bank.available));
    const name=document.createElement('span'); name.textContent=bank.name; option.append(name);
    const hint=document.createElement('small'); hint.textContent=bank.available ? (bank.name===$('bankSelect').value?'Valgt':'Danmark') : 'Ikke tilgængelig med denne opsætning'; option.append(hint);
    option.addEventListener('click',()=>{
      if(!bank.available)return;
      $('bankSelect').value=bank.name; $('bankSelectedName').textContent=bank.name;
      $('connectBank').disabled=false; closeBankDropdown(); $('bankDropdownButton').focus();
    });
    $('bankOptions').append(option);
  });
  $('bankSearchEmpty').hidden=choices.length>0;
}
function openBankDropdown() {
  $('bankDropdownPanel').hidden=false;
  $('bankDropdownButton').setAttribute('aria-expanded','true');
  $('bankSearch').setAttribute('aria-expanded','true');
  $('bankSearch').value=''; renderBankChoices(); $('bankSearch').focus();
}
$('bankDropdownButton').onclick=()=>$('bankDropdownPanel').hidden?openBankDropdown():closeBankDropdown();
$('bankDropdownButton').addEventListener('keydown',event=>{if(event.key==='ArrowDown'){event.preventDefault();openBankDropdown();}});
$('bankSearch').addEventListener('input',renderBankChoices);
$('bankDropdown').addEventListener('keydown',event=>{
  if(event.key==='Escape' && !$('bankDropdownPanel').hidden){event.preventDefault();event.stopPropagation();closeBankDropdown();$('bankDropdownButton').focus();return;}
  if(event.target!==$('bankSearch'))return;
  const options=Array.from($('bankOptions').children);
  if(['ArrowDown','ArrowUp','Home','End'].includes(event.key)){
    event.preventDefault(); if(!options.length)return;
    bankActive=event.key==='Home'?0:event.key==='End'?options.length-1:(bankActive+(event.key==='ArrowDown'?1:-1)+options.length)%options.length;
    options.forEach((option,i)=>option.classList.toggle('active',i===bankActive));
    $('bankSearch').setAttribute('aria-activedescendant',options[bankActive].id); options[bankActive].scrollIntoView({block:'nearest'});
  }else if(event.key==='Enter'){event.preventDefault();options[bankActive]?.click();}
});
$('bankDropdown').addEventListener('focusout',event=>{if(!$('bankDropdown').contains(event.relatedTarget))closeBankDropdown();});
$('bankOptions').addEventListener('mousedown',event=>event.preventDefault());
document.addEventListener('click',event=>{if(!$('bankDropdown').contains(event.target))closeBankDropdown();});
async function openBank() {
  $('bankDialog').showModal(); showError('bankError','');
  $('bankSetup').hidden=config.provider_configured; $('bankPicker').hidden=!config.provider_configured;
  $('editBankCredentials').hidden=!config.provider_configured;
  $('callbackUrl').textContent=config.callback_url;
  $('privacyUrl').textContent=config.privacy_url || location.origin+'/privacy';
  $('termsUrl').textContent=config.terms_url || location.origin+'/terms';
  $('connections').innerHTML='';
  try {
    if(config.provider_configured){
      closeBankDropdown(); $('bankSelectedName').textContent='Henter banker …'; $('bankDropdownButton').disabled=true;
      $('bankSelect').replaceChildren(); bankChoices=[]; $('bankAvailability').hidden=true; $('connectBank').disabled=true;
      const result=await api('/api/banks');
      const banks=result.banks.slice().sort((a,b)=>a.name.localeCompare(b.name,'da'));
      $('bankSelect').innerHTML=banks.map(b=>`<option value="${esc(b.name)}">${esc(b.name)}</option>`).join('');
      bankChoices=banks.map(bank=>({...bank,available:true}));
      if(!banks.some(bank=>bank.name.toLocaleLowerCase('da').includes('kreditbanken'))){
        bankChoices.push({name:'Kreditbanken',available:false}); $('bankAvailability').hidden=false;
      }
      bankChoices.sort((a,b)=>a.name.localeCompare(b.name,'da'));
      $('bankSelectedName').textContent=$('bankSelect').value || 'Ingen tilgængelige banker'; $('bankDropdownButton').disabled=false;
      $('connectBank').disabled=!result.banks.length;
      if(!result.banks.length) showError('bankError','Der er ingen danske banker tilgængelige for denne API-applikation. Kontroller miljø og adgang hos Enable Banking.');
    }
    const live=await api('/api/dashboard?'+query({source:'live'}));
    $('connections').innerHTML=live.connections.map(c=>`<div class="connection"><strong>${esc(c.bank)}</strong><p>Læseadgang indtil ${esc(new Intl.DateTimeFormat('da-DK',{dateStyle:'long'}).format(new Date(c.valid_until)))}</p><button class="disconnect" data-disconnect="${esc(c.id)}">Afbryd bankadgang</button></div>`).join('');
  } catch(error){if($('bankDropdownButton').disabled)$('bankSelectedName').textContent='Banklisten kunne ikke hentes';showError('bankError',error.message);}
}
function openBudget() {
  if(!dashboard)return;
  $('budgetDialogSubtitle').textContent=monthName($('month').value)+' · '+$('currency').value+(source==='demo'?' · Demodata':'');
  $('budgetFields').innerHTML=dashboard.categories.map((c,i)=>`<label class="budget-field"><span class="budget-category"><i class="category-dot" style="background:${c.color}"></i>${esc(c.name)}</span><input name="${esc(c.name)}" aria-label="Budget for ${esc(c.name)}" type="number" min="0" max="1000000000" step="0.01" required value="${c.budget/100}"></label>`).join('');
  showError('budgetError',''); $('budgetDialog').showModal();
}
async function pollSync() {
  if(syncPolling)return;
  syncPolling=true;
  const tick=async()=>{
    try {
      const result=await api('/api/sync');
      $('syncButton').disabled=result.running;
      if(source==='live') $('syncMessage').textContent=result.message;
      if(result.running){setTimeout(tick,1500);return;}
      syncPolling=false;
      if(result.message){toast(result.message);await refresh(); if(source==='live')$('syncMessage').textContent=result.message;}
    }catch(error){syncPolling=false;$('syncButton').disabled=false;showError('loadError',error.message);}
  };
  await tick();
}
document.addEventListener('click',async event=>{
  const connect=event.target.closest('[data-connect]'); if(connect){await openBank();return;}
  const close=event.target.closest('[data-close]'); if(close){$(close.dataset.close).close();return;}
  const nav=event.target.closest('[data-view]'); if(nav){setView(nav.dataset.view);window.scrollTo({top:0,behavior:'smooth'});return;}
  const mode=event.target.closest('[data-source]'); if(mode){await switchSource(mode.dataset.source);return;}
  const rename=event.target.closest('[data-rename]'); if(rename){
    const item=dashboard.accounts.find(a=>a.id===rename.dataset.rename);
    if(!item)return;
    $('accountNameForm').dataset.account=item.id;
    $('accountNameInput').value=item.custom_name ?? item.name;
    $('accountNameHint').textContent=`${item.bank} · •• ${item.last4}`;
    showError('accountNameError','');$('accountNameDialog').showModal();
    $('accountNameInput').focus();$('accountNameInput').select();return;
  }
  const account=event.target.closest('[data-account]'); if(account){
    $('currency').value=account.dataset.currency;page=1;
    await refresh();$('accountFilter').value=account.dataset.account;setView('transactions');
    try{await loadTransactions();}catch(error){showError('loadError',error.message);}return;
  }
  if(event.target.closest('.edit-budget')){openBudget();return;}
  const disconnect=event.target.closest('[data-disconnect]');
  if(disconnect && confirm('Afbryd bankens læseadgang? Allerede hentede posteringer bliver gemt lokalt.')){
    disconnect.disabled=true;
    try{await api('/api/connections/'+encodeURIComponent(disconnect.dataset.disconnect),{method:'DELETE'});toast('Bankadgangen er afbrudt.');await openBankRefresh();await refresh();}
    catch(error){showError('bankError',error.message);disconnect.disabled=false;}
  }
});
async function openBankRefresh(){ $('bankDialog').close(); await openBank(); }
$('bankDialog').addEventListener('close', () => {closeBankDropdown();$('bankCredentialsForm').reset();});
$('editBankCredentials').onclick=()=>{
  $('bankSetup').hidden=false; $('bankPicker').hidden=true;
  $('editBankCredentials').hidden=true; $('bankAppId').focus();
};
$('bankCredentialsForm').addEventListener('submit', async event=>{
  event.preventDefault();
  const button=$('saveBankCredentials'); button.disabled=true; button.textContent='Gemmer …';
  showError('bankError','');
  try {
    await api('/api/bank/credentials',{method:'POST',body:JSON.stringify({app_id:$('bankAppId').value.trim(),private_key:$('bankPrivateKey').value})});
    $('bankCredentialsForm').reset(); config.provider_configured=true;
    toast('API-oplysninger gemt lokalt.'); await openBankRefresh();
  } catch(error){showError('bankError',error.message);}
  finally {button.disabled=false;button.textContent='Gem og fortsæt';}
});
['month','currency'].forEach(id=>$(id).addEventListener('change',()=>{if(!$('month').value)return;page=1;$('accountFilter').value='';refresh();}));
['accountFilter','categoryFilter'].forEach(id=>$(id).addEventListener('change',()=>{page=1;loadTransactions().catch(e=>showError('loadError',e.message));}));
$('search').addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{page=1;loadTransactions().catch(e=>showError('loadError',e.message));},250);});
$('previousPage').onclick=()=>{if(page>1){page--;loadTransactions().catch(e=>showError('loadError',e.message));}};
$('nextPage').onclick=()=>{if(page<pages){page++;loadTransactions().catch(e=>showError('loadError',e.message));}};
$('transactionRows').addEventListener('change',async event=>{
  const select=event.target.closest('[data-transaction]');if(!select)return;
  select.disabled=true;
  try{await api('/api/transactions/'+select.dataset.transaction,{method:'PATCH',body:JSON.stringify({category:select.value})});toast('Kategori gemt');await refresh();}
  catch(error){showError('loadError',error.message);select.disabled=false;}
});
$('budgetForm').addEventListener('submit',async event=>{
  event.preventDefault(); const button=event.submitter;button.disabled=true;
  try{await api('/api/budgets?'+query(),{method:'PUT',body:JSON.stringify({amounts:Object.fromEntries(new FormData(event.target))})});$('budgetDialog').close();toast('Dit budget er gemt');await refresh();}
  catch(error){showError('budgetError',error.message);}finally{button.disabled=false;}
});
$('connectBank').onclick=async()=>{
  $('connectBank').disabled=true;showError('bankError','');
  try{const result=await api('/api/bank/connect',{method:'POST',body:JSON.stringify({bank:$('bankSelect').value})});window.location.assign(result.url);}
  catch(error){showError('bankError',error.message);$('connectBank').disabled=false;}
};
$('syncButton').onclick=async()=>{
  $('syncButton').disabled=true;
  try{await api('/api/sync',{method:'POST',body:'{}'});await pollSync();}
  catch(error){$('syncButton').disabled=false;showError('loadError',error.message);}
};
async function init(){
  try {
    config=await api('/api/config');
    $('month').value=config.month;
    $('currency').innerHTML=config.currencies.map(c=>`<option ${c==='DKK'?'selected':''}>${c}</option>`).join('');
    $('categoryFilter').innerHTML='<option value="">Alle kategorier</option>'+config.categories.map(c=>`<option>${esc(c)}</option>`).join('');
    const bankResult=new URLSearchParams(location.search).get('bank_result');
    if(bankResult){
      history.replaceState({},'', '/');
      const messages={connected:'Banken er forbundet. Klik Opdater saldi for at hente posteringer.',cancelled:'Bankforbindelsen blev annulleret.',invalid:'Godkendelsen er udløbet eller åbnet i en anden browser. Prøv igen.',failed:'Bankforbindelsen kunne ikke gemmes. Kontroller opsætningen og prøv igen.'};
      if(bankResult==='connected')source='live';
      toast(messages[bankResult]||'Bankens godkendelse er afsluttet.');
    }
    await switchSource(source);
    const status=await api('/api/sync'); if(status.running)await pollSync();
  }catch(error){$('loading').hidden=true;showError('loadError',error.message);}
}
if($('logoutButton'))$('logoutButton').onclick=async()=>{await api('/logout',{method:'POST',body:'{}'});window.location.assign('/login');};
init();

$('accountNameForm').addEventListener('submit',async event=>{
  event.preventDefault();const button=$('saveAccountName');button.disabled=true;
  showError('accountNameError','');
  try{
    await api('/api/accounts/'+encodeURIComponent(event.currentTarget.dataset.account)+'/name',{
      method:'PUT',body:JSON.stringify({name:$('accountNameInput').value})});
    await refresh();$('accountNameDialog').close();toast('Kontonavnet er gemt');
  }catch(error){showError('accountNameError',error.message);}
  finally{button.disabled=false;}
});
