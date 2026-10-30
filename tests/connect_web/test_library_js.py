"""Execute the shipped browser code with a tiny DOM, without network or a browser."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from teamagent.connect_web.library import _SCRIPT


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_single_player_disposal_and_checked_urls_clipboard() -> None:
    harness = r"""
const assert=require('node:assert/strict');
function element(){return {events:{},children:[],dataset:{},
 addEventListener(name,fn){this.events[name]=fn;},
 appendChild(child){this.children.push(child);child.parent=this;},
 remove(){this.parent.children=this.parent.children.filter(child=>child!==this);}};}
const cards=[element(),element()];
cards.forEach((card,i)=>card.dataset.player='https://www.tiktok.com/player/v1/'+i+'?autoplay=1&muted=1');
const imageBox=element(), pendingImage=element(), failedImage=element(), loadedImage=element();
[pendingImage,failedImage,loadedImage].forEach(img=>imageBox.appendChild(img));
pendingImage.complete=false;failedImage.complete=true;failedImage.naturalWidth=0;
loadedImage.complete=true;loadedImage.naturalWidth=480;
const copyButton=element(), status=element(), doc=element(), win=element();
const checks=[{checked:true,dataset:{url:'https://www.tiktok.com/@sample/video/1'}},
 {checked:false,dataset:{url:'https://www.tiktok.com/@sample/video/2'}},
 {checked:true,dataset:{url:'https://www.tiktok.com/@sample/video/3'}}];
let copied=null;
global.document=Object.assign(doc,{
 querySelectorAll(selector){
  if(selector==='.preview[data-player]')return cards;
  if(selector==='.preview img')return [pendingImage,failedImage,loadedImage];
  assert.equal(selector,'input[data-url]:checked');return checks.filter(c=>c.checked);
 },
 createElement(tag){assert.equal(tag,'iframe');return element();},
 getElementById(id){return id==='copy'?copyButton:status;}
});
global.window=win;
Object.defineProperty(global,'navigator',{value:{clipboard:{async writeText(text){copied=text;}}}});
__SCRIPT__
(async()=>{
 assert.deepEqual(imageBox.children,[pendingImage,loadedImage]);
 pendingImage.events.error();assert.deepEqual(imageBox.children,[loadedImage]);
 cards[0].events.mouseenter();assert.equal(cards[0].children.length,1);
 assert.equal(cards[0].children[0].src,cards[0].dataset.player);
 assert.equal(cards[0].children[0].allow,'autoplay; encrypted-media');
 cards[1].events.mouseenter();assert.equal(cards[0].children.length,0);
 assert.equal(cards[1].children.length,1);
 cards[1].events.mouseleave();assert.equal(cards[1].children.length,0);
 cards[0].events.mouseenter();win.events.blur();assert.equal(cards[0].children.length,0);
 cards[0].events.mouseenter();doc.hidden=true;doc.events.visibilitychange();
 assert.equal(cards[0].children.length,0);
 await copyButton.events.click();
 assert.equal(copied,'https://www.tiktok.com/@sample/video/1\nhttps://www.tiktok.com/@sample/video/3');
 assert.equal(status.textContent,'2件コピーしました');
 navigator.clipboard.writeText=async()=>{throw Error('denied');};
 await copyButton.events.click();assert.equal(status.textContent,'コピーできませんでした');
})().catch(e=>{console.error(e);process.exitCode=1;});
""".replace("__SCRIPT__", _SCRIPT)
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
