'use strict';
const transactionRows=$('transactionRows');
const incompleteTransactionRows=$('incompleteTransactionRows');
let merchantChoices=[], merchantChoicesSource='', merchantActive=-1, categoryActive=-1;

function normalizedSearch(value){return String(value || '').toLocaleLowerCase('da').trim();}
function merchantChoice(name){return merchantChoices.find(item=>normalizedSearch(item.name)===normalizedSearch(name));}
function syncMerchantFixed(name){const item=merchantChoice(name);if(item)$('merchantFixed').checked=item.adjustable===false;}

function renderChoiceList(containerId, emptyId, items, search, selected, activeIndex, formatter){
  const container=$(containerId), needle=normalizedSearch(search);
  const filtered=items.filter(item=>normalizedSearch(item.name).includes(needle));
  container.replaceChildren();
  filtered.forEach((item,index)=>{
    const button=document.createElement('button');
    button.type='button';
    button.className='choice-option'+(item.name===selected?' selected':'')+(index===activeIndex?' active':'');
    button.dataset.value=item.name;
    button.setAttribute('role','option');
    button.setAttribute('aria-selected',String(item.name===selected));
    const content=formatter(item);
    if(typeof content==='string')button.textContent=content;
    else button.append(...content);
    container.append(button);
  });
  $(emptyId).hidden=filtered.length>0;
  return filtered;
}

async function loadMerchantChoices(){
  const requested=source;
  if(merchantChoicesSource===requested && merchantChoices.length)return merchantChoices;
  const result=await api('/api/merchants?source='+encodeURIComponent(requested));
  if(requested!==source)return [];
  merchantChoices=result.items;
  merchantChoicesSource=requested;
  return merchantChoices;
}
function renderMerchantChoices(){
  merchantActive=-1;
  const selected=$('merchantForm').dataset.selected || '';
  renderChoiceList('merchantOptions','merchantEmpty',merchantChoices,$('merchantName').value,selected,merchantActive,item=>{
    const name=document.createElement('span');name.textContent=item.name;
    const meta=document.createElement('small');meta.textContent=(item.uses===1?'Brugt 1 gang':`Brugt ${item.uses} gange`)+(item.adjustable===false?' · Fast udgift':'');
    return [name,meta];
  });
}
function renderCategoryChoices(){
  const selected=$('categoryPickerForm').dataset.category || '';
  const items=(config?.categories || []).map(name=>({name}));
  const search=$('categorySearch').value.trim();
  const filtered=renderChoiceList('categoryOptions','categoryPickerEmpty',items,search,selected,categoryActive,item=>item.name);
  const exact=items.some(item=>normalizedSearch(item.name)===normalizedSearch(search));
  const canCreate=search.length>0 && search.length<=60 && !exact && !/[\u0000-\u001f\u007f]/.test(search);
  if(canCreate){
    const create=document.createElement('button');
    create.type='button';
    create.className='choice-create-option';
    create.dataset.createCategory=search;
    const plus=document.createElement('span');plus.className='choice-create-plus';plus.textContent='+';
    const label=document.createElement('span');label.textContent=`Opret “${search}”`;
    create.append(plus,label);
    $('categoryOptions').append(create);
  }
  $('categoryPickerEmpty').hidden=filtered.length>0 || canCreate;
}
function moveActive(inputId, optionsId, direction, kind){
  const options=Array.from($(optionsId).querySelectorAll('.choice-option'));
  if(!options.length)return;
  if(kind==='merchant')merchantActive=(merchantActive+direction+options.length)%options.length;
  else categoryActive=(categoryActive+direction+options.length)%options.length;
  const active=kind==='merchant'?merchantActive:categoryActive;
  options.forEach((option,index)=>option.classList.toggle('active',index===active));
  options[active].scrollIntoView({block:'nearest'});
  $(inputId).setAttribute('aria-activedescendant',options[active].id || '');
}
function chooseActive(optionsId, kind){
  const active=kind==='merchant'?merchantActive:categoryActive;
  const options=Array.from($(optionsId).querySelectorAll('.choice-option'));
  options[active]?.click();
}

async function handleTransactionEdit(event){
  const merchantButton=event.target.closest('[data-edit-merchant]');
  if(merchantButton){
    $('merchantForm').dataset.transaction=merchantButton.dataset.editMerchant;
    $('merchantForm').dataset.selected=merchantButton.dataset.merchant || '';
    $('merchantBankText').textContent=merchantButton.dataset.description || '';
    $('merchantName').value=merchantButton.dataset.merchant || '';
    $('merchantRemember').checked=true;
    $('merchantFixed').checked=false;
    showError('merchantError','');
    try{await loadMerchantChoices();syncMerchantFixed($('merchantName').value);}catch(error){merchantChoices=[];showError('merchantError','Kunne ikke hente eksisterende forhandlere. Du kan stadig skrive en ny.');}
    renderMerchantChoices();
    $('merchantDialog').showModal();
    $('merchantName').focus();
    return;
  }
  const categoryButton=event.target.closest('[data-edit-category]');
  if(categoryButton){
    $('categoryPickerForm').dataset.transaction=categoryButton.dataset.editCategory;
    $('categoryPickerForm').dataset.category=categoryButton.dataset.category || '';
    $('categoryPickerContext').textContent=categoryButton.dataset.description || '';
    $('categorySearch').value='';
    categoryActive=-1;
    showError('categoryPickerError','');
    renderCategoryChoices();
    $('categoryDialog').showModal();
    $('categorySearch').focus();
  }
}
transactionRows.addEventListener('click',handleTransactionEdit);
incompleteTransactionRows.addEventListener('click',handleTransactionEdit);

$('merchantName').addEventListener('input',()=>{renderMerchantChoices();syncMerchantFixed($('merchantName').value);});
$('merchantOptions').addEventListener('click',event=>{
  const option=event.target.closest('.choice-option');if(!option)return;
  $('merchantName').value=option.dataset.value;
  $('merchantForm').dataset.selected=option.dataset.value;
  syncMerchantFixed(option.dataset.value);
  renderMerchantChoices();
  $('merchantName').focus();
});
$('merchantName').addEventListener('keydown',event=>{
  if(event.key==='ArrowDown'){event.preventDefault();moveActive('merchantName','merchantOptions',1,'merchant');}
  else if(event.key==='ArrowUp'){event.preventDefault();moveActive('merchantName','merchantOptions',-1,'merchant');}
  else if(event.key==='Enter' && merchantActive>=0){event.preventDefault();chooseActive('merchantOptions','merchant');}
});
$('merchantForm').addEventListener('submit',async event=>{
  event.preventDefault();
  const form=event.currentTarget, button=event.submitter;
  button.disabled=true;showError('merchantError','');
  try{
    const merchant=$('merchantName').value.trim();
    const remember=$('merchantRemember').checked;
    const adjustable=!$('merchantFixed').checked;
    await api(`/api/transactions/${form.dataset.transaction}/merchant`,{
      method:'PATCH',body:JSON.stringify({merchant,remember,adjustable}),
    });
    $('merchantDialog').close();
    merchantChoicesSource='';
    toast(merchant?`Forhandler gemt: ${merchant}`:'Forhandler fjernet');
    await refresh();
    if($('incompleteTransactionsDialog').open)await loadIncompleteTransactions();
    if(view==='savings')await loadSavings();
  }catch(error){showError('merchantError',error.message);}
  finally{button.disabled=false;}
});
$('clearMerchant').addEventListener('click',()=>{
  $('merchantName').value='';
  $('merchantRemember').checked=true;
  $('merchantFixed').checked=false;
  $('merchantForm').requestSubmit($('saveMerchant'));
});

$('categorySearch').addEventListener('input',()=>{categoryActive=-1;renderCategoryChoices();});
$('categoryOptions').addEventListener('click',async event=>{
  const create=event.target.closest('[data-create-category]');
  if(create){
    const name=create.dataset.createCategory.trim();
    create.disabled=true;showError('categoryPickerError','');
    try{
      const result=await api('/api/categories',{method:'POST',body:JSON.stringify({name})});
      config.categories=result.items.map(item=>item.name);
      config.colors=result.items.map(item=>item.color);
      const selectedFilter=$('categoryFilter').value;
      $('categoryFilter').innerHTML='<option value="">Alle kategorier</option>'+config.categories.map(category=>`<option>${esc(category)}</option>`).join('');
      if(config.categories.includes(selectedFilter))$('categoryFilter').value=selectedFilter;
      $('categoryPickerForm').dataset.category=name;
      $('categorySearch').value=name;
      categoryActive=-1;
      renderCategoryChoices();
      toast(`Kategorien “${name}” er oprettet og valgt`);
    }catch(error){showError('categoryPickerError',error.message);}
    finally{create.disabled=false;}
    return;
  }
  const option=event.target.closest('.choice-option');if(!option)return;
  $('categoryPickerForm').dataset.category=option.dataset.value;
  $('categorySearch').value=option.dataset.value;
  categoryActive=-1;
  renderCategoryChoices();
});
$('categorySearch').addEventListener('keydown',event=>{
  if(event.key==='ArrowDown'){event.preventDefault();moveActive('categorySearch','categoryOptions',1,'category');}
  else if(event.key==='ArrowUp'){event.preventDefault();moveActive('categorySearch','categoryOptions',-1,'category');}
  else if(event.key==='Enter' && categoryActive>=0){event.preventDefault();chooseActive('categoryOptions','category');}
});
$('categoryPickerForm').addEventListener('submit',async event=>{
  event.preventDefault();
  const form=event.currentTarget, button=event.submitter;
  const category=form.dataset.category;
  if(!category || !(config?.categories || []).includes(category)){
    showError('categoryPickerError','Vælg en kategori fra listen.');
    return;
  }
  button.disabled=true;showError('categoryPickerError','');
  try{
    const result=await api('/api/transactions/'+form.dataset.transaction,{method:'PATCH',body:JSON.stringify({category})});
    $('categoryDialog').close();
    toast(`Kategori gemt · ${result.updated} posteringer opdateret. Huskes fremover.`);
    await refresh();
    if($('incompleteTransactionsDialog').open)await loadIncompleteTransactions();
  }catch(error){showError('categoryPickerError',error.message);}
  finally{button.disabled=false;}
});


async function loadMerchantLibrary(){
  showError('merchantLibraryError','');
  const result=await api('/api/merchant-library?source='+encodeURIComponent(source));
  $('merchantLibraryCount').textContent=result.items.length;
  $('merchantLibrary').innerHTML=result.items.length?result.items.map(item=>{
    const rules=item.rules.length?item.rules.map(rule=>{
      const normalized=rule.bank_text===rule.title?'':`<small>Gemt mønster: ${esc(rule.title)}</small>`;
      return `<div class="merchant-rule-row"><div><strong>${esc(rule.bank_text)}</strong>${normalized}</div><button type="button" class="text-button merchant-rule-delete" data-delete-merchant-rule="${esc(rule.title)}" data-rule-merchant="${esc(item.name)}" aria-label="Slet banktekst ${esc(rule.bank_text)}">Slet banktekst</button></div>`;
    }).join(''):'<p class="small muted merchant-no-rules">Ingen gemte banktekster. Forhandleren findes kun på eksisterende posteringer.</p>';
    return `<article class="merchant-library-card" data-library-merchant="${esc(item.name)}"><div class="merchant-library-head"><div><h3>${esc(item.name)}</h3><p>${item.transactions} ${item.transactions===1?'postering':'posteringer'} · ${item.rule_count} ${item.rule_count===1?'gemt banktekst':'gemte banktekster'}${item.adjustable===false?' · Fast udgift':''}</p></div><button type="button" class="button quiet merchant-delete-button" data-delete-library-merchant="${esc(item.name)}">Slet forhandler</button></div><div class="merchant-rules">${rules}</div></article>`;
  }).join(''):'<div class="empty-state"><svg><use href="#icon-store"/></svg><h3>Ingen forhandlere endnu</h3><p>Når du tilknytter forhandlere til posteringer, vises de her.</p></div>';
}
window.loadMerchantLibrary=loadMerchantLibrary;

$('merchantLibrary').addEventListener('click',event=>{
  const rule=event.target.closest('[data-delete-merchant-rule]');
  if(rule){
    $('merchantDeleteForm').dataset.mode='rule';
    $('merchantDeleteForm').dataset.title=rule.dataset.deleteMerchantRule;
    $('merchantDeleteForm').dataset.merchant='';
    $('merchantDeleteTitle').textContent='Slet gemt banktekst?';
    $('merchantDeleteText').textContent=`Bankteksten fjernes fra “${rule.dataset.ruleMerchant}”. Eksisterende posteringer beholder deres forhandler, men denne tekst genkendes ikke automatisk fremover.`;
    $('confirmMerchantDelete').textContent='Slet banktekst';
    showError('merchantDeleteError','');
    $('merchantDeleteDialog').showModal();
    return;
  }
  const merchant=event.target.closest('[data-delete-library-merchant]');
  if(merchant){
    $('merchantDeleteForm').dataset.mode='merchant';
    $('merchantDeleteForm').dataset.merchant=merchant.dataset.deleteLibraryMerchant;
    $('merchantDeleteForm').dataset.title='';
    $('merchantDeleteTitle').textContent='Slet hele forhandleren?';
    $('merchantDeleteText').textContent=`“${merchant.dataset.deleteLibraryMerchant}” fjernes sammen med alle gemte banktekster. Forhandlernavnet fjernes også fra eksisterende posteringer.`;
    $('confirmMerchantDelete').textContent='Slet forhandler';
    showError('merchantDeleteError','');
    $('merchantDeleteDialog').showModal();
  }
});

$('merchantDeleteForm').addEventListener('submit',async event=>{
  event.preventDefault();
  const form=event.currentTarget, button=$('confirmMerchantDelete');
  button.disabled=true;showError('merchantDeleteError','');
  try{
    const body=form.dataset.mode==='rule'?{title:form.dataset.title}:{merchant:form.dataset.merchant};
    const result=await api('/api/merchant-library?source='+encodeURIComponent(source),{method:'DELETE',body:JSON.stringify(body)});
    $('merchantDeleteDialog').close();
    merchantChoicesSource='';
    await loadMerchantLibrary();
    await refresh();
    if(view==='merchants')setView('merchants');
    toast(form.dataset.mode==='rule'?'Bankteksten er slettet':`Forhandleren er slettet · ${result.cleared_transactions} posteringer opdateret`);
  }catch(error){showError('merchantDeleteError',error.message);}
  finally{button.disabled=false;}
});
