import test from 'node:test';
import assert from 'node:assert/strict';
import {createCallerIdentityPlugin} from '../dist/index.js';

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
  hooks.get('message_received')({content, senderId: ctx.senderId,
    messageId: '1792041600.123456', runId: ctx.runId,
    metadata: {guildId: 'T07MU5P2PBR', to: `user:${ctx.senderId}`}},
  {...ctx, channelId: 'slack', conversationId: `user:${ctx.senderId}`, messageId: '1792041600.123456'});
  const remember = data => hooks.get('after_tool_call')({toolName: 'teamagent__search',
    runId: ctx.runId, result: {content: [{type: 'text', text: JSON.stringify(data)}]}}, ctx);
  const send = (text, media = {}) => {
    const payload = {text, ...media};
    const result = hooks.get('reply_payload_sending')({runId: ctx.runId, payload}, ctx);
    return result?.cancel ? '' : result?.payload?.text ?? text;
  };
  return {hooks, ctx, remember, send};
}

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
