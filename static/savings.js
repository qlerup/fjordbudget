'use strict';
let savingsGoals=[], savingsAccounts=[], savingsGeneration=0, savingsLoadedScope='', savingsPeriodInitialized=false;

function savingsIsoDate(value){
  const year=value.getFullYear(), month=String(value.getMonth()+1).padStart(2,'0'), day=String(value.getDate()).padStart(2,'0');
  return year+'-'+month+'-'+day;
}
function ensureSavingsPeriod(){
  if(savingsPeriodInitialized && $('savingsPeriodFrom').value && $('savingsPeriodTo').value)return;
  const now=new Date(), firstThisMonth=new Date(now.getFullYear(),now.getMonth(),1);
  const end=new Date(firstThisMonth.getTime()-86400000);
  const start=new Date(end.getFullYear(),end.getMonth(),1);
  $('savingsPeriodFrom').value=savingsIsoDate(start);
  $('savingsPeriodTo').value=savingsIsoDate(end);
  savingsPeriodInitialized=true;
}
function savingsScope(){
  ensureSavingsPeriod();
  const from=$('savingsPeriodFrom').value, to=$('savingsPeriodTo').value;
  if(!from || !to)throw new Error('Vælg både fra- og til-dato.');
  if(from>to)throw new Error('Fra-dato skal være før eller samme dag som til-dato.');
  return new URLSearchParams({source,currency:$('currency').value,from,to}).toString();
}
function savingsDateLabel(value){
  return new Intl.DateTimeFormat('da-DK',{day:'numeric',month:'short',year:'numeric'}).format(new Date(value+'T12:00:00'));
}
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
  if(!(goal.analysis_account_ids || []).length && a.status!=='complete' && a.status!=='expired')
    return ['neutral','Vælg konti til analysen','Rediger målet og sæt flueben ved de konti, der skal bruges til spareforslag.'];
  const amount=money(a.required_monthly,goal.currency,0);
  if(a.status==='complete')return ['complete','Målet er nået','Du har allerede sparet hele målbeløbet op.'];
  if(a.status==='expired')return ['danger','Deadline er passeret',`Der mangler ${money(a.remaining,goal.currency,0)} på målet.`];
  if(a.status==='insufficient_data')return ['neutral','Mangler historik','FjordBudget skal bruge flere posteringer, før målet kan vurderes sikkert.'];
  if(a.status==='realistic')return ['good','Målet ser realistisk ud',`Du skal spare ca. ${amount} om måneden. Dit historiske råderum er ca. ${money(a.average_available,goal.currency,0)}.`];
  if(a.status==='tight')return ['warning','Målet er stramt, men muligt',`Næsten hele dit gennemsnitlige råderum på ${money(a.average_available,goal.currency,0)} skal gå til målet.`];
  if(a.status==='possible_with_changes')return ['warning','Målet kræver ændringer',`Du mangler ca. ${money(a.monthly_shortfall,goal.currency,0)} om måneden. FjordBudget har fundet steder, hvor det muligvis kan hentes.`];
  return ['danger','Målet er ikke realistisk endnu',`Du mangler ca. ${money(a.monthly_shortfall,goal.currency,0)} om måneden, og de fundne besparelser dækker ikke hele forskellen.`];
}
function savingsSuggestion(item,currency,goal){
  const visits=new Intl.NumberFormat('da-DK',{maximumFractionDigits:1}).format(item.average_visits_per_month);
  return `<li><div class="goal-suggestion-copy"><strong>${esc(item.name)}</strong><span>${esc(item.purchases)} køb i historikken · ca. ${esc(visits)} køb/md. · ${esc(money(item.monthly_average,currency,0))}/md. i gennemsnit</span><small>Hvis du stopper med at handle her, kan du frigøre ca. <strong>${esc(money(item.recommended_cut,currency,0))}/md.</strong> ud fra dit ${item.saving_reference==='recent'?'seneste gennemsnit':'historiske gennemsnit'}.</small>${item.saving_reference==='recent'?`<small>Dit seneste gennemsnit er ${esc(money(item.recent_monthly_average,currency,0))}/md. Beregningen bruger det lavere niveau.</small>`:''}</div><div class="goal-suggestion-actions"><b>Op til ${esc(money(item.recommended_cut,currency,0))}/md.</b><button type="button" class="text-button" data-skip-suggestion="${goal.id}" data-merchant="${esc(item.name)}" aria-label="Spring ${esc(item.name)} over i ${esc(monthName(goal.analysis.suggestion_month))}">Spring over denne måned</button></div></li>`;
}
function savingsSkippedSuggestions(goal){
  const skipped=goal.analysis.skipped_suggestions || [];
  if(!skipped.length)return '';
  return `<details class="goal-skipped"><summary>Sprunget over i ${esc(monthName(goal.analysis.suggestion_month))} (${skipped.length})</summary><p>Disse forslag tæller ikke med i målets besparelser denne måned. De kan vises igen næste måned.</p><ul>${skipped.map(item=>`<li><span>${esc(item.name)}</span><button type="button" class="text-button" data-restore-suggestion="${goal.id}" data-merchant="${esc(item.name)}" aria-label="Tag ${esc(item.name)} med igen">Tag med igen</button></li>`).join('')}</ul></details>`;
}
function savingsSuggestions(goal){
  const suggestions=goal.analysis.suggestions || [];
  const h=goal.analysis.suggestion_history;
  const historyNote=h?.months_analyzed?`<p class="analysis-note suggestion-history">Spareforslag bygger på ${h.months_analyzed} måneders historik: <strong>${esc(savingsDateLabel(h.period_start))} – ${esc(savingsDateLabel(h.period_end))}</strong>. Sammenlignet med ${esc(savingsDateLabel(h.recent_period_start))} – ${esc(savingsDateLabel(h.recent_period_end))} for at tage højde for ændrede vaner.</p>`:'<p class="analysis-note suggestion-history">Spareforslag kræver historik fra afsluttede måneder. Der bruges op til 12 måneder, efterhånden som historikken bliver tilgængelig.</p>';
  if(!suggestions.length)return historyNote+(h?.months_analyzed?`<p class="analysis-note">${goal.analysis.skipped_suggestions?.length?'Ingen flere relevante spareforslag denne måned.':'Ingen sikre forslag ud fra gentagne og fortsat aktuelle køb. Engangskøb regnes ikke som en fast månedlig besparelse.'}</p>`:'');
  const more=suggestions.slice(3);
  const rows=items=>items.map(item=>savingsSuggestion(item,goal.currency,goal)).join('');
  return `<div class="goal-suggestions"><h4>${goal.analysis.monthly_shortfall?'Forslag til at lukke forskellen':'Muligheder hvis du vil nå målet hurtigere'}</h4>${historyNote}<p class="muted">Scenarier for helt at stoppe køb. Beløbet kan kun spares, hvis det ikke bruges hos andre forhandlere.</p><p class="goal-suggestion-count">${suggestions.length===1?'Der er fundet 1 relevant forslag.':`Der er fundet ${suggestions.length} relevante forslag.`}</p><ul>${rows(suggestions.slice(0,3))}</ul>${more.length?`<details class="goal-more-suggestions"><summary><span class="suggestions-show-more">Vis ${more.length} flere forslag</span><span class="suggestions-show-less">Vis færre forslag</span></summary><ul>${rows(more)}</ul></details>`:''}${goal.analysis.projected_with_changes?`<p>Hvis du fastholder ${suggestions.length===1?'dette forslag':`alle ${suggestions.length} forslag`}, peger estimatet mod <strong>${esc(monthName(goal.analysis.projected_with_changes))}</strong>.</p>`:''}</div>`;
}
function renderSavingsProfile(profile){
  const merchants=profile.merchants.slice(0,5);
  const period=savingsDateLabel(profile.period_start)+' – '+savingsDateLabel(profile.period_end);
  $('savingsProfile').innerHTML=profile.months_analyzed?`
    <p class="savings-period-caption">Samlet overblik for <strong>${esc(period)}</strong> · målenes spareforslag bruger kun de konti, du vælger på hvert mål.</p>
    <div class="savings-profile-summary">
      <article><span>Indtægt i perioden</span><strong>${esc(money(profile.total_income,$('currency').value,0))}</strong></article>
      <article><span>Udgifter i perioden</span><strong>${esc(money(profile.total_expenses,$('currency').value,0))}</strong></article>
      <article><span>Råderum i perioden</span><strong class="${profile.total_available<0?'negative':''}">${esc(money(profile.total_available,$('currency').value,0))}</strong></article>
    </div>
    <div class="spending-breakdown">
      <div><h3>Forhandlere i perioden</h3>${merchants.length?`<ol>${merchants.map(item=>`<li><span>${esc(item.name)}<small>${item.purchases} køb${item.adjustable===false?' · fast udgift':''}</small></span><b>${esc(money(item.total,$('currency').value,0))}</b></li>`).join('')}</ol>`:'<p class="muted">Angiv forhandler på posteringer, så analysen kan blive mere konkret.</p>'}</div>
      <div><h3>Sådan findes besparelser</h3><p class="muted">Spareforslag viser, hvad du kan frigøre ved helt at stoppe køb hos en forhandler. Beløbet er det laveste af dit gennemsnit over op til 12 afsluttede måneder og de seneste tre måneder. Forhandlere uden nylige køb, engangskøb og faste udgifter udelades. Det forudsætter, at pengene ikke bruges hos andre forhandlere.</p></div>
    </div>
    <p class="analysis-note">Indtægt, udgifter og råderum bruger den valgte periode, omregnet til et månedligt niveau. Spareforslagene bruger separat op til 12 afsluttede måneders historik.</p>`:
    `<div class="empty-state"><h3>Ingen posteringer i perioden</h3><p>Der er ingen posteringer mellem ${esc(period)}. Vælg en anden periode for at analysere andre datoer.</p></div>`;
}
async function loadSavings(){
  const generation=++savingsGeneration;
  let scope;
  try{scope=savingsScope();}catch(error){showError('savingsError',error.message);return;}
  savingsLoadedScope='';savingsGoals=[];
  $('newSavingsGoal').disabled=true;
  $('savingsList').innerHTML='<p class="muted">Henter opsparingsmål …</p>';
  showError('savingsError','');
  try{
    const result=await api('/api/savings-goals?'+scope);
    if(generation!==savingsGeneration || scope!==savingsScope())return;
    savingsGoals=result.items;savingsAccounts=result.accounts || [];savingsLoadedScope=scope;
    renderSavingsProfile(result.profile);
    $('savingsList').innerHTML=savingsGoals.length?savingsGoals.map(goal=>{
      const deadline=new Intl.DateTimeFormat('da-DK',{month:'long',year:'numeric'}).format(new Date(goal.deadline+'-01T12:00:00'));
      const progress=goal.target_amount?Math.max(0,Math.min(100,goal.saved_amount/goal.target_amount*100)):0;
      const [tone,label,text]=savingsStatus(goal);
      const accountLine=goal.uses_live_balance
        ? `<p class="savings-account-source">${svg('bank')}<span>Følger <strong>${esc(goal.account_name)}</strong> live${goal.account_balance_available?'':' · saldo afventer'}</span></p>`
        : '';
      const chosenAccounts=(goal.analysis_account_ids || []).map(id=>savingsAccounts.find(account=>account.id===id)).filter(Boolean);
      const analysisSource=chosenAccounts.length
        ? '<p class="savings-analysis-source">'+svg('bank')+'<span>Spareforslag fra: <strong>'+chosenAccounts.map(account=>esc(account.name)).join(', ')+'</strong></span></p>'
        : '<p class="savings-analysis-source">Ingen konti valgt til spareforslag</p>';
      return `<article class="savings-card ${goal.featured?'featured':''}">
        <div class="savings-card-top"><span class="savings-icon">${svg('target')}</span>${goal.featured?'<span class="goal-featured">Vises på overblikket</span>':''}</div>
        <h3>${esc(goal.name)}</h3>
        <p class="savings-target">${esc(money(goal.saved_amount,goal.currency,2))} <small>af ${esc(money(goal.target_amount,goal.currency,2))}</small></p>
        ${accountLine}
        ${analysisSource}
        <div class="progress"><div class="progress-fill" style="width:${progress}%"></div></div>
        <p class="savings-deadline">Senest <time datetime="${esc(goal.deadline)}">${esc(deadline)}</time></p>
        <div class="goal-status ${tone}"><strong>${esc(label)}</strong><span>${esc(text)}</span></div>
        ${goal.analysis.months_analyzed?`<div class="goal-numbers"><span>Kræver <b>${esc(money(goal.analysis.required_monthly,goal.currency,0))}/md.</b></span><span>Råderum <b>${esc(money(goal.analysis.average_available,goal.currency,0))}/md.</b></span></div>`:''}
        ${savingsSuggestions(goal)}
        ${savingsSkippedSuggestions(goal)}
        <div class="savings-actions"><button type="button" class="button quiet" data-edit-savings="${goal.id}" aria-label="Rediger ${esc(goal.name)}">Rediger</button><button type="button" class="text-button" data-delete-savings="${goal.id}" aria-label="Slet ${esc(goal.name)}">Slet</button></div>
      </article>`;
    }).join(''):'<div class="empty-state"><h3>Hvad drømmer du om?</h3><p>Opret dit første mål. FjordBudget beregner derefter, hvad det kræver pr. måned.</p></div>';
  }catch(error){if(generation===savingsGeneration){$('savingsList').innerHTML='';$('savingsProfile').innerHTML='';showError('savingsError',error.message);}}
  finally{if(generation===savingsGeneration)$('newSavingsGoal').disabled=false;}
}
function updateSavingsAccountMode(){
  const account=savingsAccounts.find(item=>item.id===$('savingsAccount').value);
  const linked=!!account;
  $('savingsManualAmountFields').hidden=linked;
  $('savingsAccountHint').hidden=!linked;
  if(linked){
    const accountLabel=account.last4?account.name+' · •• '+account.last4:account.name;
    const balance=account.balance===null?'Saldo afventer bankens næste opdatering':'Aktuel saldo: '+money(account.balance,account.currency,2);
    const hiddenNote=account.included?'':' · Kontoen er skjult fra det almindelige overblik';
    $('savingsAccountHint').textContent=accountLabel+' · '+balance+hiddenNote+'. Målet følger saldoen automatisk.';
  }
}
function openSavings(goal=null){
  $('savingsForm').reset();
  $('savingsForm').dataset.id=goal?.id || '';
  $('savingsForm').dataset.scope=savingsScope();
  $('savingsDialogTitle').textContent=goal?'Rediger opsparingsmål':'Nyt opsparingsmål';
  $('savingsContext').textContent=(source==='demo'?'Demodata':'Mine bankdata')+' · '+$('currency').value;
  $('savingsAccount').innerHTML='<option value="">Ingen konto – brug manuelt beløb</option>'+savingsAccounts.map(account=>{
    const suffix=account.last4?' · •• '+esc(account.last4):'';
    const hidden=account.included?'':' · skjult i overblik';
    return '<option value="'+esc(account.id)+'">'+esc(account.name)+suffix+hidden+'</option>';
  }).join('');
  const selected=new Set(goal?.analysis_account_ids || []);
  $('savingsAnalysisAccountChoices').innerHTML=savingsAccounts.length?savingsAccounts.map(account=>{
    const suffix=account.last4?' · •• '+esc(account.last4):'';
    const hidden=account.included?'':' · skjult i overblikket';
    return '<label class="checkbox-row savings-analysis-account"><input type="checkbox" name="analysis_account_ids" value="'+esc(account.id)+'"'+(selected.has(account.id)?' checked':'')+'><span>'+esc(account.name)+suffix+hidden+'</span></label>';
  }).join(''):'<p class="small muted">Der er ingen tilgængelige konti i denne datakilde og valuta. Du kan gemme målet og vælge konti senere.</p>';
  if(goal){
    $('savingsName').value=goal.name;
    $('savingsAmount').value=formatSavingsAmount((goal.target_amount/100).toFixed(2).replace('.',','));
    $('savingsSavedAmount').value=formatSavingsAmount(((goal.manual_saved_amount ?? goal.saved_amount)/100).toFixed(2).replace('.',','));
    $('savingsAccount').value=goal.account_id || '';
    $('savingsDeadline').value=goal.deadline;
    $('savingsFeatured').checked=!!goal.featured;
  }
  updateSavingsAccountMode();
  showError('savingsFormError','');$('savingsDialog').showModal();$('savingsName').focus();
}
$('savingsPeriodForm').addEventListener('submit',async event=>{
  event.preventDefault();
  await loadSavings();
});
$('savingsAccount').addEventListener('change',updateSavingsAccountMode);

window.renderFeaturedGoal=function(goal){
  const panel=$('featuredGoalPanel');
  if(!goal){panel.hidden=true;panel.innerHTML='';return;}
  panel.hidden=false;
  const a=goal.analysis, progress=goal.target_amount?Math.max(0,Math.min(100,goal.saved_amount/goal.target_amount*100)):0;
  const [tone,label,text]=savingsStatus(goal);
  const liveAccount=goal.uses_live_balance?`<small class="featured-goal-account">${svg('bank')} Følger ${esc(goal.account_name)} live</small>`:'';
  panel.innerHTML=`<div class="featured-goal-copy"><p class="eyebrow">DIT AKTIVE MÅL</p><h2>${esc(goal.name)}</h2><p>${esc(text)}</p><button type="button" class="text-button" data-view="savings">Se analyse og forslag <span>→</span></button></div>
    <div class="featured-goal-numbers"><span>${esc(money(goal.saved_amount,goal.currency,0))} af ${esc(money(goal.target_amount,goal.currency,0))}</span>${liveAccount}<div class="progress"><div class="progress-fill" style="width:${progress}%"></div></div><strong class="${tone}">${esc(label)}</strong>${a.months_analyzed?`<small>${esc(money(a.required_monthly,goal.currency,0))}/md. nødvendigt · ${esc(money(a.average_available,goal.currency,0))}/md. historisk råderum</small>`:''}</div>`;
};
$('newSavingsGoal').addEventListener('click',()=>openSavings());
$('savingsList').addEventListener('click',async event=>{
  const button=event.target.closest('[data-edit-savings],[data-delete-savings],[data-skip-suggestion],[data-restore-suggestion]');if(!button || savingsLoadedScope!==savingsScope())return;
  const goal=savingsGoals.find(g=>g.id===Number(button.dataset.editSavings || button.dataset.deleteSavings || button.dataset.skipSuggestion || button.dataset.restoreSuggestion));if(!goal)return;
  if(button.hasAttribute('data-skip-suggestion') || button.hasAttribute('data-restore-suggestion')){
    const scope=savingsLoadedScope, restoring=button.hasAttribute('data-restore-suggestion');
    button.disabled=true;showError('savingsError','');
    try{
      await api('/api/savings-goals/'+goal.id+'/suggestion-skips?'+scope,{method:restoring?'DELETE':'PUT',body:JSON.stringify({merchant:button.dataset.merchant,month:goal.analysis.suggestion_month})});
      if(scope!==savingsScope())return;
      toast(restoring?'Forslaget er taget med igen':'Forslaget er sprunget over denne måned');
      await refresh();
    }catch(error){if(scope===savingsScope())showError('savingsError',error.message);}
    finally{button.disabled=false;}
    return;
  }
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
    values.account_id=values.account_id || null;
    values.analysis_account_ids=Array.from(form.querySelectorAll('input[name="analysis_account_ids"]:checked'),input=>input.value);
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
