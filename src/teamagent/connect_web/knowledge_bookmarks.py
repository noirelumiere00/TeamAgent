"""ブラウザ保存のブックマークと、旧 Vault hash の確認 UI。"""

BOOKMARK_PAGE = (
    '<main class="knowledge-list" id="knowledgeBookmarks"><h1>ブックマーク</h1>'
    '<p class="list-note">このブラウザのログイン利用者ごとに保存します。'
    '開くたびに閲覧権限を確認します。</p><div id="bookmarkItems" aria-live="polite">'
    "読み込み中…</div></main>"
)

BOOKMARK_STYLE = """
.bookmark-notice{padding:12px 20px;background:var(--bg-secondary);
 border-bottom:1px solid var(--border)}
.bookmark-dialog{color:var(--text);background:white;border:1px solid var(--border);
 border-radius:8px;width:min(720px,85vw);max-height:80vh;overflow:auto;padding:24px}
.bookmark-dialog::backdrop{background:#0005}
.bookmark-dialog li,.bookmark-items li{margin:12px 0;overflow-wrap:anywhere}
.bookmark-dialog button,.bookmark-notice button,.bookmark-items button{margin:4px;padding:8px 12px;
 border:1px solid var(--border);border-radius:6px;background:white;color:var(--accent);
 cursor:pointer}
.bookmark-dialog button:disabled{color:var(--muted);cursor:default}
.bookmark-dialog :focus-visible,.bookmark-items :focus-visible{outline:2px solid var(--accent)}
"""

# 動的な文字列は textContent / text node のみ。利用者識別子は HTML 側の属性から読む。
BOOKMARK_JS = r"""
(()=>{
'use strict';
const root=document.getElementById('knowledgeBookmarkRoot');
const storageKey='connect_bm:'+root.dataset.user;
const pendingKey='ailavault.pendingHash';
const missing='見つからないか、閲覧できません';
function el(tag,text){
 const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n;
}
function button(text,action){const n=el('button',text);n.type='button';n.onclick=action;return n;}
function readArray(key){
 const raw=localStorage.getItem(key);if(!raw)return [];
 if(raw.length>1000000)throw Error('保存データが大きすぎます。');
 const entries=JSON.parse(raw);
 if(!Array.isArray(entries)||entries.length>1000)throw Error('確認できるのは1000件までです。');
 return entries;
}
function stable(entry){
 if(!entry||typeof entry.id!=='string'||entry.id.length>500)return null;
 if(entry.kind==='doc'&&/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(entry.id))
  return {kind:'doc',id:entry.id.toLowerCase()};
 if(entry.kind==='client'&&entry.id.trim()&&!/[\x00-\x1f\x7f]/.test(entry.id))
  return {kind:'client',id:entry.id};
 return null;
}
function readBookmarks(){
 const entries=readArray(storageKey);
 if(entries.some(e=>!stable(e)))throw Error('保存データの形式が正しくありません。');
 return entries.map(stable);
}
function legacyEntry(key){
 if(typeof key!=='string'||!/^([cd]):./s.test(key))return null;
 const value=key.slice(2);
 if(!value.trim()||value.length>500||/\p{C}/u.test(value))return null;
 if(key[0]==='d'&&(value.length>128||/[\\/:*?"<>|\[\]#^]/.test(value)
  ||value==='.'||value==='..'))return null;
 return {kind:key[0]==='c'?'client':'doc',value,legacy:true};
}
function hashEntry(hash){
 const match=/^#?(client|doc|open):(.+)$/.exec(hash);if(!match)return null;
 try{return {kind:match[1],value:decodeURIComponent(match[2]),legacy:true};}catch(e){return null;}
}
function panel(title){
 const d=el('dialog');d.className='bookmark-dialog';d.setAttribute('aria-label',title);
 d.append(el('h2',title),button('閉じる',()=>{d.close();d.remove();}));
 document.body.append(d);d.showModal();return d;
}
function notice(text){root.replaceChildren(el('p',text));root.hidden=false;}
async function resolve(entries){
 const results=[];
 for(let i=0;i<entries.length;i+=100){
  const response=await fetch('/api/v1/knowledge/resolve',{
   method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(entries.slice(i,i+100))});
  if(response.status===401){
   // hash は login ページに渡す。既存の login JS が同じキーに退避する。
   let hash=location.hash;
   try{if(!hash)hash=sessionStorage.getItem(pendingKey)||'';}catch(e){}
   if(hash)hash='#'+hash.replace(/^#/,'');
   location.assign('/search/login?next='+encodeURIComponent('/search?view=bookmarks')+hash);
   throw Error('ログインし直してください。');
  }
  if(!response.ok)throw Error('取得できませんでした。時間をおいて再度お試しください。');
  const data=await response.json();results.push(...data.results);
 }
 return results;
}
function destination(candidate){
 const s=stable(candidate);if(!s)throw Error('リンクの形式が正しくありません。');
 return s.kind==='doc'?'/search?view=doc&id='+encodeURIComponent(s.id)
  :'/search/client/'+encodeURIComponent(s.id);
}
function clearHash(){
 try{sessionStorage.removeItem(pendingKey);}catch(e){}
 history.replaceState(null,'',location.pathname+location.search);
}
function navigate(candidate){const url=destination(candidate);clearHash();location.assign(url);}
function preview(candidate){
 const a=el('a',' 詳細を確認 ↗');a.href=destination(candidate);
 a.target='_blank';a.rel='noopener';return a;
}
function choices(container,result,onChoose){
 if(result.status==='missing'){container.append(el('p',missing));return;}
 if(result.status==='ambiguous')container.append(el('p',result.candidates.length>1
  ?'候補が複数あります。選んでください。'
  :'旧名の付番は復元できません。候補を確認して選んでください。'));
 result.candidates.forEach(c=>{
  const row=el('div'),id=c.kind==='doc'?' (ID: '+c.id.slice(0,8)+')':'';
  row.append(button((c.kind==='client'?'取引先: ':'資料: ')+c.title+id,()=>onChoose(c)),
   preview(c));container.append(row);
 });
}
let hashSequence=0;
async function applyHash(){
 const sequence=++hashSequence;
 let hash=location.hash;try{if(!hash)hash=sessionStorage.getItem(pendingKey)||'';}catch(e){}
 if(!hash)return;
 const entry=hashEntry(hash);
 if(!entry){notice('旧リンクの形式が正しくありません。');clearHash();return;}
 try{
  const [result]=await resolve([entry]);if(sequence!==hashSequence)return;
  if(result.status==='resolved'){navigate(result.candidates[0]);return;}
  clearHash();const d=panel('旧リンクを開く');choices(d,result,navigate);
 }catch(e){notice(e.message);}
}
async function openSaved(candidate){
 try{
  const s=stable(candidate);if(!s)throw Error('リンクの形式が正しくありません。');
  const [result]=await resolve([{kind:s.kind,value:s.id,legacy:false}]);
  if(result.status==='resolved')navigate(result.candidates[0]);
  else {const d=panel('ブックマークを開く');choices(d,result,navigate);}
 }catch(e){notice(e.message);}
}
async function renderBookmarks(){
 const target=document.getElementById('bookmarkItems');if(!target)return;
 try{
  const entries=readBookmarks();target.replaceChildren();
  if(!entries.length){target.append(el('p','ブックマークはまだありません。'));return;}
  const results=await resolve(entries.map(e=>({kind:e.kind,value:e.id,legacy:false})));
  const list=el('ul');list.className='bookmark-items';target.append(list);
  results.forEach((result,i)=>{
   const row=el('li');list.append(row);
   if(result.status==='resolved')row.append(button(result.candidates[0].title,()=>openSaved(entries[i])));
   else row.append(el('span',missing));
  });
 }catch(e){target.replaceChildren(el('p',e.message));}
}
async function importBookmarks(keys){
 const d=panel('この端末の /app で付けたブックマーク');
 d.append(el('p','別の人がこの端末で付けたものが含まれる場合があります。取り込む項目を自分で確認してください。'));
 const status=el('p','確認中…');status.setAttribute('role','status');d.append(status);
 const selections=[],confirmed=el('input');confirmed.type='checkbox';
 const consent=el('label');consent.append(confirmed,document.createTextNode(
  '選んだ項目は自分のブックマークとして取り込んでよいことを確認しました。'));
 const save=button('チェックした項目を取り込む',()=>{
  if(!confirmed.checked)return;
  try{
   const checked=selections.filter(s=>s.checkbox.checked&&s.candidate).map(s=>stable(s.candidate));
   if(!checked.length){status.textContent='取り込む項目をチェックしてください。';return;}
   // 保存は安定IDだけ。旧キーは変更せず、既存の新ブックマークと併合する。
   const merged=new Map(readBookmarks().map(e=>[e.kind+':'+e.id,e]));
   checked.forEach(e=>merged.set(e.kind+':'+e.id,e));
   if(merged.size>1000)throw Error('保存できるのは1000件までです。');
   localStorage.setItem(storageKey,JSON.stringify([...merged.values()]));
   save.disabled=true;status.textContent=checked.length+' 件取り込みました。';
   void renderBookmarks();
  }catch(e){status.textContent=e.message;}
 });save.disabled=true;confirmed.onchange=()=>{save.disabled=!confirmed.checked;};
 try{
  const entries=keys.map(legacyEntry),valid=entries.filter(Boolean);
  const results=valid.length?await resolve(valid):[];let index=0;
  const list=el('ul');d.append(list);
  keys.forEach((key,i)=>{
   const row=el('li'),checkbox=el('input');checkbox.type='checkbox';checkbox.disabled=true;
   const label=el('label');label.append(checkbox,document.createTextNode(String(key)));
   row.append(label);list.append(row);
   const selection={checkbox,candidate:null};selections.push(selection);
   if(!entries[i]){row.append(el('p','この形式・種類の旧ブックマークは取り込めません。'));return;}
   const result=results[index++];
   if(result.status==='resolved'){
    selection.candidate=result.candidates[0];checkbox.disabled=false;
    row.append(el('p','取り込めます: '+selection.candidate.title));
    row.append(preview(selection.candidate));
   }else choices(row,result,c=>{
    selection.candidate=c;checkbox.disabled=false;checkbox.checked=false;
    let selected=row.querySelector('[data-selected]');
    if(!selected){selected=el('p');selected.dataset.selected='true';row.append(selected);}
    selected.textContent='選択中: '+c.title+'（取り込みには左のチェックも必要です）';
   });
  });
  status.textContent='取り込む項目をチェックし、本人確認にチェックしてください。';d.append(consent,save);
 }catch(e){status.textContent=e.message;}
}
async function boot(){
 try{
  const keys=readArray('aila_bm');
  if(keys.length){
   notice('/app で付けたブックマークが '+keys.length+' 件あります。確認して取り込みますか？');
   root.append(button('確認して取り込む',()=>importBookmarks(keys)),button('あとで',()=>{root.hidden=true;}));}
 }catch(e){notice('旧ブックマークを読み取れませんでした。'+e.message);}
 await renderBookmarks();await applyHash();
}
window.addEventListener('hashchange',()=>{void applyHash();});
void boot();
})();
"""
