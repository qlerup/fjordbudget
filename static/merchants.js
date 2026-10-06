'use strict';
const transactionRows=$('transactionRows');
let merchantChoices=[], merchantChoicesSource='', merchantActive=-1, categoryActive=-1;

function normalizedSearch(value){return String(value || '').toLocaleLowerCase('da').trim();}

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
    const meta=document.createElement('small');meta.textContent=item.uses===1?'Brugt 1 gang':`Brugt ${item.uses} gange`;
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

transactionRows.addEventListener('click',async event=>{
  const merchantButton=event.target.closest('[data-edit-merchant]');
  if(merchantButton){
    $('merchantForm').dataset.transaction=merchantButton.dataset.editMerchant;
    $('merchantForm').dataset.selected=merchantButton.dataset.merchant || '';
    $('merchantBankText').textContent=merchantButton.dataset.description || '';
    $('merchantName').value=merchantButton.dataset.merchant || '';
    $('merchantRemember').checked=true;
    showError('merchantError','');
    try{await loadMerchantChoices();}catch(error){merchantChoices=[];showError('merchantError','Kunne ikke hente eksisterende forhandlere. Du kan stadig skrive en ny.');}
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
});

$('merchantName').addEventListener('input',renderMerchantChoices);
$('merchantOptions').addEventListener('click',event=>{
  const option=event.target.closest('.choice-option');if(!option)return;
  $('merchantName').value=option.dataset.value;
  $('merchantForm').dataset.selected=option.dataset.value;
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
    await api(`/api/transactions/${form.dataset.transaction}/merchant`,{
      method:'PATCH',body:JSON.stringify({merchant,remember}),
    });
    $('merchantDialog').close();
    merchantChoicesSource='';
    toast(merchant?`Forhandler gemt: ${merchant}`:'Forhandler fjernet');
    await refresh();
    if(view==='savings')await loadSavings();
  }catch(error){showError('merchantError',error.message);}
  finally{button.disabled=false;}
});
$('clearMerchant').addEventListener('click',()=>{
  $('merchantName').value='';
  $('merchantRemember').checked=true;
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
  }catch(error){showError('categoryPickerError',error.message);}
  finally{button.disabled=false;}
});
