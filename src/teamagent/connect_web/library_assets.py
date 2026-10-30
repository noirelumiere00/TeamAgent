"""Self-contained library styles and behavior, nonced by the existing HTML renderer."""

STYLE = """
:root{color-scheme:light;--ink:#172438;--muted:#526175;--blue:#2563eb;--line:#dce2e9}
*{box-sizing:border-box}body{margin:0;background:#f6f7f9;color:var(--ink);font:16px/1.6
system-ui,-apple-system,BlinkMacSystemFont,"Hiragino Sans","Noto Sans JP","Yu Gothic UI",sans-serif}
a{color:#1d4ed8;text-underline-offset:4px}button,input,select,textarea{font:inherit}
button,a,input,select,summary{touch-action:manipulation}button{cursor:pointer}
:focus-visible{outline:3px solid #2563eb;outline-offset:4px}
header{background:#fff;border-bottom:1px solid var(--line)}.header-inner{max-width:1328px;
margin:auto;padding:16px 24px;display:flex;align-items:center;justify-content:space-between;
gap:16px}
.brand{font-weight:800;font-size:20px;color:var(--ink);text-decoration:none;white-space:nowrap}
.brand svg{vertical-align:middle;margin-right:8px;color:var(--blue)}nav{display:flex;gap:8px;
flex-wrap:wrap}
nav a{padding:8px 14px;color:var(--muted);text-decoration:none;border-radius:6px}
nav a[aria-current]{background:#eff4ff;color:#1d4ed8;font-weight:700}
main{max-width:1328px;margin:auto;padding:32px 24px 64px}h1,h2,h3,p{margin-top:0}
h1{font-size:36px;line-height:1.35;letter-spacing:-.03em;margin-bottom:14px}
h1,h2,h3{overflow-wrap:anywhere}h2{font-size:24px;line-height:1.4}h3{font-size:18px;
line-height:1.5}p{overflow-wrap:anywhere}
.eyebrow{font-size:12px;font-weight:800;letter-spacing:.14em;color:#1d4ed8;margin-bottom:12px}
.muted,.period{color:var(--muted)}.intro{max-width:720px;margin-bottom:20px}
input,select,textarea{background:#fff;color:var(--ink);border:1px solid #adb8c6;border-radius:6px;
padding:10px 12px;max-width:100%;min-width:0}input[type=checkbox]{accent-color:var(--blue)}
button,.button{border:1px solid #adb8c6;border-radius:6px;padding:10px 16px;background:#fff;
color:var(--ink);font-weight:650;text-decoration:none;display:inline-block}
.primary{background:var(--blue);border-color:var(--blue);color:white}
button.cta{background:#f97316;border-color:#f97316;color:#172438}button:disabled{cursor:not-allowed;
background:#edf0f4;color:#526175;border-color:#dce2e9;font-weight:400}
.filters{background:white;border:1px solid var(--line);padding:24px;border-radius:10px;
margin:20px 0 28px}
.search-row{display:flex;gap:12px;margin-bottom:20px}.search-row input{width:100%;padding:14px 16px}
.search-row label{flex:1}.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;
overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
.filter-row,.chips,.actions{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
.filter-row{margin-top:16px}.filter-row label{font-size:14px;display:flex;gap:8px;
align-items:center}
.chips{gap:8px}.filters>.chips{margin-bottom:12px}.chip{padding:7px 13px;border-radius:24px;
background:#fff;border:1px solid #c8d1dd;
color:#334155;text-decoration:none;font-size:14px}.chip[aria-current]{background:#eaf1ff;
color:#1d4ed8;border-color:#2563eb;font-weight:700}.filter-label{font-size:13px;color:var(--muted)}
.section-heading{display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;
gap:12px;margin:28px 0 16px}.section-heading h2{margin:0}.section-heading p{margin:0}
.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:24px}
article,section{min-width:0}.card{background:#fff;border:1px solid var(--line);border-radius:8px;
overflow:hidden;box-shadow:0 2px 5px #17243808;transition:transform 180ms ease}
.card:hover{transform:translateY(-3px)}.card-body{padding:18px}.card-body h3{margin:10px 0}
.card-body h3 a{color:var(--ink);text-decoration:none}.card-body p{margin-bottom:10px}
.preview{display:block;position:relative;aspect-ratio:9/16;overflow:hidden;background:#e5edfa;
color:#334155;text-decoration:none}.preview img,.preview iframe{position:absolute;inset:0;
width:100%;height:100%;object-fit:cover;border:0}.preview iframe{pointer-events:none}
.preview.detail-player iframe{pointer-events:auto;z-index:2}
.play-overlay{position:absolute;inset:0;width:100%;border:0;background:transparent}
.card-tags{display:flex;flex-wrap:wrap;gap:8px}.card-tag{display:grid;gap:4px}
.card-tag .filter-label{font-size:11px}.card-tag .badge{overflow-wrap:anywhere}
.placeholder{position:absolute;inset:0;
display:flex;flex-direction:column;justify-content:center;align-items:center;gap:12px;
background:linear-gradient(145deg,#e4edfd,#f8fafc 60%,#d8e5fa);color:#435c80}
.placeholder[data-category=tto]{background:linear-gradient(145deg,#d8e3f4,#f1f5fb,#cbd9ed)}
.placeholder[data-category=kirinuki]{background:linear-gradient(145deg,#e8edf7,#fcfcfd,#dce4f4)}
.placeholder[data-category=organic]{background:linear-gradient(145deg,#e0eeef,#f8fafc,#dbe8ee)}
.placeholder strong{font-size:64px;font-weight:600}.placeholder small{font-size:12px}
.cover-note{position:absolute;bottom:14px;left:14px;right:14px;background:#ffffffed;
padding:8px 12px;
color:#172438;border-radius:5px;font-size:13px;font-weight:600}
.badge,.tag{display:inline-flex;border-radius:4px;padding:3px 9px;font-size:12px;
line-height:1.6;background:#edf2fa;color:#324d74;text-decoration:none}
.badge[data-category=tto]{background:#dee8fa;color:#23467a}.badge[data-category=organic]{
background:#e5eff0;color:#34565c}.badge[data-category=kirinuki]{background:#edf0f9;color:#495471}
.card-stats,.stats{font-variant-numeric:tabular-nums}.card-stats{font-size:14px}
.card-stats strong{font-size:22px;color:var(--ink)}.period{font-size:12px;margin-bottom:0!important}
.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:1px;background:var(--line);
border:1px solid var(--line);border-radius:8px;overflow:hidden;margin:24px 0}
.stats div{padding:20px;background:#fff}.stats dt{font-size:13px;color:var(--muted)}
.stats dd{margin:6px 0 0;font-size:26px;font-weight:750}.empty{padding:32px;background:#fff;
border:1px dashed #adb8c6;border-radius:8px}.actions{margin:20px 0}.pagination{margin-top:28px}
.detail-layout{display:grid;grid-template-columns:minmax(240px,360px) minmax(0,1fr);gap:32px;
align-items:start}.detail-layout .preview{border-radius:8px}.detail-meta{margin-top:20px}
.metric-groups{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;
align-items:start}
.metric-group,.analysis,section:has(>form){background:white;border:1px solid var(--line);
border-radius:8px;padding:20px;margin-bottom:20px}.metric-group h2{font-size:18px}
.paid{grid-column:1/-1}.paid summary{cursor:pointer}.paid summary h2{display:inline}
.paid summary span{display:block;margin-top:8px;font-size:14px;color:var(--muted)}
.metric-group td{white-space:nowrap}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:13px}
th,td{text-align:left;padding:8px 4px;border-bottom:1px solid var(--line);overflow-wrap:anywhere}
th{font-weight:500}td{text-align:right}.metric-group p{font-size:12px;margin-top:12px}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}.analysis{margin-top:32px}
.timeline{display:flex;gap:3px;min-height:100px;margin:20px 0}.segment{padding:10px 4px;
min-width:0;background:#edf3ff;border:1px solid #b7caeb;color:#244673;border-radius:5px}
.segment span{display:block;font-size:11px;overflow-wrap:anywhere}
.segment strong{display:block;overflow-wrap:anywhere;
font-size:13px}.segment[aria-expanded=true]{background:#dbe8ff;border-color:#2563eb}
.segment-content{background:#f6f8fb;border-radius:6px;padding:20px;margin-bottom:20px}
.segment-content dl{display:grid;grid-template-columns:90px 1fr;gap:8px;margin:0}
.segment-content dd{margin:0;overflow-wrap:anywhere}.tag-groups{display:grid;
grid-template-columns:repeat(3,minmax(0,1fr));gap:24px}.tag-group h3{font-size:12px;
font-weight:600;color:var(--muted);margin-bottom:8px}.tag{position:relative;font-size:13px;
max-width:100%;overflow-wrap:anywhere}
.hook_type .tag,.cta .tag{background:#e6efff;color:#244b8b}
.presentation .tag,.topic .tag{background:#e8eef6;color:#3b526c}
.format .tag,.pain .tag,.standards .tag{background:#eef0f4;color:#4b5264}
.tooltip{display:none;position:absolute;z-index:5;bottom:calc(100% + 6px);left:0;
width:200px;max-width:calc(100vw - 48px);background:#172438;color:#fff;padding:10px;
border-radius:5px;font-size:12px}
.tag:hover .tooltip,.tag:focus .tooltip{display:block}.tag[aria-disabled]{cursor:help}
.account-form{display:flex;flex-wrap:wrap;align-items:end;gap:16px;background:#fff;
padding:20px;border:1px solid var(--line);border-radius:8px;margin-bottom:24px}
.account-form label{display:grid;gap:6px;flex:1 1 220px;font-size:14px}
.account-form input{width:100%}.account-table{table-layout:fixed}
.account-table td{text-align:left}
section form{display:flex;flex-wrap:wrap;gap:12px;align-items:center}section form h2{width:100%}
section form label{display:flex;flex-direction:column;gap:6px;font-size:14px}
@media(min-width:1200px){.video-grid{grid-template-columns:repeat(6,minmax(0,1fr))}}
@media(max-width:1024px){.grid{grid-template-columns:repeat(3,minmax(0,1fr));gap:18px}
.metric-groups{grid-template-columns:1fr}.detail-layout{grid-template-columns:300px minmax(0,1fr)}}
@media(max-width:767px){main{padding:28px 16px 48px}.header-inner{padding:14px 16px;flex-wrap:wrap}
nav{gap:4px}nav a{padding:6px 10px;font-size:14px}h1{font-size:28px}h2{font-size:21px}
.filters{padding:16px}.search-row{gap:8px}.search-row input{padding:12px}
.search-row button{padding:8px 12px}
.grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.card-body{padding:12px}
.card-body h3{font-size:16px}.card-stats strong{font-size:20px}
.stats{grid-template-columns:repeat(2,minmax(0,1fr))}
.stats div{padding:16px}.stats dd{font-size:23px}.detail-layout{grid-template-columns:1fr;gap:20px}
.detail-layout .preview{max-width:360px;margin:auto}
.tag-groups{grid-template-columns:repeat(2,minmax(0,1fr))}
.analysis{padding:16px}.segment-content dl{grid-template-columns:1fr}.timeline{gap:2px}
.segment span{font-size:9px}.segment strong{font-size:11px}.tooltip{width:150px}
.filter-row label{flex-wrap:wrap}.filter-row input{width:100%}}
@media(max-width:420px){.campaign-grid{grid-template-columns:1fr}
.card-body .actions{gap:8px;font-size:14px}}
@media(prefers-reduced-motion:reduce){.card{transition:none}.card:hover{transform:none}}
"""

SCRIPT = """
document.querySelectorAll('.preview img').forEach(img=>{
 img.addEventListener('error',()=>img.remove());
 if(img.complete&&img.naturalWidth===0)img.remove();
});
let active=null;
function clearPlayer(){if(active){active.remove();active=null;}}
function play(card){clearPlayer();active=document.createElement('iframe');
 active.src=card.dataset.player;active.title='TikTok プレビュー';
 active.allow='autoplay; encrypted-media';card.appendChild(active);}
document.querySelectorAll('.preview[data-player]').forEach(card=>{
 card.addEventListener('mouseenter',()=>{if(!card.classList.contains('detail-player'))play(card);});
 card.addEventListener('mouseleave',()=>{if(!card.classList.contains('detail-player'))clearPlayer();});
});
document.querySelectorAll('[data-play]').forEach(button=>{
 button.addEventListener('click',()=>play(document.getElementById(button.dataset.play)));
});
window.addEventListener('blur',clearPlayer);
document.addEventListener('visibilitychange',()=>{if(document.hidden)clearPlayer();});
async function copyText(text,message){
 const status=document.getElementById('copy-status');
 try{await navigator.clipboard.writeText(text);status.textContent=message;}
 catch(e){status.textContent='コピーできませんでした。ブラウザのクリップボード権限を確認してください。';}
}
const copy=document.getElementById('copy');
if(copy)copy.addEventListener('click',async()=>{
 const urls=[...document.querySelectorAll('input[data-url]:checked')].map(el=>el.dataset.url);
 await copyText(urls.join('\\n'),urls.length+'件コピーしました');
});
document.querySelectorAll('[data-copy]').forEach(button=>{
 button.addEventListener('click',()=>copyText(document.getElementById(button.dataset.copy).textContent,
 button.dataset.copy==='campaign-copy'?'事例をコピーしました':'URLをコピーしました'));
});
document.querySelectorAll('[data-segment]').forEach(button=>{
 function reveal(){document.querySelectorAll('[data-segment]').forEach(other=>{
  const selected=other===button;other.setAttribute('aria-expanded',String(selected));
  document.getElementById(other.dataset.segment).hidden=!selected;
 });}
 button.addEventListener('click',reveal);button.addEventListener('focus',reveal);
});
"""
