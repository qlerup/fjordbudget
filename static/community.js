'use strict';
let communityState=null,communityTimer=null,communitySaving=false;
let communityLastSeenSync=null,communityProgressRunning=false;
function renderCommunity(data,resetChoice=false){
  communityState=data;
  if(resetChoice)$('communityEnabled').checked=data.enabled;
  $('communityEnabled').disabled=!data.configured && !data.enabled;
  $('communitySave').disabled=communitySaving || (!data.configured && !data.enabled);
  $('communitySync').disabled=communitySaving || !data.enabled || data.running;
  $('communityPreviewCount').textContent=`Se ${data.eligible} banktekster, der kan blive delt`;
  $('communityPreview').innerHTML=data.preview.map(item=>`<tr><td>${esc(item.bank_text)}</td><td>${esc(item.merchant)}</td></tr>`).join('') || '<tr><td colspan="2">Ingen egnede, gemte kortkøb endnu.</td></tr>';
  $('communityExcluded').textContent=`${data.excluded} gemte tekster udelades af filteret.${data.eligible>100?' Forhåndsvisningen viser de første 100.':''}`;
  $('communityState').textContent=!data.configured?'Fællesserveren er ikke sat op på denne installation.':data.enabled?`Deling er slået til · ${data.cached} fælles regler hentet · ${data.shared} egne tekster sendt · ${data.awaiting_review} afventer godkendelse${data.upload_remaining?` · ${data.upload_remaining} tekster mangler at blive sendt, før fælles regler hentes`:''}${data.running?' · Synkroniserer …':''}`:data.revoke_pending?'Deling er slået fra lokalt. Serveradgangen tilbagekaldes, når serveren kan kontaktes.':'Deling er slået fra.';
  $('communityServer').textContent=data.server?'Server: '+data.server:'';
  $('communityLastSync').textContent=data.last_sync?'Senest synkroniseret '+new Date(data.last_sync).toLocaleString('da-DK'):'';
  showError('communityError',data.error || '');
}
async function loadCommunity(resetChoice=false){
  const data=await api('/api/community');
  const changed=communityLastSeenSync!==data.last_sync;
  communityLastSeenSync=data.last_sync;renderCommunity(data,resetChoice);
  if(changed && !resetChoice)await refresh();
}
async function watchCommunityProgress(){
  if(!config || source!=='live' || document.hidden || communitySaving || communityProgressRunning)return;
  communityProgressRunning=true;
  try{
    const data=await api('/api/community/progress');
    if(data.last_sync!==communityLastSeenSync){communityLastSeenSync=data.last_sync;await refresh();}
  }catch(error){console.debug('Fælles navne kontrolleres igen ved næste opdatering.');}
  finally{communityProgressRunning=false;}
}
setInterval(watchCommunityProgress,60000);
$('openSettings').addEventListener('click',async()=>{
  $('communityDialog').showModal();$('communityLoading').hidden=false;$('communityForm').hidden=true;showError('communityError','');
  try{await loadCommunity(true);$('communityForm').hidden=false;$('communityDialog').scrollTop=0;
    communityTimer=setInterval(()=>{if(!communitySaving)loadCommunity().catch(error=>showError('communityError',error.message));},5000);
  }catch(error){showError('communityError',error.message);}finally{$('communityLoading').hidden=true;}
});
$('communityDialog').addEventListener('close',()=>{clearInterval(communityTimer);});
$('communityForm').addEventListener('submit',async event=>{
  event.preventDefault();if(communitySaving || !communityState)return;
  communitySaving=true;const enabled=$('communityEnabled').checked;
  $('communitySave').disabled=true;$('communityEnabled').disabled=true;$('communitySync').disabled=true;
  try{renderCommunity(await api('/api/community',{method:'PUT',body:JSON.stringify({enabled,consent_version:communityState.consent_version})}),true);toast(enabled?'Deling er slået til. Synkroniseringen starter nu.':'Deling er slået fra.');await refresh();}
  catch(error){showError('communityError',error.message);}
  finally{communitySaving=false;$('communitySave').disabled=!communityState.configured && !communityState.enabled;$('communityEnabled').disabled=!communityState.configured && !communityState.enabled;$('communitySync').disabled=!communityState.enabled;}
});
$('communitySync').addEventListener('click',async()=>{
  $('communitySync').disabled=true;
  try{await api('/api/community/sync',{method:'POST',body:'{}'});toast('Synkroniseringen er sat i gang.');await loadCommunity();}
  catch(error){showError('communityError',error.message);$('communitySync').disabled=false;}
});
