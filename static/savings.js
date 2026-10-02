'use strict';
let savingsGoals=[], savingsGeneration=0, savingsLoadedScope='';
function savingsScope(){return new URLSearchParams({source,currency:$('currency').value}).toString();}
async function loadSavings(){
  const generation=++savingsGeneration, scope=savingsScope();
  savingsLoadedScope='';savingsGoals=[];
  $('savingsList').innerHTML='<p class="muted">Henter opsparingsmål …</p>';
  showError('savingsError','');
  try{
    const result=await api('/api/savings-goals?'+scope);
    if(generation!==savingsGeneration || scope!==savingsScope())return;
    savingsGoals=result.items;savingsLoadedScope=scope;
    const today=new Intl.DateTimeFormat('sv-SE',{timeZone:'Europe/Copenhagen'}).format(new Date());
    $('savingsList').innerHTML=savingsGoals.length?savingsGoals.map(goal=>{
      const deadline=new Intl.DateTimeFormat('da-DK',{day:'numeric',month:'long',year:'numeric'}).format(new Date(goal.deadline+'T12:00:00'));
      return `<article class="savings-card"><span class="savings-icon">${svg('target')}</span><h3>${esc(goal.name)}</h3><p class="savings-target">${esc(money(goal.target_amount,goal.currency,2))}</p><p class="savings-deadline">Senest <time datetime="${esc(goal.deadline)}">${esc(deadline)}</time></p>${goal.deadline<today?'<p class="savings-status">Deadline er passeret</p>':goal.deadline===today?'<p class="savings-status">Deadline er i dag</p>':''}<div class="savings-actions"><button type="button" class="button quiet" data-edit-savings="${goal.id}" aria-label="Rediger ${esc(goal.name)}">Rediger</button><button type="button" class="text-button" data-delete-savings="${goal.id}" aria-label="Slet ${esc(goal.name)}">Slet</button></div></article>`;
    }).join(''):'<div class="empty-state"><h3>Hvad drømmer du om?</h3><p>Opret dit første opsparingsmål med et beløb og en deadline.</p></div>';
  }catch(error){if(generation===savingsGeneration){$('savingsList').innerHTML='';showError('savingsError',error.message);}}
}
function openSavings(goal=null){
  $('savingsForm').reset();
  $('savingsForm').dataset.id=goal?.id || '';
  $('savingsForm').dataset.scope=savingsScope();
  $('savingsDialogTitle').textContent=goal?'Rediger opsparingsmål':'Nyt opsparingsmål';
  $('savingsContext').textContent=(source==='demo'?'Demodata':'Mine bankdata')+' · '+$('currency').value;
  if(goal){$('savingsName').value=goal.name;$('savingsAmount').value=goal.target_amount/100;$('savingsDeadline').value=goal.deadline;}
  showError('savingsFormError','');$('savingsDialog').showModal();$('savingsName').focus();
}
$('newSavingsGoal').addEventListener('click',()=>openSavings());
$('savingsList').addEventListener('click',event=>{
  const button=event.target.closest('[data-edit-savings],[data-delete-savings]');if(!button || savingsLoadedScope!==savingsScope())return;
  const goal=savingsGoals.find(g=>g.id===Number(button.dataset.editSavings || button.dataset.deleteSavings));if(!goal)return;
  if(button.hasAttribute('data-edit-savings')){openSavings(goal);return;}
  $('savingsDeleteForm').dataset.id=goal.id;$('savingsDeleteForm').dataset.scope=savingsScope();
  $('savingsDeleteText').textContent=`Vil du slette opsparingsmålet “${goal.name}”? Dine konti og posteringer bevares.`;
  showError('savingsDeleteError','');$('savingsDeleteDialog').showModal();
});
$('savingsForm').addEventListener('submit',async event=>{
  event.preventDefault();const button=event.submitter, form=event.currentTarget;button.disabled=true;showError('savingsFormError','');
  try{
    await api('/api/savings-goals'+(form.dataset.id?'/'+form.dataset.id:'')+'?'+form.dataset.scope,{method:form.dataset.id?'PUT':'POST',body:JSON.stringify(Object.fromEntries(new FormData(form)))});
    $('savingsDialog').close();toast('Opsparingsmålet er gemt');await loadSavings();
  }catch(error){showError('savingsFormError',error.message);}finally{button.disabled=false;}
});
$('savingsDeleteForm').addEventListener('submit',async event=>{
  event.preventDefault();const button=event.submitter, form=event.currentTarget;button.disabled=true;showError('savingsDeleteError','');
  try{await api('/api/savings-goals/'+form.dataset.id+'?'+form.dataset.scope,{method:'DELETE'});$('savingsDeleteDialog').close();toast('Opsparingsmålet er slettet');await loadSavings();}
  catch(error){showError('savingsDeleteError',error.message);}finally{button.disabled=false;}
});
