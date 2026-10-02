'use strict';
let savingsGoals=[], savingsGeneration=0, savingsLoadedScope='';
function savingsScope(){return new URLSearchParams({source,currency:$('currency').value}).toString();}
function formatSavingsAmount(value){
  if(!/^[\d.]+(?:,\d{0,2})?$/.test(value))return value;
  const [whole, fraction]=value.replaceAll('.','').split(',');
  return whole.replace(/\B(?=(\d{3})+(?!\d))/g,'.')+(fraction===undefined?'':','+fraction);
}
$('savingsAmount').addEventListener('input',event=>{
  const input=event.target, position=input.selectionStart;
  const count=input.value.slice(0,position).replaceAll('.','').length;
  input.value=formatSavingsAmount(input.value);
  let caret=0, seen=0;
  while(caret<input.value.length && seen<count){if(input.value[caret]!=='.')seen++;caret++;}
  input.setSelectionRange(caret,caret);
});
$('savingsAmount').addEventListener('beforeinput',event=>{
  const input=event.target, start=input.selectionStart, end=input.selectionEnd;
  if(start!==end)return;
  // Delete a digit along with an adjacent grouping dot, so deletion never gets stuck.
  if(event.inputType==='deleteContentBackward' && input.value[start-1]==='.')input.setSelectionRange(start-2,start);
  if(event.inputType==='deleteContentForward' && input.value[start]==='.')input.setSelectionRange(start,start+2);
});
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
  if(goal){$('savingsName').value=goal.name;$('savingsAmount').value=formatSavingsAmount((goal.target_amount/100).toFixed(2).replace('.',','));$('savingsDeadline').value=goal.deadline;}
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
    const values=Object.fromEntries(new FormData(form));
    values.target_amount=values.target_amount.replaceAll('.','').replace(',','.');
    await api('/api/savings-goals'+(form.dataset.id?'/'+form.dataset.id:'')+'?'+form.dataset.scope,{method:form.dataset.id?'PUT':'POST',body:JSON.stringify(values)});
    $('savingsDialog').close();toast('Opsparingsmålet er gemt');await loadSavings();
  }catch(error){showError('savingsFormError',error.message);}finally{button.disabled=false;}
});
$('savingsDeleteForm').addEventListener('submit',async event=>{
  event.preventDefault();const button=event.submitter, form=event.currentTarget;button.disabled=true;showError('savingsDeleteError','');
  try{await api('/api/savings-goals/'+form.dataset.id+'?'+form.dataset.scope,{method:'DELETE'});$('savingsDeleteDialog').close();toast('Opsparingsmålet er slettet');await loadSavings();}
  catch(error){showError('savingsDeleteError',error.message);}finally{button.disabled=false;}
});
