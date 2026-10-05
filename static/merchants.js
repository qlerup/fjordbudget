'use strict';
const merchantRows=$('transactionRows');
merchantRows.addEventListener('click',event=>{
  const button=event.target.closest('[data-edit-merchant]');
  if(!button)return;
  $('merchantForm').dataset.transaction=button.dataset.editMerchant;
  $('merchantBankText').textContent=button.dataset.description || '';
  $('merchantName').value=button.dataset.merchant || '';
  $('merchantRemember').checked=true;
  showError('merchantError','');
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
