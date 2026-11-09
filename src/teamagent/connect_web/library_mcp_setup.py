"""Public connection instructions; rendering never starts registration or authentication."""

from __future__ import annotations

import html


def setup_html(resource: str) -> str:
    """Render the validated MCP resource as a selectable URL, outside JavaScript."""
    return _PAGE.replace("__MCP_RESOURCE__", html.escape(resource, quote=True))


_PAGE = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>動画ライブラリの接続 | NewsTV Connect</title>
<style>
:root{color-scheme:light;--ink:#20252d;--muted:#5b6470;--blue:#234e89;--line:#e3e6eb}
*{box-sizing:border-box}body{margin:0;background:#f7f8fa;color:var(--ink);
font:16px/1.75 system-ui,-apple-system,BlinkMacSystemFont,"Hiragino Sans","Noto Sans JP",sans-serif}
a{color:var(--blue);text-underline-offset:4px}button,input{font:inherit}
a,button,summary{touch-action:manipulation}
:focus-visible{outline:3px solid #2563eb;outline-offset:4px}
header{background:white;border-bottom:1px solid var(--line)}.header-inner{max-width:1040px;
margin:auto;padding:18px 24px}.brand{font-size:20px;font-weight:800;color:var(--ink);
text-decoration:none}
main{max-width:1040px;margin:auto;padding:44px 24px 60px}.eyebrow{font-size:12px;
font-weight:750;letter-spacing:.12em;color:var(--blue);margin:0 0 10px}
h1{font-size:clamp(27px,4vw,40px);line-height:1.4;letter-spacing:-.035em;margin:0 0 14px}
h2{font-size:22px;line-height:1.5;margin:0 0 12px}p{margin:0 0 14px}.muted{color:var(--muted)}
.intro{max-width:760px;margin-bottom:28px}.panel{background:white;border:1px solid var(--line);
border-radius:12px;padding:26px;margin-bottom:24px}.connection-row{display:flex;align-items:end;
gap:12px;margin-top:16px}.url-field{flex:1;min-width:0}label{display:block;font-size:14px;
font-weight:700;margin-bottom:6px}input{width:100%;min-width:0;border:1px solid #adb8c6;
border-radius:6px;background:#f7f8fa;color:var(--ink);padding:11px 12px;font-size:14px}
button,.button{display:inline-block;border:1px solid var(--blue);border-radius:6px;
padding:10px 16px;min-height:46px;font-weight:650;text-decoration:none;text-align:center;
background:var(--blue);color:white;cursor:pointer}.copy{flex-shrink:0}.secondary{background:white;color:var(--blue)}
.hint,.status{font-size:13px;color:var(--muted);margin:10px 0 0}.status:empty{display:none}
.services{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:24px}
.service{display:flex;flex-direction:column}.service .button{align-self:start;margin-top:auto}
ol{padding-left:1.4em;margin:4px 0 24px}li{padding-left:4px;margin:0 0 12px}
li:last-child{margin-bottom:0}strong{font-weight:700}.example{border-left:3px solid #c7d6eb;
padding:9px 14px;background:#f7f9fc;margin:0 0 22px;font-size:14px}
details{background:white;border:1px solid var(--line);border-radius:12px;padding:20px 26px;
margin-bottom:24px}summary{font-weight:700;cursor:pointer}details .details-body{padding-top:18px}
details ol{margin-bottom:18px}.notice{font-size:14px}.notice a{font-weight:650}
code{font:inherit;background:#eff4ff;padding:1px 5px;border-radius:3px;overflow-wrap:anywhere}
@media(max-width:680px){main{padding:28px 16px 40px}.header-inner{padding:16px}.panel{padding:20px}
.services{grid-template-columns:1fr;gap:0}.connection-row{align-items:stretch;flex-direction:column}
.copy{align-self:start}details{padding:18px 20px}h2{font-size:21px}}
</style>
</head>
<body>
<header><div class="header-inner"><a class="brand" href="/">NewsTV Connect</a></div></header>
<main>
<p class="eyebrow">動画ライブラリ · AI連携</p>
<h1>動画の事例を、いつものAIで。</h1>
<p class="intro muted">ChatGPT・Claudeから、あなた自身の閲覧権限で動画を検索・参照できます。
最初に接続先を登録し、会社のGoogleアカウントでログインしてください。</p>

<section class="panel" aria-labelledby="settings-heading">
<h2 id="settings-heading">共通の接続設定</h2>
<p>接続名は <strong>動画ライブラリ</strong>、認証は <strong>OAuth</strong> を選びます。</p>
<div class="connection-row">
<div class="url-field"><label for="mcp-url">MCPサーバーURL</label>
<input id="mcp-url" type="text" value="__MCP_RESOURCE__" readonly spellcheck="false"
aria-describedby="copy-help copy-status"></div>
<button id="copy-url" class="copy" type="button" aria-controls="mcp-url">URLをコピー</button>
</div>
<p id="copy-help" class="hint">コピーできない場合は、URL欄を選択して手動でコピーしてください。</p>
<p id="copy-status" class="status" role="status" aria-live="polite" aria-atomic="true"></p>
</section>

<div class="services">
<section class="panel service" aria-labelledby="chatgpt-heading">
<h2 id="chatgpt-heading">ChatGPTで使う</h2>
<ol>
<li>下のボタンからプラグイン画面を開き、
<strong>＋ → Add custom MCP server</strong> を選びます。</li>
<li>上の接続名・URL・OAuthを設定し、画面の案内に沿って登録・Googleログインを進めます。</li>
<li>作成したプラグインをインストールし、会話で <strong>@動画ライブラリ</strong> を選びます。</li>
</ol>
<p class="example">例：「食品ブランドの動画事例を探して」</p>
<a class="button" href="https://chatgpt.com/plugins" target="_blank" rel="noopener noreferrer"
aria-label="ChatGPTのプラグイン画面を開く（新しいタブ）">ChatGPTを開く
<span aria-hidden="true">↗</span></a>
</section>
<section class="panel service" aria-labelledby="claude-heading">
<h2 id="claude-heading">Claudeで使う</h2>
<ol>
<li>下のボタンからコネクタ画面を開き、<strong>動画ライブラリ → Connect</strong> を選びます。
個人で追加する場合は「Add custom connector」から上の接続設定を入力します。</li>
<li>会社のGoogleアカウントでログインし、本人の動画閲覧への接続を許可します。</li>
<li>会話の <strong>＋ → Connectors</strong> から動画ライブラリを有効にして、検索を頼みます。</li>
</ol>
<p class="example">組織の一覧に見つからない場合は、下の管理者向け手順を確認してください。</p>
<a class="button" href="https://claude.ai/customize/connectors"
target="_blank" rel="noopener noreferrer"
aria-label="Claudeのコネクタ画面を開く（新しいタブ）">Claudeを開く
<span aria-hidden="true">↗</span></a>
</section>
</div>

<details>
<summary>管理者向け：組織へ接続先を追加する</summary>
<div class="details-body">
<p>追加ボタンが表示されない場合は、ワークスペースの管理者に登録を依頼してください。
ClaudeのTeam・Enterpriseでは、組織での登録後に各利用者が自分のアカウントで接続します。</p>
<ol>
<li>Claudeの組織コネクタ画面で <strong>Add → Custom → Web</strong> を選び、
上の接続名とURLを入力します。</li>
<li>認証を求める設定を選び、OAuth clientは <strong>Register automatically</strong> を選びます。
個人でコネクタを追加する場合も同じ設定です。</li>
<li>組織へ追加した後、利用者にこのページを案内し、各自でConnectとGoogleログインを進めてもらいます。</li>
</ol>
<a class="button secondary" href="https://claude.ai/admin-settings/connectors"
target="_blank" rel="noopener noreferrer"
aria-label="Claudeの組織コネクタ管理画面を開く（新しいタブ）">Claudeの組織設定を開く
<span aria-hidden="true">↗</span></a>
</div>
</details>

<section class="panel notice" aria-labelledby="manage-heading">
<h2 id="manage-heading">接続した後は</h2>
<p>通常は初回ログイン後に接続を自動更新します。接続を解除した場合や、長期間使わなかった場合は、再度ログインしてください。</p>
<p><a href="/mcp-auth/connections">接続の管理</a> から、許可した接続を確認・解除できます。</p>
</section>
</main>
<script>
const urlField = document.getElementById('mcp-url');
const copyStatus = document.getElementById('copy-status');
urlField.addEventListener('click', () => urlField.select());
document.getElementById('copy-url').addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(urlField.value);
    copyStatus.textContent = 'URLをコピーしました。接続設定のURL欄に貼り付けてください。';
  } catch {
    urlField.focus();
    urlField.select();
    urlField.setSelectionRange(0, urlField.value.length);
    copyStatus.textContent = 'URLを選択しました。ブラウザのコピー操作でコピーしてください。';
  }
});
</script>
</body>
</html>
"""
