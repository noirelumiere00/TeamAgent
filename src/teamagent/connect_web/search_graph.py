"""検索結果の関係図だけに既存canvasエンジンの展開操作を足す。"""

from __future__ import annotations

import html

_EXPAND_JS = r"""
let graphData={nodes:[],edges:[]}, expanding=false;
async function expandNode(n){
  if(expanding)return;
  expanding=true;
  try{
    const resp=await fetch('/api/v1/graph/neighbors?id='+encodeURIComponent(n.id));
    if(!resp.ok)throw new Error('graph_failed');
    const data=await resp.json();
    const byId=new Map(graphData.nodes.map(n=>[n.id,n]));
    for(const item of data.nodes||[]){
      if(!byId.has(item.id)&&byId.size<200)byId.set(item.id,{...item,seed:false});
    }
    const pairs=new Map(graphData.edges.map(e=>[[e.source,e.target].sort().join(','),e]));
    for(const e of data.edges||[]){
      if(byId.has(e.source)&&byId.has(e.target))pairs.set([e.source,e.target].sort().join(','),e);
    }
    graphData={nodes:[...byId.values()],edges:[...pairs.values()]};
    await load(graphData);
    const focused=nodes.find(item=>item.id===n.id);
    if(focused){focusNode=focused;renderNodeCard(focused);applyLocal();}
  }catch(e){
    statusEl.style.display='';statusEl.textContent='関連資料の取得に失敗しました。';
  }finally{expanding=false;}
}
"""


def graph_page(email: str, ids: list[str]) -> str:
    """既存の描画を新ページにだけ流用する。種は検証済UUIDをJSエスケープして渡す。"""
    from teamagent.connect_web.app import _GRAPH_DOM, _GRAPH_JS, _GRAPH_STYLE, _ROOT_TOKENS, _js_str

    js = _GRAPH_JS
    js = js.replace("async function load(){", "async function load(incoming){")
    js = js.replace(
        "  let data;", "  let data=incoming;\n  const previous=new Map(nodes.map(n=>[n.id,n]));"
    )
    js = js.replace(
        "  try{\n    const resp=await fetch('/api/v1/graph');",
        "  if(!data)try{\n    const resp=await fetch('/api/v1/graph/seed?ids='+SEEDS);"
        "\n    if(!resp.ok)throw new Error('graph_failed');",
    )
    js = js.replace(
        "  const ns=(data.nodes||[]);", "  graphData=data;\n  const ns=(data.nodes||[]);"
    )
    js = js.replace(
        "  view={x:0,y:0,scale:1};target={x:0,y:0,scale:1};panVel={x:0,y:0};",
        "  if(!incoming){view={x:0,y:0,scale:1};target={x:0,y:0,scale:1};panVel={x:0,y:0};}",
    )
    js = js.replace("dirty=true;pendingFit=true;", "dirty=true;pendingFit=!incoming;")
    js = js.replace("const o={id:n.id", "const o={seed:n.seed,id:n.id")
    js = js.replace(
        "    o.ckey=ckeyOf(o);",
        "    const old=previous.get(n.id);if(old){o.x=old.x;o.y=old.y;}\n    o.ckey=ckeyOf(o);",
    )
    js = js.replace(
        "    if(n===emph){",
        "    if(n.seed){ctx.lineWidth=3/view.scale;ctx.strokeStyle='#ffd166';ctx.stroke();}"
        "\n    if(n===emph){",
    )
    js = js.replace("  focusNode=n;flyTo(n);", "  expandNode(n);focusNode=n;flyTo(n);")
    js = js.replace(
        "  const r=cv.getBoundingClientRect();\n  let lx=",
        """  const client=n.client_name||n.project;
  if(client){
    const a=document.createElement('a');a.className='nopen';
    a.textContent='取引先のカルテを開く';
    a.href='/search/client/'+encodeURIComponent(client);ncard.appendChild(a);
  }
  const r=cv.getBoundingClientRect();
  let lx=""",
    )
    # 新しい関数も同じscriptに入れて描画状態を共有する。既存ページのJSは変更しない。
    js = js.replace(
        "async function load(incoming){", _EXPAND_JS + "\nasync function load(incoming){"
    )
    return (
        '<!doctype html><html lang="ja"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>Aico Vault 関係図</title><style>"
        + _ROOT_TOKENS
        + _GRAPH_STYLE
        + "</style></head><body><header>Aico Vault 関係図 — "
        + html.escape(email)
        + ' <a href="/search">検索に戻る</a></header>'
        + _GRAPH_DOM
        + "<script>const SEEDS="
        + _js_str(",".join(ids))
        + ";"
        + js
        + "</script></body></html>"
    )
