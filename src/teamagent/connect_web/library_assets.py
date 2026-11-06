"""Self-contained library styles and behavior, nonced by the existing HTML renderer."""

STYLE = """
.import-workspace{max-width:1000px;margin:24px auto;line-height:1.7}
.import-workspace form{display:grid;gap:14px;margin:20px 0}
.import-workspace label{display:grid;gap:6px}
.import-workspace textarea{width:100%;resize:vertical;padding:12px;font:inherit;
border:1px solid var(--line);border-radius:8px}
.import-workspace select,.import-workspace input{min-width:0;max-width:100%}
.import-workspace details{border-top:1px solid var(--line);padding-top:20px}
.import-workspace button{justify-self:start}
.import-table{overflow-x:auto}
.import-table table{width:100%;border-collapse:collapse}
.import-table th,.import-table td{padding:12px;text-align:left;
border-bottom:1px solid var(--line);vertical-align:top}
.import-table td{overflow-wrap:anywhere;min-width:70px}
.import-table td:nth-child(2){min-width:160px;max-width:420px}
#import-accounts table{table-layout:fixed}
#import-accounts th,#import-accounts td{min-width:0;max-width:none;overflow-wrap:anywhere}
#import-accounts th:first-child{width:3.5em}

[data-autoload] [hidden]{display:none!important}
[data-load-status]{font-size:13px;color:var(--muted);margin:20px 0 12px}
[data-load-more]{display:block;margin:0 auto 24px}
:root{color-scheme:light;--ink:#20252d;--muted:#5b6470;--blue:#234e89;--line:#e3e6eb}
*{box-sizing:border-box}body{margin:0;background:#fff;color:var(--ink);font:16px/1.6
system-ui,-apple-system,BlinkMacSystemFont,"Hiragino Sans","Noto Sans JP","Yu Gothic UI",sans-serif}
a{color:#1d4ed8;text-underline-offset:4px}button,input,select,textarea{font:inherit}
button,a,input,select,summary{touch-action:manipulation}button{cursor:pointer}
:focus-visible{outline:3px solid #2563eb;outline-offset:4px}
.library-header{background:#fff}
.header-inner{max-width:1328px;margin:auto;padding:24px;display:flex;
align-items:center;justify-content:space-between;gap:24px}
.library-utility{display:flex;align-items:center;gap:20px}
.soon{display:inline-flex;align-items:center;gap:8px;font-size:12px}
button.soon:disabled{border:0;background:none;color:var(--muted);padding:0}
.soon span{padding:2px 7px;border-radius:4px;background:#edf0f4;font-size:10px;white-space:nowrap}
.library-admin{font-size:12px;color:var(--muted);white-space:nowrap;text-decoration:none;
border-left:1px solid var(--line);padding-left:20px}
.library-admin[aria-current]{font-weight:700;color:var(--blue)}
.category-nav{display:flex;flex-wrap:wrap;gap:8px}
.category-nav a{font-size:12px;text-align:center}
.brand{font-weight:800;font-size:20px;color:var(--ink);text-decoration:none;white-space:nowrap}
.brand svg{vertical-align:middle;margin-right:8px;color:var(--blue)}
nav:where(:not(.connect-menu)){display:flex;gap:8px;
flex-wrap:wrap}
nav:where(:not(.connect-menu)) a{padding:8px 14px;color:var(--muted);
text-decoration:none;border-radius:6px}
nav:where(:not(.connect-menu)) a[aria-current]{background:#eff4ff;color:#1d4ed8;font-weight:700}
main{max-width:1328px;margin:auto;padding:0 24px 64px}h1,h2,h3,p{margin-top:0}
h1{font-size:40px;line-height:1.35;letter-spacing:-.03em;margin-bottom:14px}
h1,h2,h3{overflow-wrap:anywhere}h2{font-size:24px;line-height:1.4}h3{font-size:18px;
line-height:1.5}p{overflow-wrap:anywhere}
.eyebrow{font-size:12px;font-weight:800;letter-spacing:.14em;color:#1d4ed8;margin-bottom:12px}
.muted,.period{color:var(--muted)}.intro{max-width:720px;margin-bottom:20px}
input,select,textarea{background:#fff;color:var(--ink);border:1px solid #adb8c6;border-radius:6px;
padding:10px 12px;max-width:100%;min-width:0}input[type=checkbox]{accent-color:var(--blue)}
button,.button{border:1px solid #adb8c6;border-radius:6px;padding:10px 16px;background:#fff;
color:var(--ink);font-weight:650;text-decoration:none;display:inline-block}
.primary{background:var(--blue);border-color:var(--blue);color:white}
button.cta{background:var(--blue);border-color:var(--blue);
color:#fff}button:disabled{cursor:not-allowed;
background:#edf0f4;color:#526175;border-color:#dce2e9;font-weight:400}
.workspace{display:grid;grid-template-columns:232px minmax(0,1fr);gap:36px;
border-top:1px solid var(--line);padding-top:24px;margin:0}
.sidebar{min-width:0;border-right:1px solid var(--line);padding-right:24px}
.sidebar>summary{font-size:15px;font-weight:700;padding-bottom:16px;cursor:pointer}
.sidebar fieldset{border:0;padding:0;margin:0 0 20px;min-width:0}
.sidebar legend,.sidebar-fields label{font-size:12px;color:var(--muted)}
.category-options{display:grid;gap:5px;margin-top:10px}
.category-option{display:flex;align-items:center;gap:8px;padding:9px 12px;border-radius:4px;
font-size:13px;cursor:pointer}
.category-option input{margin:0;accent-color:var(--blue)}
.category-option:has(input:checked){background:#eff4ff;color:var(--blue);font-weight:700}
.sidebar-fields{display:grid;gap:16px}
.sidebar-fields label{display:grid;gap:6px}
.sidebar-fields input,.sidebar-fields select{width:100%;font-size:13px}
.apply-filters{width:100%;margin-top:20px;font-size:13px}
.reset-filters{display:inline-block;margin-top:16px;font-size:12px}
.sidebar-note{border-top:1px solid var(--line);margin-top:24px;padding-top:20px;
font-size:11px;color:var(--muted)}
.workspace-main{min-width:0}
.workspace-main>.eyebrow{font-size:10px;letter-spacing:.22em;margin:0 0 8px}
.workspace-main>h1{font-size:26px;letter-spacing:-.035em;margin:0}
.workspace-search{display:flex;gap:10px;margin-top:16px}
.workspace-search label{flex:1;min-width:0}
.workspace-search input{width:100%;height:50px;font-size:14px}
.workspace-search button{flex:none;min-width:86px}
.search-hint{font-size:12px;color:var(--muted);margin:12px 0}
.workspace-toolbar{display:flex;align-items:center;justify-content:space-between;
gap:16px;flex-wrap:wrap;margin:24px 0 20px}
.workspace-toolbar>label{font-size:12px;color:var(--muted)}
.workspace-toolbar select{font-size:12px;padding:8px}
.main-nav{font-size:13px}
.workspace .grid{grid-template-columns:repeat(3,minmax(0,1fr));gap:28px 20px}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;
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
.catalog-more{margin:24px 0}
.account-name{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:24px}
article,section{min-width:0}.card{position:relative;min-width:0;background:#fff}
.card-body{padding:14px 0}.card-body h3{margin:10px 0}
.card-body h3 a{color:var(--ink);text-decoration:none}.card-body p{margin-bottom:10px}
.preview{display:block;position:relative;aspect-ratio:9/16;overflow:hidden;background:#e5edfa;
color:#334155;
text-decoration:none}.preview img{transition:transform 200ms ease}
.card-link:hover .preview img{transform:scale(1.035)}
.preview img,.preview iframe{position:absolute;inset:0;
width:100%;height:100%;object-fit:cover;border:0}.preview iframe{pointer-events:none}
.preview[data-player-failed]::after{content:'TikTok 側でプレビューを再生できない動画です';
position:absolute;left:8px;right:8px;bottom:8px;padding:6px 8px;border-radius:4px;
background:rgba(17,24,39,.75);color:#fff;font-size:11px;line-height:1.5}
.preview.detail-player iframe{pointer-events:auto;z-index:2}
.play-overlay{position:absolute;inset:0;width:100%;border:0;background:transparent}
.card-tags{display:flex;flex-wrap:wrap;gap:8px}.card-tag{display:grid;gap:4px}
.card-tag .filter-label{font-size:11px}.card-tag .badge{overflow-wrap:anywhere}
.placeholder{position:absolute;inset:0;
display:flex;flex-direction:column;justify-content:center;align-items:center;gap:12px;
background:linear-gradient(145deg,#e4edfd,#f8fafc 60%,#d8e5fa);color:#435c80}
.placeholder strong{font-size:28px;font-weight:500}
.placeholder-account{font-size:12px;max-width:85%;white-space:nowrap;overflow:hidden;
text-overflow:ellipsis}.detail-player .placeholder{justify-content:flex-start;padding-top:24px}
.placeholder small{font-size:12px}
.cover-note{position:absolute;bottom:14px;left:14px;right:14px;background:#ffffffed;
padding:8px 12px;
color:#172438;border-radius:5px;font-size:13px;font-weight:600}
.badge,.tag{display:inline-flex;border-radius:4px;padding:3px 9px;font-size:12px;
line-height:1.6;background:#edf2fa;color:#324d74;text-decoration:none}
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
.video-insights{overflow-wrap:anywhere}.video-insights .stats{
grid-template-columns:repeat(2,minmax(0,1fr))}.video-insights .stats div{padding:12px}
.video-insights .stats dd{font-size:23px}
.retention-chart{display:block;width:100%;height:auto}
.retention-chart text{fill:var(--muted);font-size:14px}
.chart-axis,.chart-half{fill:none;stroke:var(--line)}.chart-half{stroke-dasharray:4 4}
.chart-line{fill:none;stroke:var(--blue);stroke-width:3;stroke-linejoin:round}
.chart-marker,.ratio-fill{fill:var(--blue)}.ratio-track{fill:var(--line)}
.audience-ratios>div{display:grid;grid-template-columns:1fr auto;gap:4px;margin:12px 0}
.audience-ratios dd{margin:0}.audience-ratios svg{grid-column:1/-1;width:100%;height:8px}
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
@media(max-width:1024px){.grid{grid-template-columns:repeat(3,minmax(0,1fr));gap:18px}
.metric-groups{grid-template-columns:1fr}.detail-layout{grid-template-columns:300px minmax(0,1fr)}}
@media(max-width:767px){main{padding:0 16px 48px}
nav:where(:not(.connect-menu)){gap:4px}
nav:where(:not(.connect-menu)) a{padding:6px 10px;font-size:14px}
h1{font-size:28px}h2{font-size:21px}
.grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.card-body{padding:12px}
.card-body h3{font-size:16px}.card-stats strong{font-size:20px}
.stats{grid-template-columns:repeat(2,minmax(0,1fr))}
.stats div{padding:16px}.stats dd{font-size:23px}.detail-layout{grid-template-columns:1fr;gap:20px}
.detail-layout .preview{max-width:360px;margin:auto}
.tag-groups{grid-template-columns:repeat(2,minmax(0,1fr))}
.analysis{padding:16px}.segment-content dl{grid-template-columns:1fr}.timeline{gap:2px}
.segment span{font-size:9px}.segment strong{font-size:11px}.tooltip{width:150px}
.filter-row label{flex-wrap:wrap}.filter-row input{width:100%}}
@media(max-width:420px){.card-body .actions{gap:8px;font-size:14px}}
@media(prefers-reduced-motion:reduce){.preview img{transition:none}
.card-link:hover .preview img{transform:none}}
/* Archive presentation; detail metrics and analysis keep their existing contracts. */
.brand{display:flex;align-items:center;font-size:23px;line-height:1.3;letter-spacing:-.04em}
.brand small{display:block;font-size:10px;letter-spacing:.23em;margin-top:6px;color:var(--blue)}
.page-title{margin:12px 0 32px}.archive-section{margin:12px 0 70px}
.archive-section>.section-heading{margin:0 0 26px;align-items:center}
.archive-section .section-heading h2{font-size:34px;font-weight:900;letter-spacing:.025em;
line-height:1.15;padding-top:18px;position:relative}
.archive-section .section-heading h2:before{content:"";position:absolute;top:0;left:0;
width:32px;height:3px;background:var(--blue)}
.archive-section .section-heading h2 span{display:block;font-size:14px;letter-spacing:.04em;
margin-top:10px}.archive-section .section-heading p{font-size:13px;color:var(--muted);
margin-top:12px}
.more-link{font-size:13px;color:var(--ink);white-space:nowrap;text-decoration:none}
.more-link span{margin-left:16px;color:var(--blue)}.card-link{display:block;color:inherit;
text-decoration:none}.card-link:focus-visible{outline-offset:5px}.preview{border-radius:5px}
.card-body h3{font-size:18px;line-height:1.5;margin:8px 0;display:-webkit-box;
-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.category-label{display:inline-block;font-size:10px;font-weight:800;letter-spacing:.1em;
color:var(--blue)}.card-meta,.account-name,.card-count{font-size:12px;color:var(--muted);
white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.card-body p{margin-bottom:5px}
.card-meta{display:flex;gap:8px;align-items:baseline}
.meta-source{min-width:0;flex:1;overflow:hidden;white-space:normal}
.meta-source .account-name{display:inline-block;max-width:100%;vertical-align:bottom}
.meta-plays{flex:none;max-width:100%;font-variant-numeric:tabular-nums}
.card-context{font-size:10px;font-weight:700;color:var(--blue);
white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.card-plays{display:flex;flex-wrap:wrap;gap:4px 12px;font-size:12px;color:var(--muted)}
.card-plays strong{color:var(--ink)}
.card-tag{display:flex;gap:4px;align-items:center}.card-tags .badge{font-size:10px;padding:1px 5px}
.card-tags{margin-top:10px}.card-select{display:block;font-size:13px;padding:8px 0}
.pickup-grid,.video-shelf{display:grid;grid-auto-flow:column;overflow-x:auto;gap:24px;
scroll-snap-type:x proximity;padding:5px 5px 20px;margin:-5px -5px 0;scrollbar-width:thin}
.pickup-grid{grid-auto-columns:190px;margin:0;padding:0 0 20px}
.pickup-grid .card{scroll-snap-align:start}
.pickup-grid .card-link:focus-visible{outline-offset:-3px}
.video-shelf{grid-template-columns:none;grid-auto-columns:190px}
.archive-entry{background:#f5f7fa;padding:32px}.organic-entry{display:block;margin-top:20px;
font-size:13px}.active-filters{font-size:12px;color:var(--muted);margin:10px 0 0}
.pagination{margin-bottom:28px;justify-content:center}.result-count{font-size:13px}
.breadcrumb{font-size:12px;margin-bottom:30px}.case-hero{display:grid;
grid-template-columns:minmax(180px,280px) minmax(0,1fr) minmax(250px,340px);gap:32px;
align-items:center;margin:0 0 52px}.case-cover{aspect-ratio:4/5}
.case-intro h1{font-size:34px}.case-advertiser{font-size:15px;color:var(--muted)}
.case-intro .period{margin-top:18px}.case-numbers .stats{grid-template-columns:1fr 1fr;margin:0;
border:0;border-radius:0;background:var(--line);gap:1px}.case-numbers .stats div{padding:18px 12px;
background:#f5f7fa}.case-numbers .stats dd{font-size:24px}.case-numbers>p{font-size:11px;
margin-top:14px}.case-intro .actions{gap:8px}.case-intro button{font-size:14px}
footer{background:#f5f7fa;padding:44px 24px;text-align:center}footer .brand{justify-content:center}
footer .category-nav{justify-content:center;margin-top:22px}
footer>p{font-size:12px;color:var(--muted);
margin:24px 0 0}footer>p span{margin-left:24px}
@media(max-width:1024px){.case-hero{grid-template-columns:240px minmax(0,1fr)}
.case-numbers{grid-column:1/-1}.case-numbers .stats{grid-template-columns:repeat(4,minmax(0,1fr))}
.workspace{grid-template-columns:200px minmax(0,1fr);gap:24px}
.workspace .grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:600px){
.header-inner{padding:20px 16px;gap:12px}
.library-header .brand{font-size:20px}
.library-utility>.soon{display:none}
.library-admin{border:0;padding:0;font-size:11px}
.workspace{display:flex;flex-direction:column;gap:20px;padding-top:0;border:0}
.sidebar{border:1px solid var(--line);border-radius:6px;padding:0 14px}
.sidebar>summary{font-size:13px;padding:13px 0;color:var(--blue)}
.category-options{grid-template-columns:repeat(2,minmax(0,1fr));gap:5px}
.category-option{font-size:12px;padding:8px 6px;border:1px solid var(--line)}
.sidebar-fields{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
.reset-filters{margin-bottom:12px}
.sidebar-note{display:none}
.workspace-main>h1{font-size:24px}
.workspace-search{gap:8px}
.workspace-search input{font-size:13px;height:46px;padding:10px 12px}
.workspace-search button{min-width:62px;font-size:13px;padding:9px 12px}
.search-hint{font-size:11px}
.workspace .grid{gap:24px 14px}
}
@media(max-width:767px){
.archive-section{margin-bottom:48px}.archive-section .section-heading h2{font-size:28px}
.archive-section .section-heading p{max-width:245px;font-size:12px}
.archive-section>.section-heading{gap:8px}.more-link{font-size:12px}
.pickup-grid{grid-auto-columns:160px;gap:16px}
.card-body{padding:12px 0}.card-body h3{font-size:15px}
.video-shelf{grid-auto-columns:160px;gap:16px}
.archive-entry{padding:24px 18px}.case-hero{grid-template-columns:1fr;gap:24px}
.case-cover{width:100%;max-width:340px;margin:auto}.case-intro h1{font-size:28px}
.case-numbers .stats{grid-template-columns:1fr 1fr}.case-numbers{grid-column:auto}
footer{padding:36px 16px}.main-nav a{padding:4px 0}}

"""

SCRIPT = """
document.querySelectorAll('select[name="requested_origin"]').forEach(select=>{
 function update(){document.querySelectorAll('[data-own-campaign]').forEach(el=>{
  el.hidden=select.value!=='own';
 });}
 select.addEventListener('change',update);update();
});
document.querySelectorAll('[data-import-campaign]').forEach(form=>{
 form.addEventListener('submit',()=>{
  const source=document.getElementById('import-urls').form;
  ['urls','case','idempotency_key','requested_origin'].forEach(name=>{
   form.elements[name].value=source.elements[name].value;
  });
 });
});
if(document.querySelectorAll('[data-import-poll="true"]').length){
 window.setTimeout(()=>{if(!document.hidden)window.location.reload();},5000);
 document.addEventListener('visibilitychange',()=>{if(!document.hidden)window.location.reload();});
}

document.querySelectorAll('.sidebar').forEach(details=>{
 const desktop=window.matchMedia('(min-width:601px)');
 function resize(){details.open=desktop.matches;}
 resize();desktop.addEventListener('change',resize);
});
document.addEventListener('error',event=>{
 if(event.target.matches('.preview img'))event.target.remove();
},true);
function removeBrokenImages(root){root.querySelectorAll('.preview img').forEach(img=>{
 if(img.complete&&img.naturalWidth===0)img.remove();
});}
removeBrokenImages(document);
let active=null,activeCard=null;
function clearPlayer(){if(active){active.remove();active=null;activeCard=null;}}
function play(card){if(card.dataset.playerFailed)return;clearPlayer();
 active=document.createElement('iframe');activeCard=card;
 active.src=card.dataset.player;active.title='TikTok プレビュー';
 active.allow='autoplay; encrypted-media';card.appendChild(active);}
/* TikTok cannot play some posts in its embed: fall back to our cover, not its error screen. */
window.addEventListener('message',event=>{
 const type=event.data&&event.data.type;
 if(event.origin!=='https://www.tiktok.com'||!active||event.source!==active.contentWindow
    ||(type!=='onError'&&type!=='onPlayerError'))return;
 const card=activeCard;clearPlayer();card.dataset.playerFailed='1';
});
document.addEventListener('mouseover',event=>{
 const card=event.target.closest('.preview[data-player]');
 if(card&&!card.classList.contains('detail-player')&&!card.contains(event.relatedTarget))play(card);
});
document.addEventListener('mouseout',event=>{
 const card=event.target.closest('.preview[data-player]');
 if(card&&!card.classList.contains('detail-player')&&!card.contains(event.relatedTarget)
    &&card.contains(active))clearPlayer();
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
document.querySelectorAll('[data-autoload]').forEach(root=>{
 const grid=root.querySelector('[data-items]'),more=root.querySelector('[data-load-more]');
 const status=root.querySelector('[data-load-status]'),nav=root.querySelector('.pagination');
 const link=root.querySelector('a[rel="next"]'),previous=root.querySelector('a[rel="prev"]');
 let next=link?link.getAttribute('href'):null,busy=false,failed=false,observer=null;
 const total=Number(root.dataset.total),format=n=>n.toLocaleString('ja-JP');
 function progress(){
  const count=grid.children.length;
  status.textContent=!next&&count===total?'すべて表示しました（'+format(total)+'件）':
   format(count)+'件表示（全'+format(total)+'件）'+(!next?'・この先はありません':'');
  more.hidden=!next;more.textContent='さらに読み込む';
  if(!next&&observer)observer.disconnect();
 }
 function pageURL(raw){
  const url=new URL(raw,window.location.href);
  if(url.origin!==window.location.origin||url.pathname!==window.location.pathname)
   throw Error('Invalid next page');
  return url.href;
 }
 async function load(){
  if(busy||!next)return;
  busy=true;failed=false;more.disabled=true;root.setAttribute('aria-busy','true');
  status.textContent='読み込み中…';
  try{
   const url=pageURL(next);
   const response=await fetch(url,{credentials:'same-origin',mode:'same-origin',redirect:'error'});
   if(!response.ok)throw Error('Page unavailable');
   const parsed=new DOMParser().parseFromString(await response.text(),'text/html');
   const page=parsed.querySelector('[data-autoload]');
   const items=page?.querySelectorAll('[data-items] > .card');
   if(!items||!items.length)throw Error('Missing cards');
   const following=page.querySelector('a[rel="next"]')?.getAttribute('href')||null;
   if(following&&pageURL(following)===url)throw Error('Repeated next page');
   grid.append(...Array.from(items,card=>document.importNode(card,true)));
   next=following;
   if(next)link.setAttribute('href',next);
   link.hidden=true;nav.hidden=!previous;removeBrokenImages(grid);progress();
  }catch(error){
   failed=true;status.textContent='読み込めませんでした。再試行するか、次ページを開いてください。';
   more.textContent='再試行';nav.hidden=false;link.hidden=false;
  }finally{
   busy=false;more.disabled=false;root.setAttribute('aria-busy','false');
  }
 }
 more.addEventListener('click',load);
 if(link)link.hidden=true;nav.hidden=!previous;progress();
 if(next&&'IntersectionObserver' in window){
  observer=new IntersectionObserver(entries=>{
   if(!failed&&entries.some(entry=>entry.isIntersecting))load();
  },{rootMargin:'400px'});
  observer.observe(more);
 }
});

"""
