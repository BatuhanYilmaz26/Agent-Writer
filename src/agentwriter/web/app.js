// The preview creates DOM nodes with textContent. Untrusted Markdown is never HTML.
const $ = id => document.getElementById(id);
let csrf = '', active = null, article = null, stream = null, poll = null, nextCursor = null;
let ledger = new Map();
const terminal = new Set(['succeeded', 'needs_review', 'failed', 'cancelled']);
const labels = {queued:'Queued',running:'Writing',retry_wait:'Waiting to retry',cancel_requested:'Cancelling',succeeded:'Generated',needs_review:'Needs review',failed:'Could not finish',cancelled:'Cancelled'};

async function api(path, options = {}) {
  const response = await fetch(path, {...options, headers:{'Content-Type':'application/json', 'X-CSRF-Token':csrf, ...(options.headers || {})}});
  if (!response.ok) {
    const error = await response.json().catch(() => ({message:'The service could not be reached.'}));
    throw new Error(error.message || 'Please try again.');
  }
  return response.status === 204 ? null : response.json();
}
function showError(error) { $('error').textContent = error.message || String(error); $('error').hidden = false; }
function clearError() { $('error').hidden = true; }
function stopObserving() { stream?.close(); stream=null; clearInterval(poll); poll=null; }

function inline(node, text) {
  for (const part of text.split(/(\[@[A-Za-z][A-Za-z0-9_.:-]{0,79}\])/g)) {
    const match = part.match(/^\[@(.+)\]$/);
    const source = match && ledger.get(match[1]);
    if (source) {
      const link = document.createElement('a');
      const url = new URL(source.url);
      if (!['http:','https:'].includes(url.protocol)) { node.append(document.createTextNode(part)); continue; }
      link.href = url.href; link.textContent = `[${source.title}]`; link.target='_blank';link.rel='noopener noreferrer';node.append(link);
    } else node.append(document.createTextNode(part));
  }
}
export function renderMarkdown(root, text) {
  root.replaceChildren(); let code = null;
  for (const line of text.split('\n')) {
    if (line.startsWith('```')) { if (code) {code=null;} else {const pre=document.createElement('pre');code=document.createElement('code');pre.append(code);root.append(pre);} continue; }
    if (code) {code.append(document.createTextNode(line+'\n'));continue;}
    if (!line.trim()) continue;
    const heading=line.match(/^(#{1,6})\s+(.*)$/), bullet=line.match(/^[-*]\s+(.*)$/);
    const element=document.createElement(heading ? `h${heading[1].length}` : bullet ? 'p' : 'p');
    inline(element,heading ? heading[2] : bullet ? `• ${bullet[1]}` : line);root.append(element);
  }
}
async function refreshHistory(append=false) {
  const data=await api('/api/v1/runs?limit=20'+(append&&nextCursor?'&before='+encodeURIComponent(nextCursor):''));
  if (!append) $('history').replaceChildren();
  if (!append && !data.runs.length) $('history').textContent='Your saved runs will appear here.';
  for (const run of data.runs) {
    const li=document.createElement('li'), button=document.createElement('button'), meta=document.createElement('small'), state=document.createElement('span');
    button.type='button';button.textContent=run.request.topic;button.onclick=()=>selectRun(run.id).catch(showError);
    meta.textContent=new Date(run.accepted_at).toLocaleString();button.append(meta);
    state.className='label';state.textContent=labels[run.state] || run.state;li.append(button,state);$('history').append(li);
  }
  nextCursor=data.next_cursor;$('more').hidden=!nextCursor;
}
async function loadArticle() {
  const [result,evidence,versions]=await Promise.all([api(`/api/v1/runs/${active}/article`),api(`/api/v1/runs/${active}/sources`),api(`/api/v1/runs/${active}/revisions`)]);
  article=result;ledger=new Map(evidence.sources.map(source=>[source.id,source]));
  const editable=result.citation_map?.editable_markdown || result.markdown;
  $('editor').value=editable;renderMarkdown($('preview'),editable);$('article-view').hidden=false;
  $('review-status').hidden=false;$('review-status').textContent=result.review_status==='approved'?'Generated revision passed its configured checks. Please review before publishing.':result.review_status==='unassessed'?'Your edited revision has not been assessed by a model.':'This draft needs your review. Check its evidence and limitations.';
  $('issues').replaceChildren();for (const issue of result.issues || []) {const li=document.createElement('li');li.textContent=issue.explanation || issue.code;$('issues').append(li);}
  $('sources').replaceChildren();for (const source of evidence.sources) {const li=document.createElement('li'), a=document.createElement('a'),note=document.createElement('small');a.href=source.url;a.textContent=source.title;a.target='_blank';a.rel='noopener noreferrer';note.textContent=`${source.provider} · retrieved ${source.retrieved_at}`;li.append(a,note);$('sources').append(li);}
  if (!evidence.sources.length) $('sources').textContent='No external sources were used for this draft.';
  $('revision').replaceChildren();for(const [index,version] of versions.revisions.entries()){const option=document.createElement('option');option.value=version.id;option.textContent=`${index===0?'Latest · ':''}${version.author_type==='human'?'Your edit':'Generated'} · ${version.review_status} · ${new Date(version.created_at).toLocaleString()}`;$('revision').append(option);}
  $('revision').value=result.id;setDownload(result.id);
}
function setDownload(id){$('download').href=`/api/v1/runs/${active}/revisions/${id}/download`;}
async function refreshRun() {
  if(!active)return;const run=await api(`/api/v1/runs/${active}`);$('state').textContent=labels[run.state]||run.state;
  $('cancel').hidden=terminal.has(run.state);$('progress').textContent=run.error_message || (terminal.has(run.state)?'Run saved. You can return to it from history.':'Your run continues even if you close this page.');
  if(terminal.has(run.state)){stopObserving();if(run.current_revision)await loadArticle();await refreshHistory();}
  return run;
}
async function selectRun(id) {
  stopObserving();clearError();active=id;article=null;localStorage.setItem('agentwriter.run',id);
  $('empty').hidden=true;$('run-view').hidden=false;$('article-view').hidden=true;$('review-status').hidden=true;$('issues').replaceChildren();
  const run=await refreshRun();if(!run||terminal.has(run.state))return;
  let sequence=0;stream=new EventSource(`/api/v1/runs/${id}/events`);
  const onEvent=event=>{const data=JSON.parse(event.data);if(data.sequence<=sequence)return;sequence=data.sequence;$('progress').textContent=event.type.replaceAll('_',' ');if(['completed','failed','cancelled'].includes(event.type))refreshRun().catch(showError);};
  for(const name of ['queued','running','plan_completed','research_completed','source_added','section_completed','review_completed','retry_wait','cancel_requested','completed','failed','cancelled'])stream.addEventListener(name,onEvent);
  stream.addEventListener('stream_error',()=>{stopObserving();refreshRun().catch(showError);});
  poll=setInterval(()=>refreshRun().catch(showError),3000);
}
$('brief-form').addEventListener('submit',async event=>{event.preventDefault();clearError();$('submit').disabled=true;try{const data=Object.fromEntries(new FormData(event.target));data.target_words=Number(data.target_words);data.language='en';const result=await api('/api/v1/runs',{method:'POST',headers:{'Idempotency-Key':crypto.randomUUID()},body:JSON.stringify(data)});await refreshHistory();await selectRun(result.id);}catch(error){showError(error);}finally{$('submit').disabled=false;}});
$('cancel').onclick=async()=>{try{await api(`/api/v1/runs/${active}/cancel`,{method:'POST'});await refreshRun();}catch(error){showError(error);}};
$('delete').onclick=async()=>{try{await api(`/api/v1/runs/${active}`,{method:'DELETE'});stopObserving();active=null;localStorage.removeItem('agentwriter.run');$('run-view').hidden=true;$('empty').hidden=false;$('state').textContent='No run selected';await refreshHistory();}catch(error){showError(error);}};
function editing(enabled){$('editor').hidden=!enabled;$('editor-label').hidden=!enabled;$('preview').hidden=enabled;$('save').hidden=!enabled;$('preview-tab').setAttribute('aria-pressed',String(!enabled));$('edit-tab').setAttribute('aria-pressed',String(enabled));}
$('preview-tab').onclick=()=>{renderMarkdown($('preview'),$('editor').value);editing(false);};$('edit-tab').onclick=()=>editing(true);
$('save').onclick=async()=>{try{clearError();await api(`/api/v1/runs/${active}/revisions`,{method:'POST',body:JSON.stringify({parent_revision:article.id,markdown:$('editor').value})});await loadArticle();editing(false);}catch(error){showError(error);}};
$('revision').onchange=()=>setDownload($('revision').value);$('refresh').onclick=()=>refreshHistory().catch(showError);$('more').onclick=()=>refreshHistory(true).catch(showError);
async function start(){const session=await api('/api/v1/session');csrf=session.csrf_token;$('mode').textContent=session.fixture?'Local fixture profile':'Local profile';await refreshHistory();const saved=localStorage.getItem('agentwriter.run');if(saved)await selectRun(saved);}
start().catch(showError);
