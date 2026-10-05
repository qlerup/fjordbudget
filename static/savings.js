'use strict';
let savingsGoals=[], savingsGeneration=0, savingsLoadedScope='';

function savingsScope(){return new URLSearchParams({source,currency:$('currency').value}).toString();}
function formatSavingsAmount(value){
  if(!/^[\d.]+(?:,\d{0,2})?$/.test(value))return value;
  const [whole, fraction]=value.replaceAll('.','').split(',');
  return whole.replace(/\B(?=(\d{3})+(?!\d))/g,'.')+(fraction===undefined?'':','+fraction);
}
function attachSavingsMoneyInput(id){
  const input=$(id);
  input.addEventListener('input',event=>{
    const field=event.target, position=field.selectionStart;
    const count=field.value.slice(0,position).replaceAll('.','').length;
    field.value=formatSavingsAmount(field.value);
    let caret=0, seen=0;
    while(caret<field.value.length && seen<count){if(field.value[caret]!=='.')seen++;caret++;}
    field.setSelectionRange(caret,caret);
  });
  input.addEventListener('beforeinput',event=>{
    const field=event.target, start=field.selectionStart, end=field.selectionEnd;
    if(start!==end)return;
    if(event.inputType==='deleteContentBackward' && field.value[start-1]==='.')field.setSelectionRange(start-2,start);
    if(event.inputType==='deleteContentForward' && field.value[start]==='.')field.setSelectionRange(start,start+2);
  });
}
attachSavingsMoneyInput('savingsAmount');
attachSavingsMoneyInput('savingsSavedAmount');

function savingsStatus(goal){
  const a=goal.analysis;
  const amount=money(a.required_monthly,goal.currency,0);
  if(a.status==='complete')return ['complete','Målet er nået','Du har allerede sparet hele målbeløbet op.'];
  if(a.status==='expired')return ['danger','Deadline er passeret',`Der mangler ${money(a.remaining,goal.currency,0)} på målet.`];
  if(a.status==='insufficient_data')return ['neutral','Mangler historik','FjordBudget skal bruge flere posteringer, før målet kan vurderes sikkert.'];
  if(a.status==='realistic')return ['good','Målet ser realistisk ud',`Du skal spare ca. ${amount} om måneden. Dit historiske råderum er ca. ${money(a.average_available,goal.currency,0)}.`];
  if(a.status==='tight')return ['warning','Målet er stramt, men muligt',`Næsten hele dit gennemsnitlige råderum på ${money(a.average_available,goal.currency,0)} skal gå til målet.`];
  if(a.status==='possible_with_changes')return ['warning','Målet kræver ændringer',`Du mangler ca. ${money(a.monthly_shortfall,goal.currency,0)} om måneden. FjordBudget har fundet steder, hvor det muligvis kan hentes.`];
  return ['danger','Målet er ikke realistisk endnu',`Du mangler ca. ${money(a.monthly_shortfall,goal.currency,0)} om måneden, og de fundne besparelser dækker ikke hele forskellen.`];
}
function savingsSuggestion(item,currency){
  const type=item.type==='merchant'?'Forhandler':'Kategori';
  return `<li><div><strong>${esc(item.name)}</strong><span>${type} · ${esc(item.category)} · normalt ca. ${esc(money(item.monthly_average,currency,0))}/md.</span></div><b>+${esc(money(item.recommended_cut,currency,0))}/md.</b></li>`;
}
function renderSavingsProfile(profile){
  const categories=profile.categories.slice(0,5);
  const merchants=profile.merchants.slice(0,5);
  $('savingsProfile').innerHTML=profile.months_analyzed?`
    <div class="savings-profile-summary">
      <article><span>Gns. indtægt</span><strong>${esc(money(profile.average_income,$('currency').value,0))}</strong></article>
      <article><span>Gns. udgifter</span><strong>${esc(money(profile.average_expenses,$('currency').value,0))}</strong></article>
      <article><span>Historisk råderum</span><strong class="${profile.average_available<0?'negative':''}">${esc(money(profile.average_available,$('currency').value,0))}/md.</strong></article>
    </div>
    <div class="spending-breakdown">
      <div><h3>Hvor pengene går hen</h3>${categories.length?`<ol>${categories.map(item=>`<li><span>${esc(item.name)}</span><b>${esc(money(item.monthly_average,$('currency').value,0))}/md.</b></li>`).join('')}</ol>`:'<p class="muted">Ingen udgifter at analysere endnu.</p>'}</div>
      <div><h3>Forhandlere du har lært FjordBudget</h3>${merchants.length?`<ol>${merchants.map(item=>`<li><span>${esc(item.name)}<small>${esc(item.category)} · ${item.purchases} køb</small></span><b>${esc(money(item.monthly_average,$('currency').value,0))}/md.</b></li>`).join('')}</ol>`:'<p class="muted">Angiv forhandler på posteringer, så analysen kan blive mere konkret.</p>'}</div>
    </div>
    <p class="analysis-note">Baseret på ${profile.months_analyzed} ${profile.months_analyzed===1?'måned':'måneder'} med posteringer. Forslag er muligheder — ikke en vurdering af hvad du bør bruge penge på.</p>`:
    '<div class="empty-state"><h3>Analysen vokser med dine data</h3><p>Når der er posteringer, kan FjordBudget sammenholde løn, udgifter, forhandlere og dine mål.</p></div>';
}
async function loadSavings(){
  const generation=++savingsGeneration, scope=savingsScope();
  savingsLoadedScope='';savingsGoals=[];
  $('savingsList').innerHTML='<p class="muted">Henter opsparingsmål …</p>';
  showError('savingsError','');
  try{
    const result=await api('/api/savings-goals?'+scope);
    if(generation!==savingsGeneration || scope!==savingsScope())return;
    savingsGoals=result.items;savingsLoadedScope=scope;
    renderSavingsProfile(result.profile);
    $('savingsList').innerHTML=savingsGoals.length?savingsGoals.map(goal=>{
      const deadline=new Intl.DateTimeFormat('da-DK',{month:'long',year:'numeric'}).format(new Date(goal.deadline+'-01T12:00:00'));
      const progress=goal.target_amount?Math.min(100,goal.saved_amount/goal.target_amount*100):0;
      const [tone,label,text]=savingsStatus(goal);
      const suggestions=goal.analysis.suggestions || [];
      return `<article class="savings-card ${goal.featured?'featured':''}">
        <div class="savings-card-top"><span class="savings-icon">${svg('target')}</span>${goal.featured?'<span class="goal-featured">Vises på overblikket</span>':''}</div>
        <h3>${esc(goal.name)}</h3>
        <p class="savings-target">${esc(money(goal.saved_amount,goal.currency,2))} <small>af ${esc(money(goal.target_amount,goal.currency,2))}</small></p>
        <div class="progress"><div class="progress-fill" style="width:${progress}%"></div></div>
        <p class="savings-deadline">Senest <time datetime="${esc(goal.deadline)}">${esc(deadline)}</time></p>
        <div class="goal-status ${tone}"><strong>${esc(label)}</strong><span>${esc(text)}</span></div>
        ${goal.analysis.months_analyzed?`<div class="goal-numbers"><span>Kræver <b>${esc(money(goal.analysis.required_monthly,goal.currency,0))}/md.</b></span><span>Råderum <b>${esc(money(goal.analysis.average_available,goal.currency,0))}/md.</b></span></div>`:''}
        ${suggestions.length?`<div class="goal-suggestions"><h4>${goal.analysis.monthly_shortfall?'Forslag til at lukke forskellen':'Muligheder hvis du vil nå målet hurtigere'}</h4><ul>${suggestions.map(item=>savingsSuggestion(item,goal.currency)).join('')}</ul>${goal.analysis.projected_with_changes?`<p>Med disse ændringer peger beregningen mod <strong>${esc(monthName(goal.analysis.projected_with_changes))}</strong>.</p>`:''}</div>`:''}
        <div class="savings-actions"><button type="button" class="button quiet" data-edit-savings="${goal.id}" aria-label="Rediger ${esc(goal.name)}">Rediger</button><button type="button" class="text-button" data-delete-savings="${goal.id}" aria-label="Slet ${esc(goal.name)}">Slet</button></div>
      </article>`;
    }).join(''):'<div class="empty-state"><h3>Hvad drømmer du om?</h3><p>Opret dit første mål. FjordBudget beregner derefter, hvad det kræver pr. måned.</p></div>';
  }catch(error){if(generation===savingsGeneration){$('savingsList').innerHTML='';$('savingsProfile').innerHTML='';showError('savingsError',error.message);}}
}
function openSavings(goal=null){
  $('savingsForm').reset();
  $('savingsForm').dataset.id=goal?.id || '';
  $('savingsForm').dataset.scope=savingsScope();
  $('savingsDialogTitle').textContent=goal?'Rediger opsparingsmål':'Nyt opsparingsmål';
  $('savingsContext').textContent=(source==='demo'?'Demodata':'Mine bankdata')+' · '+$('currency').value;
  if(goal){
    $('savingsName').value=goal.name;
    $('savingsAmount').value=formatSavingsAmount((goal.target_amount/100).toFixed(2).replace('.',','));
    $('savingsSavedAmount').value=formatSavingsAmount((goal.saved_amount/100).toFixed(2).replace('.',','));
    $('savingsDeadline').value=goal.deadline;
    $('savingsFeatured').checked=!!goal.featured;
  }
  showError('savingsFormError','');$('savingsDialog').showModal();$('savingsName').focus();
}
window.renderFeaturedGoal=function(goal){
  const panel=$('featuredGoalPanel');
  if(!goal){panel.hidden=true;panel.innerHTML='';return;}
  panel.hidden=false;
  const a=goal.analysis, progress=goal.target_amount?Math.min(100,goal.saved_amount/goal.target_amount*100):0;
  const [tone,label,text]=savingsStatus(goal);
  panel.innerHTML=`<div class="featured-goal-copy"><p class="eyebrow">DIT AKTIVE MÅL</p><h2>${esc(goal.name)}</h2><p>${esc(text)}</p><button type="button" class="text-button" data-view="savings">Se analyse og forslag <span>→</span></button></div>
    <div class="featured-goal-numbers"><span>${esc(money(goal.saved_amount,goal.currency,0))} af ${esc(money(goal.target_amount,goal.currency,0))}</span><div class="progress"><div class="progress-fill" style="width:${progress}%"></div></div><strong class="${tone}">${esc(label)}</strong>${a.months_analyzed?`<small>${esc(money(a.required_monthly,goal.currency,0))}/md. nødvendigt · ${esc(money(a.average_available,goal.currency,0))}/md. historisk råderum</small>`:''}</div>`;
};
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
    values.saved_amount=(values.saved_amount || '0').replaceAll('.','').replace(',','.');
    values.featured=$('savingsFeatured').checked;
    await api('/api/savings-goals'+(form.dataset.id?'/'+form.dataset.id:'')+'?'+form.dataset.scope,{method:form.dataset.id?'PUT':'POST',body:JSON.stringify(values)});
    $('savingsDialog').close();toast('Opsparingsmålet er gemt');await refresh();if(view==='savings')await loadSavings();
  }catch(error){showError('savingsFormError',error.message);}finally{button.disabled=false;}
});
$('savingsDeleteForm').addEventListener('submit',async event=>{
  event.preventDefault();const button=event.submitter, form=event.currentTarget;button.disabled=true;showError('savingsDeleteError','');
  try{await api('/api/savings-goals/'+form.dataset.id+'?'+form.dataset.scope,{method:'DELETE'});$('savingsDeleteDialog').close();toast('Opsparingsmålet er slettet');await refresh();if(view==='savings')await loadSavings();}
  catch(error){showError('savingsDeleteError',error.message);}finally{button.disabled=false;}
});
