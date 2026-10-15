"""実際に配信するJSでクリック展開・上限・カードのXSS対策を検証する。"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from teamagent.connect_web.search_graph import graph_page
from tests.connect_web.test_search_graph import uid


@pytest.mark.skipif(shutil.which("node") is None, reason="node が必要")
def test_canvas_expand_dedup_cap_and_safe_card() -> None:
    page = graph_page("<script>email</script>", [uid(1)])
    assert "<script>email</script>" not in page
    script = re.search(r"<script>([\s\S]*)</script>", page)
    assert script
    harness = r"""
const assert=require('node:assert/strict');
const elements=new Map();
function element(){return {style:{},children:[],classList:{add(){},remove(){},toggle(){}},
  appendChild(n){this.children.push(n);},addEventListener(){},
  getBoundingClientRect(){return {width:800,height:600,left:0,top:0};},
  getContext(){return {setTransform(){}}},setAttribute(){}};}
global.document={getElementById(id){if(!elements.has(id))elements.set(id,element());return elements.get(id);},
  createElement:element};
global.window={__shellMode:true,addEventListener(){},devicePixelRatio:1};
global.location={};global.requestAnimationFrame=()=>{};global.setTimeout=()=>{};
let urls=[];
global.fetch=async url=>{urls.push(url);if(url.startsWith('/api/v1/graph/seed'))return {ok:true,status:200,json:async()=>({nodes:[{id:'0',seed:true,title:'<script>alert(1)</script>',project:'A/B<script>',url:'https://example.test/0'}],edges:[]})};return {ok:true,status:200,json:async()=>({
  nodes:Array.from({length:250},(_,i)=>({id:String(i),title:'<script>alert(1)</script>',
      project:'A/B<script>',url:'https://example.test/'+i})),
  edges:[{source:'0',target:'1',reason:'project:A'}]
})};};
__SCRIPT__
(async()=>{
  await load();
  assert.equal(urls[0],'/api/v1/graph/seed?ids='+SEEDS);
  assert.equal(nodes.length,1);urls=[];
  await expandNode(nodes[0]);
  assert.equal(nodes.length,200);
  assert.equal(new Set(nodes.map(n=>n.id)).size,200);
  assert.equal(nodes.find(n=>n.id==='0').seed,true);
  assert.equal(edges.length,1);
  await expandNode(nodes[0]);
  assert.equal(nodes.length,200);assert.equal(edges.length,1);
  assert.equal(urls[0],'/api/v1/graph/neighbors?id=0');
  renderNodeCard(nodes[0]);
  const card=elements.get('ncard');
  assert.ok(card.children.some(n=>n.textContent==='<script>alert(1)</script>'));
  assert.ok(card.children.some(n=>n.href==='/search/client/A%2FB%3Cscript%3E'));
  assert.ok(card.children.some(n=>n.textContent==='出典を開く ↗'));
  assert.ok(card.children.every(n=>n.innerHTML===undefined));
  setFocus(nodes[0]);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(urls.length,3);
  global.fetch=async()=>({ok:false,status:500});
  await expandNode(nodes[0]);
  assert.equal(nodes.length,200);assert.equal(expanding,false);
  console.log('canvas expansion verified');
})().catch(e=>{console.error(e);process.exitCode=1;});
""".replace("__SCRIPT__", script.group(1))
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
