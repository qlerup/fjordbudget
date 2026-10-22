'use strict';
document.querySelectorAll('time').forEach(element=>{
  const original=element.textContent,date=new Date(original);
  if(!Number.isNaN(date.valueOf())){element.dateTime=original;element.textContent=new Intl.DateTimeFormat('da-DK',{dateStyle:'medium',timeStyle:'short'}).format(date);}
});
document.addEventListener('click',event=>{
  const close=event.target.closest('[data-close]');
  if(close){document.getElementById(close.dataset.close).close();return;}
  const review=event.target.closest('[data-review]');
  if(review){
    const form=document.getElementById('reviewForm');form.reset();
    form.setAttribute('action','/proposals/'+review.dataset.review+'/review');
    document.getElementById('reviewText').textContent=review.dataset.bankText;
    document.getElementById('merchant').value=review.dataset.merchant;
    document.getElementById('mergeTarget').value=review.dataset.current;
    document.getElementById('replaceRow').hidden=!review.dataset.current;
    document.getElementById('reviewError').hidden=true;
    document.getElementById('reviewDialog').showModal();document.getElementById('merchant').focus();return;
  }
  const revoke=event.target.closest('[data-revoke]');
  if(revoke){document.getElementById('revokeForm').action='/proposals/'+revoke.dataset.revoke+'/revoke';document.getElementById('revokeText').textContent=revoke.dataset.bankText;document.getElementById('revokeDialog').showModal();}
});
document.getElementById('mergeTarget').addEventListener('change',event=>{
  if(event.target.value){document.getElementById('merchant').value=event.target.value;document.getElementById('replaceRow').hidden=false;}
});
document.getElementById('reviewForm').addEventListener('submit',async event=>{
  event.preventDefault();const form=event.currentTarget,buttons=form.querySelectorAll('button');buttons.forEach(b=>b.disabled=true);
  const error=document.getElementById('reviewError');error.hidden=true;
  try{const result=await fetch(form.getAttribute('action'),{method:'POST',body:new FormData(form)});if(!result.ok){let data;try{data=await result.json();}catch{throw new Error('Kunne ikke gemme. Prøv igen.');}throw new Error(data.error || 'Kunne ikke gemme.');}window.location.reload();}
  catch(problem){error.textContent=problem.message;error.hidden=false;buttons.forEach(b=>b.disabled=false);}
});
