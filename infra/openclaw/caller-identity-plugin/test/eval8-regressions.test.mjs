import test from 'node:test';
import assert from 'node:assert/strict';
import {createCallerIdentityPlugin, scrubOutboundReplyText} from '../dist/index.js';

function setup(content) {
  const hooks = new Map();
  const ctx = {runId: 'eval8-run', sessionKey: 'agent:teamagent:slack:direct:u12345678',
    senderId: 'U12345678', channelId: 'U12345678'};
  createCallerIdentityPlugin({env: {
    TEAMAGENT_CALLER_CLAIM_SECRET: 'x'.repeat(48), SLACK_TEAM_ID: 'T07MU5P2PBR',
    SEARCH_DIRECT_ANSWER: '1', SEARCH_DIRECT_ANSWER_ALLOWED_EMAILS: '*',
    SEARCH_DIRECT_ANSWER_KINDS: 'not_found,list_fast,comparison',
  }}).register({registerInteractiveHandler() {}, logger: {info() {}, warn() {}},
    on: (name, fn) => hooks.set(name, fn)});
  const receive = (content, messageId = '1792041600.123456') => hooks.get('message_received')({content, senderId: ctx.senderId,
    messageId, runId: ctx.runId,
    metadata: {guildId: 'T07MU5P2PBR', to: `user:${ctx.senderId}`}},
  {...ctx, channelId: 'slack', conversationId: `user:${ctx.senderId}`, messageId});
  receive(content);
  const remember = data => hooks.get('after_tool_call')({toolName: 'teamagent__search',
    runId: ctx.runId, result: {content: [{type: 'text', text: JSON.stringify(data)}]}}, ctx);
  const send = (text, media = {}) => {
    const payload = {text, ...media};
    const result = hooks.get('reply_payload_sending')({runId: ctx.runId, payload}, ctx);
    return result?.cancel ? '' : result?.payload?.text ?? text;
  };
  return {hooks, ctx, remember, send, receive};
}

for (const query of ['社内でいうビデオリリースって何？', '社内でいう{term}って何？',
  '{newspaper}の{exhibition}って何？うちも関わってる？']) {
  for (const otherTool of [false, true]) {
    test(`eval9 internal questions require search despite unrelated tools: ${query} ${otherTool}`, () => {
      const p = setup(query);
      if (otherTool) p.hooks.get('after_tool_call')({runId: p.ctx.runId,
        toolName: 'teamagent__web_research', result: {content: [{type: 'text', text: '外部Web情報'}]}}, p.ctx);
      const event = {runId: p.ctx.runId, lastAssistantMessage: '社内資料で確認するのが確実です。'};
      assert.equal(p.hooks.get('before_agent_finalize')(event, p.ctx)?.action, 'revise');
      assert.equal(p.hooks.get('before_agent_finalize')(event, p.ctx), undefined);
    });
  }
  test(`eval9 internal questions search before web: ${query}`, () => {
    const p = setup(query);
    const call = {runId: p.ctx.runId, toolName: 'teamagent__web_research',
      toolCallId: 'web-call', params: {query: '公開の展示会情報'}};
    const ctx = {...p.ctx, toolName: call.toolName, toolCallId: call.toolCallId};
    assert.equal(p.hooks.get('before_tool_call')(call, ctx)?.block, true);
    p.remember({answer: '社内資料では関与を確認できませんでした。', found: false});
    assert.notEqual(p.hooks.get('before_tool_call')(call, ctx)?.block, true);
    assert.equal(p.hooks.get('before_agent_finalize')({runId: p.ctx.runId,
      lastAssistantMessage: '社内資料では関与を確認できませんでした。'}, p.ctx), undefined);
  });
  test(`eval9 exhausted retry does not restore an unsearched definition: ${query}`, () => {
    const p = setup(query);
    const draft = '自社メニューです。SOUL.mdを見て検索すれば出ると思います。';
    p.hooks.get('before_agent_finalize')({runId: p.ctx.runId, lastAssistantMessage: draft}, p.ctx);
    assert.equal(p.send(draft), '社内資料の確認を完了できませんでした。');
  });
}

test('eval9 outbound guard removes internal file and configuration names but preserves source URLs', () => {
  for (const name of ['SOUL.md', 'IDENTITY.md', 'openclaw.config.json5', 'SEARCH_DRIVE_FIRST']) {
    const p = setup('社内でいうビデオリリースって何？');
    const out = p.send(`動画版ニュースリリースです。\n\n${name} に書いてあります。\n\n出典: https://example.test/SOUL.md`);
    assert.match(out, /動画版ニュースリリース/);
    assert.doesNotMatch(out.replace('https://example.test/SOUL.md', ''), /SOUL\.md|IDENTITY\.md|openclaw\.config\.json5|SEARCH_DRIVE_FIRST/);
    assert.match(out, /https:\/\/example.test\/SOUL\.md/);
    assert.doesNotMatch(scrubOutboundReplyText(`${name}に記載。動画版ニュースリリースです。`).text,
      /SOUL\.md|IDENTITY\.md|openclaw\.config\.json5|SEARCH_DRIVE_FIRST/);
  }
});

test('eval9 removes rounded posted metrics while preserving the explanation', () => {
  const p = setup('比較して何が効いた？');
  p.remember({found: true, video_metrics_posted: true,
    video_metrics_evidence: {text: '📊 動画ごとの数字\n1. 331,300 再生・保存 127', urls: []}});
  const out = p.send('再生33万でした。\n調理実演が関心につながった可能性があります。');
  assert.doesNotMatch(out, /33万/);
  assert.match(out, /調理実演/);
});

for (const searchAgain of [false, true]) {
  for (const direct of [false, true]) {
    test(`eval9 effect followup retains the metrics receipt across runs: ${searchAgain} ${direct}`, () => {
      const p = setup('{campaign_a}と{campaign_b}の結果を見せて');
      const metrics = '📊 動画ごとの数字\n1. 331,300 再生・保存 127';
      p.remember(direct ? {status: 'posted', delivered: true,
        delivery: {kind: 'search_direct_answer', answer_kind: 'video_metrics'},
        direct_delivery: {answer_kind: 'video_metrics', answer: metrics}}
        : {found: true, video_metrics_posted: true,
          video_metrics_evidence: {text: metrics, urls: []}});
      p.hooks.get('agent_end')({runId: p.ctx.runId, success: true}, p.ctx);
      p.ctx.runId = 'eval9-followup';
      p.receive('比較して何が効いた？', '1792041660.123456');
      if (searchAgain) p.remember({found: true, answer: '調理実演で利用場面を伝えています。'});
      const out = p.send('| 再生数 | 331,300 |\n調理実演が関心につながった可能性があります。');
      assert.doesNotMatch(out, /331,300/);
      assert.match(out, /調理実演/);
    });
  }
}

test('eval9 only a confirmed direct search delivery is a metrics receipt', () => {
  for (const change of [{delivered: false}, {status: 'failed'}, {delivery: {kind: 'other'}}]) {
    const p = setup('比較して何が効いた？');
    p.remember({status: 'posted', delivered: true,
      delivery: {kind: 'search_direct_answer'},
      direct_delivery: {answer: '📊 動画ごとの数字\n1. 331,300 再生・保存 127'}, ...change});
    assert.match(p.send('再生331,300でした。'), /331,300/);
  }
});

test('Drive fallback restores only the displayed document link', () => {
  const p = setup('{client_a}の{campaign_a}は今どうなっている？');
  const url = 'https://drive.google.com/file/d/REPORT/view';
  p.remember({answer: '2026-09-30 時点では実施は完了し、今期は見送りです。', found: true,
    displayed_refs: [{kind: 'drive', url}], hits: [{url: 'https://docs.google.com/spreadsheets/d/LEDGER/edit'}]});
  const out = p.send('状態を説明します。');
  assert.ok(out.includes(url));
  assert.doesNotMatch(out, /spreadsheets/u);
});

test('posted metrics cannot be repeated even when the exact numbers are grounded', () => {
  const p = setup('比較して何が効いた？');
  p.remember({found: true, answer: '調理実演が関心につながった可能性があります。',
    video_metrics_posted: true,
    video_metrics_evidence: {text: '📊 動画ごとの数字\n1. 331,300 再生・保存 127、調理実演', urls: []},
    sources: [{url: 'https://drive.google.com/file/d/REPORT/view'}]});
  const out = p.send('再生331,300、保存127でした。\n調理実演が関心につながった可能性があります。');
  assert.doesNotMatch(out, /331,300|127/);
  assert.match(out, /調理実演/);
  assert.match(out, /https:\/\/drive.google.com/u);
});

test('posted metrics leave the exact text unchanged when no sentence repeats them', () => {
  const p = setup('比較して何が効いた？');
  p.remember({found: true, video_metrics_posted: true,
    video_metrics_evidence: {text: '📊 331,300 再生・保存 127', urls: []}});
  const text = '調理実演が関心につながった可能性があります。同じ行で理由を説明します。\n\n' +
    '完成品を見せる構成も関係したと考えられます。こちらも同じ段落です。\n\n\n' +
    '元資料からの推測です。';
  assert.equal(p.send(text), text);
});

test('removing posted metrics preserves surrounding sentences, paragraphs and blank lines', () => {
  const p = setup('比較して何が効いた？');
  p.remember({found: true, video_metrics_posted: true,
    video_metrics_evidence: {text: '📊 331,300 再生・保存 127', urls: []}});
  const text = '調理実演が関心につながった可能性があります。再生331,300でした。同じ行で理由を説明します。\n\n' +
    '完成品を見せる構成も関係したと考えられます。\n\n保存127でした。\n\n' +
    '元資料からの推測です。こちらも同じ段落です。';
  const expected = '調理実演が関心につながった可能性があります。同じ行で理由を説明します。\n\n' +
    '完成品を見せる構成も関係したと考えられます。\n\n\n\n' +
    '元資料からの推測です。こちらも同じ段落です。';
  assert.equal(p.send(text), expected);
});

test('status reply drops the next action but retains its source', () => {
  const p = setup('{campaign_a}は今どうなっている？');
  p.remember({found: true, answer: '実施済みです。', sources: [{url: 'https://drive.google.com/file/d/REPORT/view'}]});
  const out = p.send('実施済みです。\n\n次のアクション\n再提案しましょう。\n\n出典: https://drive.google.com/file/d/REPORT/view');
  assert.doesNotMatch(out, /次のアクション|再提案/);
  assert.match(out, /REPORT/);
});

test('source explanation requires a new search even without a refusal draft', () => {
  const p = setup('昨日のあなたの返事ではなく、元の資料を根拠に説明して');
  const result = p.hooks.get('before_agent_finalize')({runId: p.ctx.runId,
    lastAssistantMessage: '元資料では調理実演を紹介しています。'}, p.ctx);
  assert.equal(result?.action, 'revise');
});

test('a media reply always has text', () => {
  const p = setup('{campaign_a}は今どうなっている？');
  p.remember({answer: '実施済みです。', found: true});
  assert.ok(p.send('', {mediaUrl: 'https://example.test/report.pdf'}).trim());
});

test('source explanation keeps the exact user request for the 0034 lookup', () => {
  const query = '昨日のあなたの返事ではなく、元の資料を根拠に説明して';
  const p = setup(query);
  const call = p.hooks.get('before_tool_call')({runId: p.ctx.runId,
    toolName: 'teamagent__search', toolCallId: 'source-call', params: {query: '営業FBの資料'}},
  {...p.ctx, toolName: 'teamagent__search', toolCallId: 'source-call'});
  assert.equal(call?.params?.query, query);
});
