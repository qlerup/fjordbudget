'use strict';
const merchantRows=$('transactionRows');
let merchantSuggestionsSource='';
async function loadMerchantSuggestions(){
  const requested=source;
  if(merchantSuggestionsSource===requested && $('merchantSuggestions').children.length)return;
  const result=await api('/api/merchants?source='+encodeURIComponent(requested));
  if(requested!==source)return;
  $('merchantSuggestions').replaceChildren(...result.items.map(item=>{
    const option=document.createElement('option');
    option.value=item.name;
    option.label=item.uses===1?'Brugt 1 gang':`Brugt ${item.uses} gange`;
    return option;
  }));
  merchantSuggestionsSource=requested;
}
merchantRows.addEventListener('click',async event=>{
  const button=event.target.closest('[data-edit-merchant]');
  if(!button)return;
  $('merchantForm').dataset.transaction=button.dataset.editMerchant;
  $('merchantBankText').textContent=button.dataset.description || '';
  $('merchantName').value=button.dataset.merchant || '';
  $('merchantRemember').checked=true;
  showError('merchantError','');
  try{await loadMerchantSuggestions();}catch(error){showError('merchantError','Kunne ikke hente eksisterende forhandlere. Du kan stadig skrive en ny.');}
  $('merchantDialog').showModal();
  $('merchantName').focus();
});
$('merchantForm').addEventListener('submit',async event=>{
  event.preventDefault();
  const form=event.currentTarget, button=event.submitter;
  button.disabled=true;showError('merchantError','');
  try{
    const merchant=$('merchantName').value.trim();
    const remember=$('merchantRemember').checked;
    await api(`/api/transactions/${form.dataset.transaction}/merchant`,{
      method:'PATCH',
      body:JSON.stringify({merchant,remember}),
    });
    $('merchantDialog').close();
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
