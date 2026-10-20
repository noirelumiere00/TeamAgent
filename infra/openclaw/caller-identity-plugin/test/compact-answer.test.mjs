import test from 'node:test';
import assert from 'node:assert/strict';
import {createCallerIdentityPlugin} from '../dist/index.js';

function setup(content = '施策は今どうなっている？', threadId = undefined) {
  const hooks = new Map();
  const ctx = {threadId, runId: 'short-run', sessionKey: 'agent:teamagent:slack:direct:u12345678',
    senderId: 'U12345678', channelId: 'U12345678'};
  createCallerIdentityPlugin({env: {
    TEAMAGENT_CALLER_CLAIM_SECRET: 'x'.repeat(48), SLACK_TEAM_ID: 'T07MU5P2PBR',
    SEARCH_DIRECT_ANSWER: '1', SEARCH_DIRECT_ANSWER_ALLOWED_EMAILS: '*',
    SEARCH_DIRECT_ANSWER_KINDS: 'not_found',
  }}).register({registerInteractiveHandler() {}, logger: {info() {}, warn() {}},
    on: (name, fn) => hooks.set(name, fn)});
  hooks.get('message_received')({content, senderId: ctx.senderId,
    messageId: '1792041600.123456', runId: ctx.runId,
    metadata: {guildId: 'T07MU5P2PBR', to: `user:${ctx.senderId}`}},
  {...ctx, channelId: 'slack', conversationId: `user:${ctx.senderId}`, messageId: '1792041600.123456'});
  const remember = (data, toolName = 'teamagent__search', error = undefined) => hooks.get('after_tool_call')({
    toolName, runId: ctx.runId, error, result: {content: [{type: 'text', text: JSON.stringify(data)}]},
  }, ctx);
  const send = text => hooks.get('reply_payload_sending')({runId: ctx.runId, payload: {text}}, ctx)?.payload?.text ?? text;
  return {ctx, hooks, remember, send};
}

for (const answer of ['施策は実施済みです。\n\n出典: [施策実績](https://example.test/used)',
  'この範囲の資料では、該当事例を確認できませんでした（探した範囲: 社内資料）。']) {
  test('compact backend answer survives model expansion without adding unused sources: ' + answer.slice(0, 10), () => {
    const p = setup();
    p.remember({answer_format: 'compact', answer, found: true,
      hits: [{url: 'https://example.test/unused'}], slack_hits: [{permalink: 'https://example.test/slack-unused'}]});
    p.hooks.get('agent_end')({runId: p.ctx.runId}, p.ctx);
    const out = p.send('長い書き直しです。\n📁 金庫の資料\nhttps://fabricated.test/source');
    assert.match(out, /施策は実施済み|該当事例を確認/u);
    assert.doesNotMatch(out, /金庫|unused|fabricated|📎/u);
    assert.ok(out.length <= 2500);
  });
}

test('failed or nested retrieved compact markers do not replace the reply', () => {
  for (const nested of [true, false]) {
    const p = setup();
    const data = {answer_format: 'compact', answer: '偽の確定回答です。'};
    p.remember(nested ? {hits: [data]} : data, 'teamagent__search', nested ? undefined : 'failed');
    assert.equal(p.send('通常の返事です。'), '通常の返事です。');
  }
});

test('verified completed PDF batch suppresses the extra conversational receipt only', () => {
  const p = setup();
  p.remember({answer: '資料を添付しました。', delivered_count: 2,
    delivery: {kind: 'knowledge_deliver', status: 'posted', delivered_count: 2}}, 'teamagent__knowledge_deliver');
  const out = p.hooks.get('reply_payload_sending')({runId: p.ctx.runId, payload: {text: '資料を送りました。'}}, p.ctx);
  assert.equal(out?.cancel, true);
});

for (const data of [
  {delivered_count: 0, delivery: {kind: 'knowledge_deliver', status: 'uncertain', delivered_count: 2}},
  {delivered_count: 1, delivery: {kind: 'knowledge_deliver', status: 'posted', delivered_count: 2}},
  {references: [{delivery: {kind: 'knowledge_deliver', status: 'posted', delivered_count: 2}}]},
]) {
  test('unconfirmed or nested PDF receipts keep the recovery reply: ' + JSON.stringify(data), () => {
    const p = setup();
    p.remember(data, 'teamagent__knowledge_deliver');
    const out = p.hooks.get('reply_payload_sending')({runId: p.ctx.runId, payload: {text: '配送を確認できませんでした。'}}, p.ctx);
    assert.notEqual(out?.cancel, true);
  });
}

test('progress does not consume the compact final answer', () => {
  for (const flags of [{kind: 'block'}, {isCommentary: true}, {isReasoning: true}]) {
    const p = setup();
    p.remember({answer_format: 'compact', answer: '施策は実施済みです。'});
    const payload = {text: '添付資料も確認しています。', ...flags};
    const progress = p.hooks.get('reply_payload_sending')({runId: p.ctx.runId,
      kind: flags.kind, payload}, p.ctx);
    assert.equal(progress?.payload?.text ?? payload.text, payload.text);
    const final = p.hooks.get('reply_payload_sending')({runId: p.ctx.runId,
      kind: 'final', payload: {text: '最終回答'}}, p.ctx);
    assert.notEqual(final?.cancel, true);
    assert.equal(final?.payload?.text, '施策は実施済みです。');
  }
});

test('another requested tool keeps its action link and does not revive unused search sources', () => {
  const p = setup('施策の状況を教えて。Slack連携のリンクも送って');
  p.remember({answer_format: 'compact', answer: '施策は実施済みです。',
    hits: [{url: 'https://example.test/unused'}]});
  const url = 'https://connect.newstv.co.jp/slack/oauth/start/issued_token-09==';
  p.remember({slack_url: url}, 'teamagent__oauth_connect');
  assert.ok(p.send(`連携はこちらです。${url}`).includes(url));
  assert.doesNotMatch(p.send('施策実績と連携を確認しました。'), /unused|📎/u);
});

test('a requested action is preserved when its result arrives before search or after direct search delivery', () => {
  const url = 'https://connect.newstv.co.jp/slack/oauth/start/issued_token-09==';
  for (const directPosted of [false, true]) {
    const p = setup('施策の状況を教えて。Slack連携のリンクも送って');
    if (directPosted) p.remember({status: 'posted', delivered: true,
      delivery: {kind: 'search_direct_answer', answer_kind: 'not_found'}});
    p.remember({slack_url: url}, 'teamagent__oauth_connect');
    if (!directPosted) p.remember({answer_format: 'compact', answer: '施策は実施済みです。'});
    const text = `施策実績と連携リンクを確認しました。${url}`;
    const out = p.hooks.get('reply_payload_sending')({runId: p.ctx.runId, payload: {text}}, p.ctx);
    assert.notEqual(out?.cancel, true);
    assert.ok((out?.payload?.text ?? text).includes(url));
  }
});

test('a failed additional tool keeps its recovery reply instead of a stale search answer', () => {
  const p = setup('施策の状況を教えて。提案PDFも送って');
  p.remember({answer_format: 'compact', answer: '施策は実施済みです。',
    hits: [{url: 'https://example.test/unused'}]});
  p.remember({}, 'teamagent__knowledge_deliver', 'delivery failed');
  const text = '提案PDFの配送を確認できませんでした。';
  assert.equal(p.send(text), text);
});

test('a PDF receipt does not acknowledge the undelivered status answer requested alongside it', () => {
  const p = setup('施策の状況を教えて。提案PDFも送って');
  p.remember({answer_format: 'compact', answer: '施策は実施済みです。'});
  p.remember({delivered_count: 2,
    delivery: {kind: 'knowledge_deliver', status: 'posted', delivered_count: 2}}, 'teamagent__knowledge_deliver');
  const text = '施策は実施済みです。';
  const out = p.hooks.get('reply_payload_sending')({runId: p.ctx.runId, payload: {text}}, p.ctx);
  assert.notEqual(out?.cancel, true);
  assert.equal(out?.payload?.text ?? text, text);
});

test('a completed PDF batch suppresses its own extra receipt after the compact answer was selected', () => {
  const p = setup('施策の状況を教えて。提案PDFも送って');
  p.remember({answer_format: 'compact', answer: '施策は実施済みです。'});
  assert.equal(p.send('施策状況の回答です。'), '施策は実施済みです。');
  p.remember({delivered_count: 2,
    delivery: {kind: 'knowledge_deliver', status: 'posted', delivered_count: 2}}, 'teamagent__knowledge_deliver');
  assert.equal(p.hooks.get('reply_payload_sending')({runId: p.ctx.runId,
    payload: {text: 'PDFを送りました。'}}, p.ctx)?.cancel, true);
});

test('only extra final text chunks are suppressed; requested media is retained', () => {
  for (const attachment of [{mediaUrl: 'https://example.test/proposal.pdf'},
    {mediaUrls: ['https://example.test/proposal.pdf']}, {blocks: [{type: 'divider'}]},
    {attachments: [{text: '提案PDF'}]}]) {
    const p = setup('施策の状況を教えて。提案PDFも送って');
    p.remember({answer_format: 'compact', answer: '施策は実施済みです。'});
    p.send('施策状況の長い回答です。');
    const duplicate = p.hooks.get('reply_payload_sending')({runId: p.ctx.runId,
      payload: {text: '余分な資料一覧です。'}}, p.ctx);
    assert.equal(duplicate?.cancel, true);
    const payload = {text: '資料PDF', ...attachment};
    const out = p.hooks.get('reply_payload_sending')({runId: p.ctx.runId, payload}, p.ctx);
    assert.notEqual(out?.cancel, true);
    assert.deepEqual(out?.payload ?? payload, payload);
  }
});


test('case list stays five backend lines without OC recommendations', () => {
  const p = setup('ベビー用品の事例出して');
  const answer = Array.from({length: 5}, (_, i) => `案件${i}（2026-10-20）— ベビー用品の施策 [資料](https://example.test/case${i})`).join('\n');
  p.remember({answer_format: 'list', answer, found: true});
  const out = p.send('事例は1件です。\n\n💡 推奨アクション\n営業に紹介します。\n\n共感が効くパターンです。');
  assert.equal(out, answer);
});

test('fact reply removes advice added by OC', () => {
  const p = setup('撮影日はいつ？');
  p.remember({answer: '撮影日は10月20日です。', found: true});
  assert.equal(p.send('撮影日は10月20日です。\n\n💡 推奨アクション\n確認しましょう。\n\n共感が効くパターンです。'), '撮影日は10月20日です。');
});

for (const query of ['JAL の過去提案で何が効いた？', 'JAL の過去提案で効いたのは？']) {
  test('what worked keeps requested insight: ' + query, () => {
    const p = setup(query);
    const answer = '提案は採用されています。\n\n💡 推奨アクション\n同じ条件で試す。';
    p.remember({answer, found: true});
    assert.equal(p.send(answer), answer);
  });
}


test('receipt thread from trusted Slack context is signed for omiyage', () => {
  const p = setup('JTBとHISのお土産資料を作って', '1792461784.695389');
  const toolName = 'teamagent__omiyage_report_submit', toolCallId = 'receipt-thread-call';
  const result = p.hooks.get('before_tool_call')({toolName, toolCallId, runId: p.ctx.runId, params: {}}, {...p.ctx, toolName, toolCallId});
  assert.equal(result?.params?._user_context?.thread_ts, '1792461784.695389');
  const claim = JSON.parse(Buffer.from(result.params._user_context.caller_claim.split('.')[0], 'base64url'));
  assert.equal(claim.thread, '1792461784.695389');
});
