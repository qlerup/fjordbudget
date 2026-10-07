'use strict';
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
const svg = (name, cls='') => `<svg class="${cls}" aria-hidden="true"><use href="#icon-${name}"/></svg>`;
const csrf = document.querySelector('meta[name="csrf-token"]').content;
let config, dashboard, page=1, pages=1, view='overview', source=localStorage.getItem('fjordbudget-source') || 'demo';
let dashboardGeneration=0, transactionsGeneration=0, toastTimer, searchTimer, syncPolling=false, lastSyncCompletion=null;
if (!['demo','live'].includes(source)) source='demo';
const money = (value, currency=$('currency').value || 'DKK', decimals=0) => new Intl.NumberFormat('da-DK', {style:'currency', currency, minimumFractionDigits:decimals, maximumFractionDigits:decimals}).format(value / 100);
const monthName = month => new Intl.DateTimeFormat('da-DK', {month:'long', year:'numeric'}).format(new Date(month+'-15T12:00:00'));
const dateName = date => new Intl.DateTimeFormat('da-DK', {day:'numeric', month:'short'}).format(new Date(date+'T12:00:00'));
function transactionState(t) {
  if (t.amount >= 0) return {className:'', title:''};
  const hasMerchant=!!String(t.merchant || '').trim();
  const hasCategory=!!String(t.category || '').trim();
  const requiresMerchant=!(t.requires_merchant===0 || t.requires_merchant===false);
  if (hasCategory && !requiresMerchant) {
    return {className:'transaction-complete', title:'Kategorien kræver ikke forhandler'};
  }
  const completed=(hasMerchant?1:0)+(hasCategory?1:0);
  return completed===2
    ? {className:'transaction-complete', title:'Forhandler og kategori er på plads'}
    : completed===1
      ? {className:'transaction-partial', title:'Mangler enten forhandler eller kategori'}
      : {className:'transaction-incomplete', title:'Mangler både forhandler og kategori'};
}

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
  $('month').hidden=next==='savings' || next==='merchants';
  const titles={savings:['Dine drømme, dine mål.','Planlæg det, du vil spare op til.','Opsparingsmål'],merchants:['Dine forhandlere.','Se og administrer de forhandlere og banktekster, FjordBudget har lært.','Forhandlere'],categories:['Dine kategorier.','Tilpas kategorier til din økonomi.','Kategorier'],overview:['Din økonomi, samlet.','Alle dine konti. Ét enkelt overblik.','Overblik'], accounts:['Alle konti. Helt enkelt.','Se din saldo, og gå på opdagelse i dine posteringer.','Mine konti'], transactions:['De små tal fortæller.','Find og kategorisér dine bogførte posteringer.','Posteringer'], budget:['Plads til dine planer.','Sæt et budget, der passer til din hverdag.','Mit budget']};
  $('pageTitle').textContent=titles[next][0]; $('pageSubtitle').textContent=titles[next][1]; $('breadcrumb').textContent=titles[next][2];
  document.querySelectorAll('.nav-item[data-view]').forEach(b=>{b.classList.toggle('active',b.dataset.view===next); b.setAttribute('aria-current',b.dataset.view===next?'page':'false');});
  document.querySelectorAll('[data-section]').forEach(el=>{ const section=el.dataset.section; el.hidden=next==='overview'? ['budget','categories','savings','merchants','transactions'].includes(section) : next==='budget'?!['summary','budget'].includes(section):section!==next; });
  $('manageAccountsButton').hidden=source==='demo' || next!=='accounts';
}
function switchSource(next) {
  source=config?.has_bank_connections?'live':next; localStorage.setItem('fjordbudget-source',source);
  document.querySelector('.mode-switch').hidden=!!config?.has_bank_connections;
  document.querySelectorAll('[data-source]').forEach(b=>{b.classList.toggle('selected',b.dataset.source===source); b.setAttribute('aria-pressed',String(b.dataset.source===source));});
  $('demoNotice').hidden=source!=='demo';
  $('syncButton').hidden=source==='demo';
  $('manageAccountsButton').hidden=source==='demo' || view!=='accounts';
  $('lastSyncText').hidden=source==='demo';
  $('syncMessage').textContent='';
  $('accountFilter').value=''; $('search').value=''; $('categoryFilter').value=''; page=1;
  return refresh();
}
function renderAccounts() {
  const accounts=dashboard.accounts;
  $('accountCount').textContent=accounts.length; $('accountsLabel').textContent=accounts.length;
  $('accounts').innerHTML=accounts.length?accounts.map(a=>`<div class="account-tile"><button class="account-card" data-account="${esc(a.id)}" data-currency="${esc(a.currency)}"><div class="account-top"><span class="account-bank-icon">${svg('bank')}</span><span class="account-name">${esc(a.name)}<small>${esc(a.bank)}${source==='demo'?' · Demokonto':''}</small></span>${svg('chevron','chevron')}</div><div class="account-bottom"><span class="account-balance">${a.balance===null?'Afventer saldo':esc(money(a.balance,a.currency,2))}</span><span class="account-number">${a.last4?'•• '+esc(a.last4):esc(a.currency)}</span></div></button><button class="text-button rename-account" data-rename="${esc(a.id)}" aria-label="Navngiv konto ${esc(a.name)}">Navngiv konto</button></div>`).join(''):(source==='live' && dashboard.hidden_account_count?`<div class="empty-state">${svg('bank')}<h3>Ingen konti er valgt</h3><p>Dine bankkonti er gemt, men ingen er med i overblikket lige nu.</p><button class="button primary" id="emptyManageAccounts" type="button">Administrer konti</button></div>`:`<div class="empty-state">${svg('bank')}<h3>Din første konto starter her</h3><p>Forbind din bank for at se dine egne konti og saldi.</p><button class="button primary" data-connect>Forbind bank</button></div>`);
  const previous=$('accountFilter').value;
  $('accountFilter').innerHTML='<option value="">Alle konti</option>'+accounts.filter(a=>a.currency===$('currency').value).map(a=>`<option value="${esc(a.id)}">${esc(a.name)}</option>`).join('');
  if ([...$('accountFilter').options].some(o=>o.value===previous)) $('accountFilter').value=previous;
  const dates=accounts.map(a=>a.synced_at).filter(Boolean).sort();
  if(source==='live'){
    if(dates.length){
      const synced=new Date(dates[dates.length-1]);
      const syncDate=new Intl.DateTimeFormat('da-DK',{day:'numeric',month:'short',year:'numeric'}).format(synced);
      const syncTime=new Intl.DateTimeFormat('da-DK',{hour:'2-digit',minute:'2-digit'}).format(synced);
      $('lastSyncText').textContent=`Senest opdateret: ${syncDate} kl. ${syncTime}`;
      $('lastSyncText').hidden=false;
    }else{
      $('lastSyncText').textContent='Ikke opdateret endnu';
      $('lastSyncText').hidden=false;
    }
  }
}
function budgetRows(categories, editable=false) {
  return categories.map(c=>`<div class="budget-row"><span class="budget-category"><i class="category-dot" style="background:${c.color}"></i>${esc(c.name)}${editable?`<button type="button" class="text-button" data-delete-budget-category="${esc(c.name)}" aria-label="Fjern budgetkategori ${esc(c.name)}">Fjern</button>`:''}</span><span class="budget-value"><b>${esc(money(c.spent))}</b> / ${c.budget?esc(money(c.budget)):'Intet budget'}</span><div class="progress"><div class="progress-fill ${c.budget&&c.spent>c.budget?'over':''}" style="width:${c.budget?Math.min(100,c.spent/c.budget*100):0}%"></div></div></div>`).join('');
}
function renderDashboard() {
  const d=dashboard;
  $('balance').textContent=money(d.balance);
  $('balanceNote').textContent=d.missing_balances?`${d.missing_balances} saldi afventer hentning`:`På tværs af konti i ${$('currency').value}`;
  $('income').textContent=money(d.income); $('expenses').textContent=money(d.expenses);
  $('remaining').textContent=d.budget?money(d.budget-d.budget_spent):'Intet budget';
  $('remaining').classList.toggle('negative',!!d.budget && d.budget_spent>d.budget);
  $('remainingNote').textContent=d.budget?'Af '+money(d.budget)+' planlagt':'Sæt dit første månedsbudget';
  const incomplete=d.incomplete_transactions || 0;
  $('incompleteTransactionsCount').textContent=incomplete;
  $('incompleteTransactionsText').textContent=incomplete===0?'Alt er udfyldt denne måned':incomplete===1?'1 postering mangler oplysninger':`${incomplete} posteringer mangler oplysninger`;
  $('incompleteTransactionsCard').classList.toggle('complete',incomplete===0);
  $('budgetProgress').innerHTML=`<div class="budget-progress-label"><strong>${esc(money(d.budget_spent))} brugt</strong><span>${d.budget?esc(money(d.budget))+' i budget':'Intet budget endnu'}</span></div><div class="progress"><div class="progress-fill ${d.budget&&d.budget_spent>d.budget?'over':''}" style="width:${d.budget?Math.min(100,d.budget_spent/d.budget*100):0}%"></div></div>`;
  $('budgetPreview').innerHTML=budgetRows(d.categories.slice(0,3)); $('budgetFull').innerHTML=d.categories.length?budgetRows(d.categories,true):'<p class="empty-state">Tilføj din første budgetkategori ovenfor.</p>';
  const max=Math.max(1,...d.history.flatMap(h=>[h.income,h.expenses]));
  $('cashflowChart').innerHTML=d.history.length?d.history.map(h=>`<div class="chart-group"><div class="bars"><div class="bar" style="height:${h.income/max*100}%" title="Indtægter: ${esc(money(h.income))}"></div><div class="bar expenses" style="height:${h.expenses/max*100}%" title="Udgifter: ${esc(money(h.expenses))}"></div></div><span class="chart-label">${esc(new Intl.DateTimeFormat('da-DK',{month:'short'}).format(new Date(h.month+'-15T12:00:00')))}</span></div>`).join(''):'<div class="empty-state"><p>Dit overblik vokser med dine posteringer.</p></div>';
  $('cashflowChart').setAttribute('role','img'); $('cashflowChart').setAttribute('aria-label',d.history.map(h=>`${monthName(h.month)}: indtægter ${money(h.income)}, udgifter ${money(h.expenses)}`).join('. ')||'Ingen posteringer');
  $('netCashflow').textContent=(d.income-d.expenses>=0?'+':'')+money(d.income-d.expenses)+' denne måned';
  window.renderFeaturedGoal?.(d.featured_goal);
  renderAccounts();
}
function transactionRow(t) {
  const requiresMerchant=!(t.requires_merchant===0 || t.requires_merchant===false);
  const display=requiresMerchant && t.merchant ? t.merchant : t.description;
  const detail=requiresMerchant ? (t.merchant ? t.description : (t.amount>=0?'Indgående':'Ukendt forhandler')) : '';
  const subtitle=detail ? dateName(t.booked_on)+' · '+detail : dateName(t.booked_on);
  const state=transactionState(t);
  const categoryControl=`<button type="button" class="category-picker-button ${t.category?'confirmed':''}" data-edit-category="${t.id}" data-category="${esc(t.category)}" data-description="${esc(t.description)}" aria-label="Kategori for ${esc(t.description)}"><span>${esc(t.category || 'Vælg kategori')}</span><svg aria-hidden="true"><use href="#icon-chevron"/></svg></button>`;
  const merchantControl=requiresMerchant?`<button type="button" class="merchant-picker-button ${t.merchant?'confirmed':''}" data-edit-merchant="${t.id}" data-description="${esc(t.description)}" data-merchant="${esc(t.merchant || '')}" aria-label="Forhandler for ${esc(t.description)}"><span>${esc(t.merchant || 'Vælg forhandler')}</span><svg aria-hidden="true"><use href="#icon-chevron"/></svg></button>`:'';
  return `<tr class="${state.className}" title="${esc(state.title)}"><td><div class="merchant"><span class="merchant-logo">${esc(display.slice(0,1).toUpperCase())}</span><span><span class="merchant-name" title="${esc(t.description)}">${esc(display)}</span><span class="merchant-kind">${esc(subtitle)}</span></span></div></td><td>${esc(t.account)}</td><td class="category-control-cell">${categoryControl}</td><td class="merchant-control-cell">${merchantControl}</td><td>${esc(dateName(t.booked_on))}</td><td class="amount-cell ${t.amount>0?'positive':t.amount<0?'negative':''}">${t.amount>0?'+':''}${esc(money(t.amount,t.currency,2))}</td></tr>`;
}
async function loadTransactions() {
  const generation=++transactionsGeneration;
  const data=await api('/api/transactions?'+query({page,account:$('accountFilter').value,q:$('search').value,category:$('categoryFilter').value}));
  if(generation!==transactionsGeneration)return;
  pages=data.pages;
  $('transactionRows').innerHTML=data.items.map(transactionRow).join('');
  $('emptyTransactions').hidden=!!data.items.length;
  $('transactionCount').textContent=data.total?`${(page-1)*30+1}–${Math.min(page*30,data.total)} af ${data.total} posteringer`:'0 posteringer';
  $('pageLabel').textContent=`${page} / ${pages}`;
  $('previousPage').disabled=page<=1; $('nextPage').disabled=page>=pages;
  $('transactionSubtitle').textContent=monthName($('month').value)+' · '+$('currency').value;
}
async function loadIncompleteTransactions() {
  showError('incompleteTransactionsError','');
  const data=await api('/api/transactions?'+query({missing:'1',per_page:'200'}));
  $('incompleteTransactionRows').innerHTML=data.items.map(transactionRow).join('');
  $('incompleteTransactionsEmpty').hidden=!!data.items.length;
  $('incompleteTransactionsModalCount').textContent=data.total===0?'0 posteringer mangler oplysninger':
    (data.total===1?'1 postering mangler oplysninger':`${data.total} posteringer mangler oplysninger`) +
    (data.total>data.items.length?` · viser de første ${data.items.length}`:'');
}
async function openIncompleteTransactions() {
  try{
    await loadIncompleteTransactions();
    $('incompleteTransactionsDialog').showModal();
  }catch(error){showError('loadError',error.message);}
}
async function refresh() {
  const generation=++dashboardGeneration;
  const requestedSource=source;
  transactionsGeneration++;
  showError('loadError','');
  try {
    const result=await api('/api/dashboard?'+query());
    if(generation!==dashboardGeneration)return;
    config.has_bank_connections=!!result.has_bank_connections;
    document.querySelector('.mode-switch').hidden=config.has_bank_connections;
    if(config.has_bank_connections && source==='demo')return await switchSource('live');
    dashboard=result; dashboardSource=requestedSource; renderDashboard();
    await loadTransactions();
    if(generation!==dashboardGeneration)return;
    $('loading').hidden=true; $('appContent').hidden=false;
    setView(view);
    if(view==='savings')await loadSavings();
    if(view==='merchants')await window.loadMerchantLibrary?.();
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
async function openBudgetSuggestion(){
  const button=$('suggestBudget');
  button.disabled=true;showError('budgetCategoryError','');
  try{
    const scope=query().toString();
    const result=await api('/api/budgets/suggestion?'+scope);
    if(!result.months_analyzed){
      showError('budgetCategoryError','Der er ingen historiske posteringer at lave et budgetforslag ud fra.');
      return;
    }
    $('budgetSuggestionForm').dataset.scope=scope;
    const start=new Intl.DateTimeFormat('da-DK',{dateStyle:'long'}).format(new Date(result.period_start+'T12:00:00'));
    const end=new Intl.DateTimeFormat('da-DK',{dateStyle:'long'}).format(new Date(result.period_end+'T12:00:00'));
    const coverage=result.months_analyzed===12?'12 hele måneder':`${result.months_analyzed} måneder med data`;
    $('budgetSuggestionIntro').textContent=`Baseret på ${coverage} fra ${start} til ${end}. Forslaget gælder ${monthName(result.month)}.`;
    $('budgetSuggestionFields').innerHTML=result.items.map(item=>{
      const previous=item.current_budget?` · nu ${money(item.current_budget,result.currency,0)}`:'';
      return `<label class="budget-field"><span><span class="budget-category"><i class="category-dot" style="background:${item.color}"></i>${esc(item.name)}</span><small class="muted">Gns. ${esc(money(item.monthly_average,result.currency,0))}/md.${esc(previous)}</small></span><input name="${esc(item.name)}" aria-label="Budgetforslag for ${esc(item.name)}" type="number" min="0" max="1000000000" step="0.01" required value="${item.suggested/100}"></label>`;
    }).join('');
    if(result.unmapped_monthly_average){
      $('budgetSuggestionUnmapped').textContent=`Der ligger desuden ca. ${money(result.unmapped_monthly_average,result.currency,0)}/md. i kategorier, som ikke er tilknyttet en budgetkategori. Det beløb er ikke med i forslaget.`;
      $('budgetSuggestionUnmapped').hidden=false;
    }else{
      $('budgetSuggestionUnmapped').textContent='';
      $('budgetSuggestionUnmapped').hidden=true;
    }
    showError('budgetSuggestionError','');
    $('budgetSuggestionDialog').showModal();
  }catch(error){showError('budgetCategoryError',error.message);}
  finally{button.disabled=false;}
}
let accountVisibilityAccounts=[];
async function openAccountVisibility(pending=false){
  const result=await api('/api/accounts/manage'+(pending?'?pending=1':''));
  accountVisibilityAccounts=result.items;
  if(pending && !accountVisibilityAccounts.length)return false;
  $('accountVisibilityForm').dataset.pending=String(pending);
  $('accountVisibilityTitle').textContent=pending?'Hvilke nye konti skal være med?':'Administrer konti';
  $('accountVisibilityIntro').textContent=pending
    ?'Vælg de konti, FjordBudget skal bruge. De fravalgte gemmes, men holdes ude af saldo, posteringer, analyse og mål.'
    :'Slå konti til eller fra når som helst. Historikken slettes ikke, når en konto skjules.';
  $('accountVisibilityFields').innerHTML=accountVisibilityAccounts.length?accountVisibilityAccounts.map((a,i)=>{
    const checked=pending?true:!!a.included;
    const status=a.connected?'Forbundet':'Kun gemt historik';
    return `<label class="account-visibility-row" for="visibleAccount${i}"><input id="visibleAccount${i}" type="checkbox" data-account-visible="${esc(a.id)}" ${checked?'checked':''}><span><strong>${esc(a.name)}</strong><small>${esc(a.bank)} · ${a.last4?'•• '+esc(a.last4)+' · ':''}${esc(a.currency)} · ${status}</small></span><i class="visibility-switch" aria-hidden="true"></i></label>`;
  }).join(''):'<p class="empty-state">Der er ingen konti at administrere endnu.</p>';
  showError('accountVisibilityError','');
  $('accountVisibilityDialog').showModal();
  return true;
}
let accountSetupDismissed=false, dashboardSource=null;
function openAccountSetup(){
  if(source!=='live' || dashboardSource!=='live' || syncPolling || accountSetupDismissed || document.querySelector('dialog[open]'))return;
  const accounts=(dashboard?.accounts || []).filter(a=>!a.custom_name);
  if(!accounts.length)return;
  $('accountSetupFields').innerHTML=accounts.map((a,i)=>`<div class="account-setup-row">
    <div class="account-setup-summary"><div><strong>${esc(a.bank)}</strong><small>${esc(a.name)} · ${a.last4?'•• '+esc(a.last4):esc(a.currency)}</small></div>
    <strong>${a.balance===null?'Saldo ikke hentet':esc(money(a.balance,a.currency,2))}</strong></div>
    <label for="setupAccount${i}">Navn på konto ${i+1}</label>
    <input id="setupAccount${i}" name="${esc(a.id)}" maxlength="100" required placeholder="Fx Kostkonto eller Budgetkonto" autocomplete="off"></div>`).join('');
  showError('accountSetupError','');$('accountSetupDialog').showModal();
}
function historyAgeLabel(earliest){
  if(!earliest)return '';
  const start=new Date(earliest+'T12:00:00'), today=new Date();
  let months=(today.getFullYear()-start.getFullYear())*12+(today.getMonth()-start.getMonth());
  if(today.getDate()<start.getDate())months=Math.max(0,months-1);
  if(months<1){
    const days=Math.max(0,Math.floor((today-start)/86400000));
    return days===1?'ca. 1 dag':`ca. ${days} dage`;
  }
  const years=Math.floor(months/12), rest=months%12, parts=[];
  if(years)parts.push(years===1?'ca. 1 år':`ca. ${years} år`);
  if(rest)parts.push(rest===1?'1 måned':`${rest} måneder`);
  return parts.join(' og ');
}
function showHistoryImport(history){
  if(!history)return false;
  if(history.earliest_date){
    const oldest=new Intl.DateTimeFormat('da-DK',{dateStyle:'long'}).format(new Date(history.earliest_date+'T12:00:00'));
    $('historyImportText').textContent=`Banken gjorde det muligt at hente posteringer tilbage til ${oldest}.`;
    const age=historyAgeLabel(history.earliest_date);
    $('historyImportDetail').textContent=(age?`Det svarer til ${age} historik. `:'')+`${history.transactions} bogførte posteringer blev hentet fra ${history.accounts} ${history.accounts===1?'konto':'konti'}.`;
  }else{
    $('historyImportText').textContent='Banken returnerede ingen historiske posteringer i den første hentning.';
    $('historyImportDetail').textContent='FjordBudget forsøgte at hente den længst mulige historik. Fremtidige synkroniseringer henter de seneste 90 dage.';
  }
  $('historyImportDialog').showModal();
  return true;
}

async function pollSync() {
  if(syncPolling)return;
  syncPolling=true;
  const tick=async()=>{
    try {
      const result=await api('/api/sync');
      if(result.completed_at)lastSyncCompletion=result.completed_at;
      $('syncButton').disabled=result.running;
      if(source==='live') $('syncMessage').textContent=result.message;
      if(result.running){setTimeout(tick,1500);return;}
      syncPolling=false;
      if(result.message){toast(result.message);await refresh(); if(source==='live')$('syncMessage').textContent=result.message;}
      if(!showHistoryImport(result.history))openAccountSetup();
    }catch(error){syncPolling=false;$('syncButton').disabled=false;showError('loadError',error.message);}
  };
  await tick();
}
document.addEventListener('click',async event=>{
  if(event.target.closest('#incompleteTransactionsCard')){await openIncompleteTransactions();return;}
  if(event.target.closest('#viewAllTransactions')){
    $('incompleteTransactionsDialog').close();
    setView('transactions');
    await loadTransactions();
    window.scrollTo({top:0,behavior:'smooth'});
    return;
  }
  const connect=event.target.closest('[data-connect]'); if(connect){await openBank();return;}
  const close=event.target.closest('[data-close]'); if(close){$(close.dataset.close).close();return;}
  const nav=event.target.closest('[data-view]'); if(nav){
    if(nav.dataset.view==='categories'){try{await loadCategories();}catch(error){showError('loadError',error.message);}}
    setView(nav.dataset.view);
    if(nav.dataset.view==='savings')await loadSavings();
    if(nav.dataset.view==='merchants'){try{await window.loadMerchantLibrary?.();}catch(error){showError('merchantLibraryError',error.message);}}
    window.scrollTo({top:0,behavior:'smooth'});return;
  }
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
  if(event.target.closest('#manageAccountsButton') || event.target.closest('#emptyManageAccounts')){await openAccountVisibility(false);return;}
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
$('budgetForm').addEventListener('submit',async event=>{
  event.preventDefault(); const button=event.submitter;button.disabled=true;
  try{await api('/api/budgets?'+query(),{method:'PUT',body:JSON.stringify({amounts:Object.fromEntries(new FormData(event.target))})});$('budgetDialog').close();toast('Dit budget er gemt');await refresh();}
  catch(error){showError('budgetError',error.message);}finally{button.disabled=false;}
});
$('suggestBudget').addEventListener('click',openBudgetSuggestion);
$('budgetSuggestionForm').addEventListener('submit',async event=>{
  event.preventDefault();
  const form=event.currentTarget, button=event.submitter;
  button.disabled=true;showError('budgetSuggestionError','');
  try{
    await api('/api/budgets?'+form.dataset.scope,{method:'PUT',body:JSON.stringify({amounts:Object.fromEntries(new FormData(form))})});
    $('budgetSuggestionDialog').close();
    toast('Budgetforslaget er gemt');
    await refresh();
  }catch(error){showError('budgetSuggestionError',error.message);}
  finally{button.disabled=false;}
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
      const messages={connected:'Banken er forbundet. Henter konti, saldi og posteringer …',cancelled:'Bankforbindelsen blev annulleret.',invalid:'Godkendelsen er udløbet eller åbnet i en anden browser. Prøv igen.',failed:'Bankforbindelsen kunne ikke gemmes. Kontroller opsætningen og prøv igen.'};
      if(bankResult==='connected')source='live';
      toast(messages[bankResult]||'Bankens godkendelse er afsluttet.');
    }
    await switchSource(source);
    const status=await api('/api/sync');
    lastSyncCompletion=status.completed_at || null;
    if(status.running)await pollSync();
    else if(bankResult==='connected'){
      const needsSelection=await openAccountVisibility(true);
      if(!needsSelection){
        try{await api('/api/sync',{method:'POST',body:'{}'});await pollSync();}
        catch(error){showError('loadError',error.message);openAccountSetup();}
      }
    }else openAccountSetup();
  }catch(error){$('loading').hidden=true;showError('loadError',error.message);}
}
if($('logoutButton'))$('logoutButton').onclick=async()=>{await api('/logout',{method:'POST',body:'{}'});window.location.assign('/login');};
init();

async function watchAutomaticSync(){
  if(!config || syncPolling || source!=='live')return;
  try{
    const status=await api('/api/sync');
    if(status.running){await pollSync();return;}
    if(status.completed_at && status.completed_at!==lastSyncCompletion){
      lastSyncCompletion=status.completed_at;
      await refresh();
      if(status.error){
        $('syncMessage').textContent=status.message || 'Den automatiske banksynkronisering fejlede.';
      }else if(status.message){
        toast(status.message);
        showHistoryImport(status.history);
      }
    }
  }catch{}
}
setInterval(watchAutomaticSync,30000);

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

$('accountVisibilityForm').addEventListener('submit',async event=>{
  event.preventDefault();
  const form=event.currentTarget, button=event.submitter;
  button.disabled=true;showError('accountVisibilityError','');
  try{
    const included={};
    form.querySelectorAll('[data-account-visible]').forEach(input=>{included[input.dataset.accountVisible]=input.checked;});
    if(!Object.keys(included).length){showError('accountVisibilityError','Der er ingen konti at gemme.');return;}
    const result=await api('/api/accounts/manage',{method:'PUT',body:JSON.stringify({included})});
    const wasPending=form.dataset.pending==='true';
    $('accountVisibilityDialog').close();
    await refresh();
    toast(result.active?`${result.active} konti er med i FjordBudget`:'Alle konti er skjult');
    if(wasPending && result.active){
      try{await api('/api/sync',{method:'POST',body:'{}'});await pollSync();}
      catch(error){showError('loadError',error.message);openAccountSetup();}
    }else if(wasPending)openAccountSetup();
  }catch(error){showError('accountVisibilityError',error.message);}
  finally{button.disabled=false;}
});
$('historyImportDialog').addEventListener('close',()=>openAccountSetup());
$('accountSetupDialog').addEventListener('close',()=>{accountSetupDismissed=true;});
$('accountSetupForm').addEventListener('submit',async event=>{
  event.preventDefault();const button=$('saveAccountSetup');button.disabled=true;
  showError('accountSetupError','');
  try{
    await api('/api/accounts/setup',{method:'PUT',body:JSON.stringify({names:Object.fromEntries(new FormData(event.currentTarget))})});
    await refresh();$('accountSetupDialog').close();toast('Dine konti er klar');
  }catch(error){showError('accountSetupError',error.message);}
  finally{button.disabled=false;}
});

let editableCategories=[];
async function loadCategories(){
  const result=await api('/api/categories');editableCategories=result.items;
  config.categories=result.items.map(c=>c.name);config.colors=result.items.map(c=>c.color);
  const selected=$('categoryFilter').value;
  $('categoryFilter').innerHTML='<option value="">Alle kategorier</option>'+config.categories.map(c=>`<option>${esc(c)}</option>`).join('');
  if(config.categories.includes(selected))$('categoryFilter').value=selected;
  $('categoryList').innerHTML=result.items.map(c=>`<div class="category-management-row"><span><i class="category-dot" style="background:${c.color}"></i>${esc(c.name)}</span><div class="category-settings"><label class="category-merchant-setting"><span>Kræver forhandler</span><input type="checkbox" data-merchant-required="${esc(c.name)}" ${c.requires_merchant?'checked':''} aria-label="Kræver forhandler for ${esc(c.name)}"><i class="setting-switch" aria-hidden="true"></i></label><label class="category-budget-link">Budgetkategori<select data-budget-link="${esc(c.name)}" aria-label="Budgetkategori for ${esc(c.name)}"><option value="">Ingen budgetkategori</option>${result.budget_categories.map(b=>`<option value="${esc(b.name)}" ${c.budget_category===b.name?'selected':''}>${esc(b.name)}</option>`).join('')}</select></label></div>${c.protected?'<small class="muted">Fast kategori</small>':`<div class="category-row-actions"><button class="button quiet" type="button" data-rename-category="${esc(c.name)}" aria-label="Omdøb ${esc(c.name)}">Omdøb</button><button class="button quiet" type="button" data-delete-category="${esc(c.name)}" aria-label="Slet ${esc(c.name)}">Slet</button></div>`}</div>`).join('');
}
$('categoryCreateForm').addEventListener('submit',async event=>{
  event.preventDefault();const button=event.submitter;button.disabled=true;showError('categoryError','');
  try{await api('/api/categories',{method:'POST',body:JSON.stringify({name:$('newCategoryName').value})});$('newCategoryName').value='';await loadCategories();await refresh();toast('Kategori tilføjet');}
  catch(error){showError('categoryError',error.message);}finally{button.disabled=false;}
});
$('categoryList').addEventListener('click',event=>{
  const rename=event.target.closest('[data-rename-category]');
  if(rename){
    const name=rename.dataset.renameCategory;
    $('categoryRenameForm').dataset.category=name;
    $('categoryRenameInput').value=name;
    showError('categoryRenameError','');
    $('categoryRenameDialog').showModal();
    $('categoryRenameInput').focus();
    $('categoryRenameInput').select();
    return;
  }
  const button=event.target.closest('[data-delete-category]');if(!button)return;
  const name=button.dataset.deleteCategory;$('categoryDeleteForm').dataset.category=name;
  $('categoryDeleteText').textContent=`Alle posteringer og regler i “${name}” flyttes, før kategorien slettes. Dette gælder alle gemte data.`;
  $('categoryReplacement').innerHTML=editableCategories.filter(c=>![name,'Indkomst','Overførsler'].includes(c.name)).map(c=>`<option>${esc(c.name)}</option>`).join('');
  $('categoryReplacement').value='Andet';showError('categoryDeleteError','');$('categoryDeleteDialog').showModal();
});
$('categoryRenameForm').addEventListener('submit',async event=>{
  event.preventDefault();
  const form=event.currentTarget, button=event.submitter;
  button.disabled=true;showError('categoryRenameError','');
  try{
    const oldName=form.dataset.category;
    const newName=$('categoryRenameInput').value.trim();
    await api('/api/categories',{method:'PATCH',body:JSON.stringify({name:oldName,new_name:newName})});
    await loadCategories();
    await refresh();
    $('categoryRenameDialog').close();
    toast(`Kategorien er omdøbt til “${newName}”`);
  }catch(error){showError('categoryRenameError',error.message);}
  finally{button.disabled=false;}
});
$('categoryDeleteForm').addEventListener('submit',async event=>{
  event.preventDefault();const button=event.submitter;button.disabled=true;showError('categoryDeleteError','');
  try{await api('/api/categories',{method:'DELETE',body:JSON.stringify({name:event.currentTarget.dataset.category,replacement:$('categoryReplacement').value})});await loadCategories();await refresh();$('categoryDeleteDialog').close();toast('Kategorien er slettet, og data er flyttet');}
  catch(error){showError('categoryDeleteError',error.message);}finally{button.disabled=false;}
});

$('categoryList').addEventListener('change',async event=>{
  const merchantToggle=event.target.closest('[data-merchant-required]');
  if(merchantToggle){
    merchantToggle.disabled=true;showError('categoryError','');
    try{
      await api('/api/categories',{method:'PATCH',body:JSON.stringify({name:merchantToggle.dataset.merchantRequired,requires_merchant:merchantToggle.checked})});
      await refresh();
      toast(merchantToggle.checked?'Forhandler er nu påkrævet':'Forhandler er ikke længere påkrævet');
    }catch(error){showError('categoryError',error.message);await loadCategories();}
    finally{merchantToggle.disabled=false;}
    return;
  }
  const select=event.target.closest('[data-budget-link]');if(!select)return;
  select.disabled=true;showError('categoryError','');
  try{await api('/api/categories',{method:'PATCH',body:JSON.stringify({name:select.dataset.budgetLink,budget_category:select.value || null})});await refresh();toast('Budgettilknytning gemt');}
  catch(error){showError('categoryError',error.message);await loadCategories();}
  finally{select.disabled=false;}
});

$('budgetCategoryCreateForm').addEventListener('submit',async event=>{
  event.preventDefault();const button=event.submitter;button.disabled=true;showError('budgetCategoryError','');
  try{await api('/api/budget-categories',{method:'POST',body:JSON.stringify({name:$('newBudgetCategoryName').value})});$('newBudgetCategoryName').value='';await refresh();toast('Budgetkategori tilføjet');}
  catch(error){showError('budgetCategoryError',error.message);}finally{button.disabled=false;}
});
$('budgetFull').addEventListener('click',event=>{
  const button=event.target.closest('[data-delete-budget-category]');if(!button)return;
  const name=button.dataset.deleteBudgetCategory;
  $('budgetCategoryDeleteForm').dataset.category=name;
  $('budgetCategoryDeleteText').textContent=`Fjern “${name}”? Kategoriens budgetbeløb slettes for alle måneder og valutaer. Tilknytninger til posteringernes kategorier fjernes. Dine posteringer og deres kategorier bevares.`;
  showError('budgetCategoryDeleteError','');$('budgetCategoryDeleteDialog').showModal();
});
$('budgetCategoryDeleteForm').addEventListener('submit',async event=>{
  event.preventDefault();const button=event.submitter;button.disabled=true;showError('budgetCategoryDeleteError','');
  try{await api('/api/budget-categories',{method:'DELETE',body:JSON.stringify({name:event.currentTarget.dataset.category})});await refresh();$('budgetCategoryDeleteDialog').close();toast('Budgetkategori fjernet');}
  catch(error){showError('budgetCategoryDeleteError',error.message);}finally{button.disabled=false;}
});
