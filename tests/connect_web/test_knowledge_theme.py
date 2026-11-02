"""Knowledge presentation stays light and keeps useful, escaped navigation."""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest
from bs4 import BeautifulSoup

from teamagent.connect_web import app, knowledge_lists, portal
from teamagent.connect_web.search_graph import graph_page


def test_all_knowledge_pages_share_light_palette_and_header() -> None:
    pages = [
        portal.render_home("demo@example.test"),
        app._shell_page("demo@example.test", mode="list"),
        app._shell_page("demo@example.test", mode="graph"),
        *[
            app._shell_page("demo@example.test", mode=view, list_content=content)
            for view, content in (
                ("clients", knowledge_lists.render_list("clients", 1, [], 0)),
                ("docs", knowledge_lists.render_list("docs", 1, [], 0)),
                ("tags", knowledge_lists.render_tags([])),
                ("doc", knowledge_lists.render_document({"title": "<unsafe>"})),
            )
        ],
        app._karte_page("<unsafe>", "demo@example.test"),
        graph_page("demo@example.test", []),
    ]
    old_colors = re.compile(
        r"#(?:0f1420|0c111c|1a2233|222d44|283450|16203a|2b3a5e|101827|1b2740)\b"
        r"|rgba?\(20,\s*28,\s*46",
        re.I,
    )
    for markup in pages:
        page = BeautifulSoup(markup, "html.parser")
        css = "".join(style.text for style in page.select("style"))
        assert "color-scheme:light" in css
        assert "#234e89" in css
        assert not old_colors.search(css)
        assert len(page.select(".connect-header")) == 1
        assert page.select_one('.connect-brand[href="/"]')
        assert not page.select("unsafe")

    # AA text contrast on the darkest light surface, including muted sidebar counts.
    def luminance(color: str) -> float:
        values = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in values]
        return sum(v * weight for v, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True))

    tokens = dict(re.findall(r"--([a-z-]+):(#[0-9a-f]{6})", app._ROOT_TOKENS))
    for key in ("text", "muted", "faint", "accent"):
        assert (luminance("#e4ecf8") + 0.05) / (luminance(tokens[key]) + 0.05) >= 4.5
    assert (luminance("#ffffff") + 0.05) / (luminance("#234e89") + 0.05) >= 4.5


def test_search_entries_and_document_mobile_labels() -> None:
    page = BeautifulSoup(app._shell_page("demo@example.test", mode="list"), "html.parser")
    assert page.select_one("#tabList").text == "検索"
    assert page.select_one("#tabGraph").text == "関係図"
    assert page.select_one('.journey-links a[href="/library"]')
    assert page.select_one('label[for="q"]')
    assert page.select_one(".search-options #fclient")
    listing = BeautifulSoup(
        knowledge_lists.render_list("clients", 1, [{"name": "<A>", "document_count": 3}], 1),
        "html.parser",
    )
    assert [td["data-label"] for td in listing.select("td")] == ["名前", "資料件数", "最新FB日"]
    assert listing.select_one("td a").text == "<A>"


@pytest.mark.skipif(shutil.which("node") is None, reason="node が必要")
def test_shell_respects_entry_mode_and_reports_graph_failure() -> None:
    harness = r"""
const assert=require('node:assert/strict');
const elements=new Map();
function element(){return {hidden:false,value:'',textContent:'',attrs:{},children:[],
  classList:{add(){},remove(){},toggle(){}},appendChild(n){this.children.push(n);},
  setAttribute(k,v){this.attrs[k]=v;},addEventListener(){}};}
global.document={getElementById(id){if(!elements.has(id))elements.set(id,element());
  return elements.get(id);},createElement:element};
global.window={innerWidth:375};global.requestAnimationFrame=()=>{};
global.localStorage={getItem(){return JSON.stringify({mode:'graph',leftOpen:true,rightOpen:true});},
  setItem(){}};
global.fetch=async()=>({status:500,ok:false,json:async()=>({})});
document.getElementById('mainGraph').hidden=true;
__SCRIPT__
(async()=>{
  assert.equal(currentMode,'list');assert.equal(st.leftOpen,false);assert.equal(st.rightOpen,false);
  assert.equal(tabList.attrs['aria-pressed'],'true');
  assert.equal(leftToggle.attrs['aria-expanded'],'false');
  await loadGraphData();assert.match(tagexp.textContent,/取得できませんでした/);
  global.fetch=async()=>({status:200,ok:true,json:async()=>({nodes:[],edges:[]})});
  await loadGraphData();assert.equal(GNODES.length,0);
  assert.ok(tagexp.children.length);setMode('graph');
  assert.equal(mainGraph.hidden,false);assert.equal(tabGraph.attrs['aria-pressed'],'true');
})().catch(e=>{console.error(e);process.exitCode=1;});
""".replace("__SCRIPT__", app._SHELL_JS)
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
