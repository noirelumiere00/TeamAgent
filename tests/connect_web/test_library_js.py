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
function element(){return {events:{},children:[],dataset:{},attributes:{},
 matches(selector){return selector==='.preview img';},
 closest(selector){return selector==='.preview[data-player]'?this:null;},
 contains(child){return child===this||this.children.includes(child);},
 classList:{contains(){return false;}},setAttribute(k,v){this.attributes[k]=v;},
 addEventListener(name,fn){this.events[name]=fn;},
 appendChild(child){this.children.push(child);child.parent=this;},
 remove(){this.parent.children=this.parent.children.filter(child=>child!==this);}};}
const filters=element(),desktop={matches:true,addEventListener(name,fn){this.change=fn;}};
const cards=[element(),element()];
cards.forEach((card,i)=>card.dataset.player='https://www.tiktok.com/player/v1/'+i+'?autoplay=1&muted=1');
const imageBox=element(), pendingImage=element(), failedImage=element(), loadedImage=element();
[pendingImage,failedImage,loadedImage].forEach(img=>imageBox.appendChild(img));
pendingImage.complete=false;failedImage.complete=true;failedImage.naturalWidth=0;
loadedImage.complete=true;loadedImage.naturalWidth=480;
const copyButton=element(), status=element(), doc=element(), win=element();
const caseCopy=element(),urlCopy=element(),playButton=element(),detail=element();
caseCopy.dataset.copy='campaign-copy';urlCopy.dataset.copy='campaign-urls';
playButton.dataset.play='detail-player';detail.dataset.player=cards[0].dataset.player;
const segments=[element(),element()], panels=[element(),element()];
segments.forEach((s,i)=>s.dataset.segment='segment-'+i);
const ids={'copy':copyButton,'copy-status':status,'campaign-copy':{textContent:'事例: 商品\n動画: 2本'},
 'campaign-urls':{textContent:'https://www.tiktok.com/@sample/video/1'},'detail-player':detail};
panels.forEach((p,i)=>ids['segment-'+i]=p);
const checks=[{checked:true,dataset:{url:'https://www.tiktok.com/@sample/video/1'}},
 {checked:false,dataset:{url:'https://www.tiktok.com/@sample/video/2'}},
 {checked:true,dataset:{url:'https://www.tiktok.com/@sample/video/3'}}];
let copied=null;
global.document=Object.assign(doc,{
 querySelectorAll(selector){
  if(selector==='[data-autoload]')return [];
  if(selector==='.header-search')return [filters];
  if(selector==='.preview[data-player]')return cards;
  if(selector==='.preview img')return [pendingImage,failedImage,loadedImage];
  if(selector==='[data-copy]')return [caseCopy,urlCopy];
  if(selector==='[data-play]')return [playButton];
  if(selector==='[data-segment]')return segments;
  assert.equal(selector,'input[data-url]:checked');return checks.filter(c=>c.checked);
 },
 createElement(tag){assert.equal(tag,'iframe');return element();},
 getElementById(id){return ids[id];}
});
global.window=Object.assign(win,{matchMedia(query){assert.equal(query,'(min-width:768px)');return desktop;}});
Object.defineProperty(global,'navigator',{value:{clipboard:{async writeText(text){copied=text;}}}});
__SCRIPT__
(async()=>{
 assert.equal(filters.open,true);
 desktop.matches=false;desktop.change();assert.equal(filters.open,false);
 filters.open=true;assert.equal(filters.open,true);
 desktop.matches=true;desktop.change();assert.equal(filters.open,true);
 assert.deepEqual(imageBox.children,[pendingImage,loadedImage]);
 doc.events.error({target:pendingImage});assert.deepEqual(imageBox.children,[loadedImage]);
 doc.events.mouseover({target:cards[0],relatedTarget:null});assert.equal(cards[0].children.length,1);
 assert.equal(cards[0].children[0].src,cards[0].dataset.player);
 assert.equal(cards[0].children[0].allow,'autoplay; encrypted-media');
 doc.events.mouseover({target:cards[1],relatedTarget:null});assert.equal(cards[0].children.length,0);
 assert.equal(cards[1].children.length,1);
 doc.events.mouseout({target:cards[1],relatedTarget:null});assert.equal(cards[1].children.length,0);
 doc.events.mouseover({target:cards[0],relatedTarget:null});win.events.blur();assert.equal(cards[0].children.length,0);
 doc.events.mouseover({target:cards[0],relatedTarget:null});doc.hidden=true;doc.events.visibilitychange();
 assert.equal(cards[0].children.length,0);
 // Appended cards have no per-card listeners: delegation must work immediately.
 const added=element();added.dataset.player='https://www.tiktok.com/player/v1/99';
 doc.events.mouseover({target:added,relatedTarget:null});assert.equal(added.children.length,1);
 doc.events.mouseover({target:added,relatedTarget:added});assert.equal(added.children.length,1);
 doc.events.mouseout({target:added,relatedTarget:added});assert.equal(added.children.length,1);
 doc.events.mouseout({target:added,relatedTarget:null});assert.equal(added.children.length,0);
 await copyButton.events.click();
 assert.equal(copied,'https://www.tiktok.com/@sample/video/1\nhttps://www.tiktok.com/@sample/video/3');
 assert.equal(status.textContent,'2件コピーしました');
 checks.push({checked:true,dataset:{url:'https://www.tiktok.com/@sample/video/99'}});
 await copyButton.events.click();assert.equal(status.textContent,'3件コピーしました');
 assert.equal(copied.split('\n').at(-1),checks.at(-1).dataset.url);
 const addedImage=element();imageBox.appendChild(addedImage);
 doc.events.error({target:addedImage});assert.equal(imageBox.children.includes(addedImage),false);
 await caseCopy.events.click();assert.equal(copied,'事例: 商品\n動画: 2本');
 assert.equal(status.textContent,'事例をコピーしました');
 await urlCopy.events.click();assert.equal(copied,ids['campaign-urls'].textContent);
 playButton.events.click();assert.equal(detail.children.length,1);
 doc.events.mouseover({target:cards[0],relatedTarget:null});assert.equal(detail.children.length,0);
 segments[0].events.focus();assert.equal(panels[0].hidden,false);
 assert.equal(panels[1].hidden,true);assert.equal(segments[0].attributes['aria-expanded'],'true');
 segments[1].events.click();assert.equal(panels[0].hidden,true);
 assert.equal(panels[1].hidden,false);
 navigator.clipboard.writeText=async()=>{throw Error('denied');};
 await copyButton.events.click();assert.match(status.textContent,/コピーできませんでした/);
})().catch(e=>{console.error(e);process.exitCode=1;});
""".replace("__SCRIPT__", _SCRIPT)
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
@pytest.mark.parametrize("observer_enabled", [True, False])
@pytest.mark.parametrize("previous_enabled", [True, False])
def test_autoload_serial_fetch_retry_completion_and_button_fallback(
    observer_enabled: bool, previous_enabled: bool
) -> None:
    harness = (
        r"""
const assert=require('node:assert/strict');
const eventHandlers={};
function el(){return {hidden:false,disabled:false,events:{},dataset:{},attributes:{},
 addEventListener(name,fn){this.events[name]=fn;},
 setAttribute(k,v){this.attributes[k]=v;},getAttribute(k){return this.attributes[k];}};}
const root=el(),more=el(),status=el(),nav=el(),link=el(),previous=__PREVIOUS__?el():null;
root.dataset.total='150';link.attributes.href='/library/videos?category=media&sort=plays&page=2';
const grid=Object.assign(el(),{children:Array.from({length:50},(_,i)=>({id:i})),
 append(...cards){this.children.push(...cards);},querySelectorAll(){return [];}});
root.querySelector=selector=>({'[data-items]':grid,'[data-load-more]':more,
 '[data-load-status]':status,'.pagination':nav,'a[rel="next"]':link,'a[rel="prev"]':previous})[selector];
let parsedPage,observer,requests=[];
global.document={querySelectorAll(selector){return selector==='[data-autoload]'?[root]:[];},
 getElementById(){return null;},addEventListener(n,fn){eventHandlers[n]=fn;},
 importNode(card,deep){assert.equal(deep,true);return {...card};}};
global.window={location:new URL('https://archive.example/library/videos?category=media&sort=plays'),
 addEventListener(){}};
if(__OBSERVER__){
 global.IntersectionObserver=class{
  constructor(callback,options){this.callback=callback;this.options=options;observer=this;}
  observe(target){assert.equal(target,more);}
  disconnect(){this.disconnected=true;}
 };window.IntersectionObserver=global.IntersectionObserver;
}
global.fetch=(url,options)=>new Promise(resolve=>requests.push({url,options,resolve}));
global.DOMParser=class{parseFromString(text,type){assert.equal(type,'text/html');
 return {querySelector(selector){assert.equal(selector,'[data-autoload]');return parsedPage;}};}};
function page(start,next){const cards=Array.from({length:50},(_,i)=>({id:start+i}));
 return {cards,querySelectorAll(selector){assert.equal(selector,'[data-items] > .card');return cards;},
 querySelector(selector){assert.equal(selector,'a[rel="next"]');
 return next?{getAttribute(){return next;}}:null;}};}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
function enter(){observer.callback([{isIntersecting:true}]);}
function respond(ok=true){requests.at(-1).resolve({ok,async text(){return '<html>only cards</html>';}});}
__SCRIPT__
(async()=>{
 assert.equal(nav.hidden,!previous);assert.equal(link.hidden,true);assert.equal(more.hidden,false);
 assert.equal(status.textContent,'50件表示（全150件）');
 if(observer){assert.equal(observer.options.rootMargin,'400px');enter();enter();}
 else more.events.click();
 const duplicate=more.events.click(); // busy: no second request, even from the button
 assert.equal(requests.length,1);await duplicate;assert.equal(more.disabled,true);
 assert.equal(status.textContent,'読み込み中…');assert.equal(root.attributes['aria-busy'],'true');
 assert.equal(requests[0].url,'https://archive.example/library/videos?category=media&sort=plays&page=2');
 assert.deepEqual(requests[0].options,{credentials:'same-origin',mode:'same-origin',redirect:'error'});
 parsedPage=page(50,'/library/videos?category=media&sort=plays&page=3');respond();await flush();
 assert.equal(grid.children.length,100);assert.equal(grid.children[50].id,50);
 assert.notEqual(grid.children[50],parsedPage.cards[0]);
 assert.equal(status.textContent,'100件表示（全150件）');assert.equal(more.disabled,false);
 if(observer)enter();else more.events.click();
 respond(false);await flush();
 assert.equal(grid.children.length,100);assert.equal(more.textContent,'再試行');
 assert.match(status.textContent,/読み込めませんでした/);assert.equal(nav.hidden,false);assert.equal(link.hidden,false);
 assert.equal(link.attributes.href,'/library/videos?category=media&sort=plays&page=3');
 assert.equal(root.attributes['aria-busy'],'false');
 if(observer){enter();enter();assert.equal(requests.length,2);} // no infinite retry
 // Login/malformed HTML and invalid next links must not append anything.
 for(const bad of [null,page(100,'https://other.example/library/videos?page=4'),
                   page(100,'/library/videos?category=media&sort=plays&page=3')]){
  more.events.click();parsedPage=bad;respond();await flush();
  assert.equal(grid.children.length,100);assert.equal(more.textContent,'再試行');
 }
 more.events.click();assert.match(requests.at(-1).url,/page=3$/);
 parsedPage=page(100,null);respond();await flush();
 assert.equal(grid.children.length,150);assert.equal(more.hidden,true);assert.equal(nav.hidden,!previous);assert.equal(link.hidden,true);
 assert.equal(status.textContent,'すべて表示しました（150件）');
 if(observer)assert.equal(observer.disconnected,true);
 const count=requests.length;await more.events.click();assert.equal(requests.length,count);
})().catch(error=>{console.error(error);process.exitCode=1;});
""".replace("__OBSERVER__", str(observer_enabled).lower())
        .replace("__PREVIOUS__", str(previous_enabled).lower())
        .replace("__SCRIPT__", _SCRIPT)
    )
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
