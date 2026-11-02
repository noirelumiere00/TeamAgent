"""配信する JS で旧 hash・本人確認・安定 ID 保存を検証する。"""

from __future__ import annotations

import shutil
import subprocess

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from teamagent.connect_web.app import create_app
from teamagent.connect_web.knowledge_bookmarks import BOOKMARK_JS
from tests.connect_web.test_search_routes import _config


@pytest.mark.skipif(shutil.which("node") is None, reason="node が必要")
def test_knowledge_bookmarks_hash_import_consent_and_storage() -> None:
    login = TestClient(create_app(search_config=_config())).get(
        "/search/login?next=%2Fsearch%3Fview%3Dbookmarks"
    )
    login_script = BeautifulSoup(login.text, "html.parser").select_one("script:not([src])").text
    harness = r"""
const assert=require('node:assert/strict');
class Element {
 constructor(tag){this.tag=tag;this.children=[];this.dataset={};this.attrs={};this._text='';}
 set textContent(value){this._text=value;this.children=[];}
 get textContent(){return this._text+this.children.map(c=>c.textContent).join('');}
 append(...nodes){this.children.push(...nodes);}
 replaceChildren(...nodes){this._text='';this.children=nodes;}
 setAttribute(key,value){this.attrs[key]=value;}
 showModal(){this.open=true;}
 close(){this.open=false;}
 remove(){this.removed=true;}
 querySelector(){return this.children.find(c=>c.dataset.selected)||null;}
}
const body=new Element('body'),root=new Element('div');root.dataset.user='b@example.com';
const elements=new Map([['knowledgeBookmarkRoot',root]]);
global.document={body,getElementById:id=>elements.get(id)||null,
 createElement:tag=>new Element(tag),createTextNode:text=>{const n=new Element('text');n.textContent=text;return n;}};
let callbacks={};global.window={addEventListener:(name,fn)=>callbacks[name]=fn};
const old=['c:取引先','d:候補','r:研究'];
const local=new Map([['aila_bm',JSON.stringify(old)]]),session=new Map();let writes=[];
global.localStorage={getItem:k=>local.get(k)||null,setItem:(k,v)=>{writes.push([k,v]);local.set(k,v);}};
global.sessionStorage={getItem:k=>session.get(k)||null,setItem:(k,v)=>session.set(k,v),removeItem:k=>session.delete(k)};
let navigation=[];global.location={hash:'',pathname:'/search',search:'',assign:url=>navigation.push(url)};
global.history={replaceState:()=>{location.hash='';}};
const uuid='12345678-1234-1234-1234-123456789abc';
const doc={kind:'doc',id:uuid,title:'<img src=x onerror=alert(1)>'};
const company={kind:'client',id:'取引先 / <test>',title:'取引先 / <test>'};
const missing={status:'missing',candidates:[]};
let calls=[],responseStatus=200;
global.fetch=async(url,options)=>{
 assert.equal(url,'/api/v1/knowledge/resolve');const entries=JSON.parse(options.body);calls.push(entries);
 return {ok:responseStatus===200,status:responseStatus,json:async()=>({results:entries.map(e=>
  e.value==='候補'?{status:'ambiguous',candidates:[doc,company]}:
  e.value==='missing'?missing:{status:'resolved',candidates:[e.kind==='client'?company:doc]})})};
};
__SCRIPT__
function all(node,tag){return node.children.flatMap(n=>[...(n.tag===tag?[n]:[]),...all(n,tag)]);}
function dialog(){return body.children.filter(c=>c.tag==='dialog').at(-1);}
(async()=>{
 const t=global.knowledgeTest;
 await t.boot();
 assert.equal(calls.length,0);assert.equal(writes.length,0);
 assert.ok(root.textContent.includes('3 件'));assert.ok(root.textContent.includes('確認して取り込み'));
 await t.importBookmarks(old);
 let d=dialog(),inputs=all(d,'input'),buttons=all(d,'button');
 assert.ok(d.textContent.includes('この端末の /app'));assert.ok(d.textContent.includes('別の人'));
 assert.equal(inputs.length,4);assert.ok(inputs.every(n=>!n.checked));
 const [clientCheck,ambiguousCheck,unsupportedCheck,consent]=inputs;
 assert.equal(clientCheck.disabled,false);assert.equal(ambiguousCheck.disabled,true);
 assert.equal(unsupportedCheck.disabled,true);
 const save=buttons.find(n=>n.textContent==='チェックした項目を取り込む');
 assert.equal(save.disabled,true);save.onclick();assert.equal(writes.length,0);
 clientCheck.checked=true;save.onclick();assert.equal(writes.length,0);
 const choice=buttons.find(n=>n.textContent.startsWith('資料: '));choice.onclick();
 assert.equal(ambiguousCheck.disabled,false);assert.equal(ambiguousCheck.checked,false);
 assert.ok(d.textContent.includes(doc.title));
 assert.ok(all(d,'img').length===0);assert.ok(all(d,'script').length===0);
 assert.ok(all(d,'button').every(n=>n.innerHTML===undefined));
 consent.checked=true;consent.onchange();assert.equal(save.disabled,false);save.onclick();
 assert.equal(writes.length,1);assert.equal(writes[0][0],'connect_bm:b@example.com');
 assert.deepEqual(JSON.parse(writes[0][1]),[{kind:'client',id:company.id}]);
 assert.equal(local.get('aila_bm'),JSON.stringify(old));
 assert.ok(!writes[0][1].includes('title'));assert.ok(!writes[0][1].includes('content'));
 // 別利用者の保存キーは読み込まない。保存リストは毎回RLS照合し、クリックでも再照合する。
 local.set('connect_bm:a@example.com',JSON.stringify([{kind:'doc',id:uuid}]));
 const target=new Element('div');elements.set('bookmarkItems',target);
 let before=calls.length;await t.renderBookmarks();assert.equal(calls.length,before+1);
 assert.deepEqual(calls.at(-1),[{kind:'client',value:company.id,legacy:false}]);
 await all(target,'button')[0].onclick();assert.equal(calls.length,before+2);
 assert.equal(navigation.at(-1),'/search/client/'+encodeURIComponent(company.id));
 // hash の URL decode と pendingHash 復帰、正常解決後の消去。
 navigation=[];location.hash='#doc:'+encodeURIComponent('文書 / 名');
 await t.applyHash();assert.equal(calls.at(-1)[0].value,'文書 / 名');
 assert.equal(navigation.at(-1),'/search?view=doc&id='+uuid);
 location.hash='#client:'+encodeURIComponent('取引先');
 __LOGIN_SCRIPT__
 assert.equal(session.get('ailavault.pendingHash'),location.hash);location.hash='';
 await t.applyHash();assert.equal(session.has('ailavault.pendingHash'),false);
 assert.equal(navigation.at(-1),'/search/client/'+encodeURIComponent(company.id));
 // 曖昧候補は自動選択しない。見えない対象は不存在と同じ表示。
 navigation=[];location.hash='#open:'+encodeURIComponent('候補');
 await t.applyHash();assert.equal(navigation.length,0);
 d=dialog();assert.ok(d.textContent.includes('候補が複数'));
 all(d,'button').find(n=>n.textContent.startsWith('資料: ')).onclick();
 assert.equal(navigation.length,1);
 location.hash='#doc:missing';await t.applyHash();
 assert.ok(dialog().textContent.includes('見つからないか、閲覧できません'));
 // 取得失敗はnot foundにせず、復帰用hashを保持する。
 session.set('ailavault.pendingHash','#doc:retry');responseStatus=503;location.hash='';
 await t.applyHash();assert.equal(session.get('ailavault.pendingHash'),'#doc:retry');
 assert.ok(root.textContent.includes('取得できませんでした'));
 before=writes.length;await t.importBookmarks(old);
 assert.equal(writes.length,before);assert.ok(dialog().textContent.includes('取得できませんでした'));
 responseStatus=401;await t.applyHash();
 assert.ok(navigation.at(-1).endsWith('#doc:retry'));
 assert.ok(navigation.at(-1).startsWith('/search/login?next='));
 responseStatus=200;session.clear();location.hash='#doc:%ZZ';before=calls.length;
 await t.applyHash();assert.equal(calls.length,before);assert.ok(root.textContent.includes('形式'));
 assert.equal(t.hashEntry('#external:x'),null);
 assert.equal(t.legacyEntry('d:../bad'),null);
 assert.equal(t.legacyEntry('c:'+'x'.repeat(501)),null);
 assert.equal(t.legacyEntry('d:bad\nname'),null);
 // 書込失敗時は成功を表示せず、旧データを残す。
 await t.importBookmarks(['d:資料']);d=dialog();inputs=all(d,'input');
 inputs[0].checked=true;inputs[1].checked=true;
 localStorage.setItem=()=>{throw Error('保存できません');};
 all(d,'button').find(n=>n.textContent==='チェックした項目を取り込む').onclick();
 assert.ok(d.textContent.includes('保存できません'));assert.ok(!d.textContent.includes('件取り込みました'));
 assert.equal(local.get('aila_bm'),JSON.stringify(old));
 console.log('hash, pendingHash, consent, RLS recheck and stable-ID storage verified');
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    script = BOOKMARK_JS.replace(
        "void boot();",
        "globalThis.knowledgeTest={boot,importBookmarks,renderBookmarks,applyHash,hashEntry,legacyEntry};",
    )
    result = subprocess.run(
        [
            "node",
            "-e",
            harness.replace("__SCRIPT__", script).replace("__LOGIN_SCRIPT__", login_script),
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
