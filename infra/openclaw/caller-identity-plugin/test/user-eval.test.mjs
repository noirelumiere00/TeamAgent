import test from 'node:test';
import assert from 'node:assert/strict';
import { createCallerIdentityPlugin } from '../dist/index.js';

function setup() {
  const hooks = new Map();
  const logs = [];
  createCallerIdentityPlugin({ env: {
    TEAMAGENT_CALLER_CLAIM_SECRET: 'x'.repeat(48), SLACK_TEAM_ID: 'T07MU5P2PBR',
  }}).register({ registerInteractiveHandler() {},
    logger: { info: line => logs.push(line), warn: line => logs.push(line) },
    on: (name, fn) => hooks.set(name, fn) });
  const ctx = { runId: 'run-1' };
  const send = (text, eventCtx = ctx, eventRunId = eventCtx.runId) =>
    hooks.get('reply_payload_sending')({ runId: eventRunId, payload: {text} }, eventCtx)?.payload?.text ?? text;
  const remember = (url, event = {}, eventCtx = ctx) => hooks.get('after_tool_call')({
    toolName: 'teamagent__oauth_connect',
    result: {content: [{type: 'text', text: JSON.stringify({slack_url: url})}]}, ...event,
  }, eventCtx);
  return { hooks, ctx, send, remember, logs };
}
const fallback = '連携は Aico の DM に『連携』とだけ送ると、正しいリンクをお出しします';
// 2026-10-15 の実際の文字列（省略記号も URL の一部）。
const fake = 'https://connect.newstv.co.jp/oauth/slack?state=eyJhbG…QEpU';
const real = 'https://connect.newstv.co.jp/slack/oauth/start/issued_token-09==';
const google = 'https://connect.newstv.co.jp/oauth2/start/issued_token-09=';
const renders = [url => url, url => `<${url}|連携>`, url => `[連携](${url})`];
for (const [index, render] of renders.entries()) {
  test(`malformed link is replaced in rendered form ${index}`, () => {
    const { send, logs } = setup();
    assert.equal(send(`説明 ${render(fake)}`), `説明 ${fallback}`);
    assert.ok(logs.some(line => line.includes('diagnostic=CONNECT-Z03')));
    assert.ok(logs.every(line => !line.includes('eyJhbG') && !line.includes('QEpU')));
  });
  test(`allowed start links survive without run information ${index}`, () => {
    const { send, logs } = setup();
    for (const url of [real, google, real.replace(/=+$/, '')]) {
      assert.equal(send(render(url)), render(url));
      assert.equal(send(render(url), {}), render(url));
      assert.equal(send(render(url), {}, 'run-1'), render(url));
      assert.equal(send(render(url), {runId: 'other'}, 'run-1'), render(url));
    }
    assert.ok(!logs.some(line => line.includes('CONNECT-Z03')));
  });
  test(`same run link restores malformed link after agent_end ${index}`, () => {
    const { hooks, ctx, send, remember, logs } = setup();
    remember(real);
    hooks.get('agent_end')({runId: ctx.runId}, ctx);
    assert.equal(send(render(fake)), real);
    assert.ok(logs.some(line => line.includes('diagnostic=CONNECT-Z03')));
    assert.ok(logs.every(line => !line.includes('issued_token') && !line.includes('eyJhbG')));
    ctx.runId = 'run-2';
    assert.equal(send(render(fake)), fallback);
    assert.equal(send(render(real)), render(real));
  });
}

test('Google start link is also usable as the same run replacement', () => {
  const {send, remember} = setup();
  remember(google);
  assert.equal(send('https://connect.newstv.co.jp/oauth/google?state=bad'), google);
});

test('only permitted URL shapes survive, even when malformed URL came from tool', () => {
  const malformed = [
    fake, 'https://connect.newstv.co.jp/oauth/google',
    'https://connect.newstv.co.jp/connect', 'https://connect.newstv.co.jp/help?state=abc',
    `${real}?state=abc`, `${google}?scope=mail`, `${real}#fragment`,
    'https://connect.newstv.co.jp/oauth2/start/',
    'https://connect.newstv.co.jp/oauth2/start/token…tail',
    'https://connect.newstv.co.jp/slack/oauth/start/token...tail',
    'https://connect.newstv.co.jp/oauth2/start/token/extra',
    'https://connect.newstv.co.jp/oauth2/start/token!tail',
    'https://connect.newstv.co.jp/oauth2/start/to=ken',
    'https://connect.newstv.co.jp/oauth2/start/token===',
    'http://connect.newstv.co.jp/oauth2/start/token',
  ];
  for (const url of malformed) {
    const {send, remember} = setup();
    remember(url);
    for (const render of renders) assert.equal(send(render(url)), fallback, url);
  }
});

test('missing, conflicting or unrelated tool results never supply replacement links', () => {
  for (const [event, eventCtx] of [
    [{toolName: 'teamagent__search'}, undefined],
    [{error: 'failed'}, undefined],
    [{result: {isError: true, url: real}}, undefined],
    [{runId: 'other'}, undefined], [{}, {}],
  ]) {
    const {send, remember} = setup();
    remember(real, event, eventCtx);
    assert.equal(send(fake), fallback);
    assert.equal(send(real, {}), real);
  }
  const {send, remember} = setup();
  remember(real);
  assert.equal(send(fake, {}), fallback);
  assert.equal(send(fake, {runId: 'other'}, 'run-1'), fallback);
});

test('unrelated URLs are unchanged', () => {
  const {send} = setup();
  for (const url of ['https://connect.newstv.co.jp/help',
    'https://connect.newstv.co.jp/r/report-token',
    'https://example.test/oauth/slack?state=eyJhbG…QEpU',
    'https://connect.newstv.co.jp.example.test/connect?state=abc']) {
    for (const render of renders) assert.equal(send(render(url)), render(url));
  }
});

test('malformed links in labels and titles are replaced', () => {
  const { send } = setup();
  const fakeUpper = fake.replace('connect.newstv.co.jp', 'CONNECT.NEWSTV.CO.JP');
  for (const text of [`[${fakeUpper}](https://example.test/help)`,
    `<https://example.test/help|${fakeUpper}>`,
    `[説明](https://example.test/help "${fakeUpper}")`]) {
    const out = send(text);
    assert.ok(!out.includes(fakeUpper));
    assert.ok(out.includes(fallback));
    assert.ok(out.includes('https://example.test/help'));
  }
});

test('missing table cells normalize while prose and table syntax retain behavior', () => {
  const { send } = setup();
  assert.equal(send('| ハンバーグ | 6本 | 1,230,000 | 205,000 | 507,700 | — | - | — |'),
    '| ハンバーグ | 6本 | 1,230,000 | 205,000 | 507,700 | なし | なし | なし |');
  assert.equal(send('説明 — 補足'), '説明、補足');
  assert.equal(send('| --- | :---: |'), '| --- | :---: |');
  assert.equal(send('```\n| — | - |\n```'), '```\n| — | - |\n```');
});

const sheet = 'https://docs.google.com/spreadsheets/d/19Y_IRJxr1oPLqNwgpi9nHRkyMqZPuw13ZikN2gD6pWQ/edit#gid=386118222';
const typo = sheet.replace('IRJxr', 'IRJxl');
test('iter2 restores near-identical evidence URLs only in the authoritative run', () => {
  const {send, remember, hooks, ctx, logs} = setup();
  remember(sheet, {toolName: 'teamagent__search'});
  hooks.get('agent_end')({runId: ctx.runId}, ctx);
  for (const render of renders) assert.equal(send(render(typo)), render(sheet));
  assert.equal(send(sheet), sheet);
  assert.equal(send('https://docs.google.com/help'), 'https://docs.google.com/help');
  assert.equal(send(sheet.replace('docs.google.com', 'docs.google.cpm')), sheet.replace('docs.google.com', 'docs.google.cpm'));
  assert.equal(send(typo, {runId: 'other'}, 'run-1'), typo);
  assert.equal(send(typo, {}), typo);
  assert.equal(send(typo, {runId: 'other'}), typo);
  assert.ok(logs.some(line => line.includes('CONNECT-Z04')));
});

function receive(hooks, content, {user = 'U12345678', runId = 'run-1', messageId = '123.456'} = {}) {
  hooks.get('message_received')({content, senderId: user, messageId,
    runId, metadata: {guildId: 'T07MU5P2PBR', to: `user:${user}`}},
  {channelId: 'slack', sessionKey: 'agent:teamagent:slack:direct:u12345678',
    conversationId: `user:${user}`, senderId: user, messageId, runId});
}
for (const query of ['比較して何が効いた？', 'どっちが勝ち？', '社内でいうビデオリリースって何？',
  'TTOとは？', '切り抜きとは？', 'VVSとは？', 'タテガタとは？',
  'ショートドラマとは？', 'スワイプ LPとは？', 'ソリューションプランとは？', 'VSEOとは？',
  '検索面とは？', 'お土産資料とは？', 'ケイパとは？', '感触とは？', '営業 FBとは？', 'カルテとは？', '二次利用費とは？']) {
  test(`iter2 requires evidence once: ${query}`, () => {
    const {hooks, ctx} = setup();
    receive(hooks, query);
    const finalize = () => hooks.get('before_agent_finalize')({runId: ctx.runId,
      lastAssistantMessage: 'クイックディナーが効いた。平均171,325再生。15〜90秒です。'}, ctx);
    const out = finalize();
    assert.equal(out?.action, 'revise');
    assert.match(out.retry.instruction, /search/);
    assert.equal(out.retry.maxAttempts, 1);
    assert.equal(finalize(), undefined);
  });
}
test('iter2 leaves casual conversation alone', () => {
  const {hooks, ctx} = setup();
  receive(hooks, 'こんにちは');
  assert.equal(hooks.get('before_agent_finalize')({runId: ctx.runId,
    lastAssistantMessage: 'こんにちは。'}, ctx), undefined);
});
test('iter2 strips only the final unsolicited question or handoff', () => {
  for (const tail of ['検索結果で見つかった提案資料を確認しますか？', '何に使いたいですか？',
    '進行中なら小池さんへ確認してください。']) {
    const {send} = setup();
    assert.equal(send(`資料を確認しました。\n${tail}`), '資料を確認しました。');
  }
});
test('iter2 preserves required input and approval based on tool markers', () => {
  for (const marker of ['needs_input', 'requires_confirmation', 'confirmation_required', 'needs_authorization']) {
    const {send, remember} = setup();
    remember('', {toolName: 'teamagent__search', result: {status: marker}});
    const text = '入力を確認しました。何に使いたいですか？';
    assert.equal(send(text), text);
  }
  const {send} = setup();
  assert.equal(send('メールを送信しますか？'), 'メールを送信しますか？');
});

test('iter2 URL boundaries: edits, ambiguity, errors, and exact evidence', () => {
  for (const bad of [sheet.replace('IRJxr', 'IRJx'), sheet.replace('IRJxr', 'IRJxrr'),
    sheet.replace('IRJxr', 'IRabc')]) {
    const {send, remember} = setup();
    remember(sheet, {toolName: 'teamagent__search'});
    assert.equal(send(`[資料](${bad})。`), `[資料](${sheet})。`);
  }
  const {send, remember} = setup();
  remember([sheet, typo].join('\n'), {toolName: 'teamagent__search'});
  assert.equal(send(typo), typo);
  const ambiguous = sheet.replace('IRJxr', 'IRJxm');
  assert.equal(send(ambiguous), ambiguous);
  for (const event of [{error: 'failed'}, {runId: 'other'}, {result: {isError: true, url: sheet}}]) {
    const sample = setup();
    sample.remember(sheet, {toolName: 'teamagent__search', ...event});
    assert.equal(sample.send(typo), typo);
  }
});
test('iter2 uses recent evidence only from the same bound conversation', () => {
  const {hooks, ctx, send, remember} = setup();
  receive(hooks, '資料を見せて');
  remember(sheet, {toolName: 'teamagent__search'});
  hooks.get('agent_end')({runId: ctx.runId}, ctx);
  ctx.runId = 'run-2';
  // A second received message binds a new run in the same conversation.
  const user = 'U12345678';
  hooks.get('message_received')({content: '続き', senderId: user, messageId: '124.456',
    runId: ctx.runId, metadata: {guildId: 'T07MU5P2PBR', to: `user:${user}`}},
  {channelId: 'slack', sessionKey: 'agent:teamagent:slack:direct:u12345678',
    conversationId: `user:${user}`, senderId: user, messageId: '124.456', runId: ctx.runId});
  assert.equal(send(typo), sheet);
  ctx.runId = 'unbound';
  assert.equal(send(typo), typo);
});
test('iter2 tool result avoids redundant revise; no binding never revises', () => {
  const {hooks, ctx, remember} = setup();
  receive(hooks, '比較して何が効いた？');
  remember(sheet, {toolName: 'teamagent__search'});
  assert.equal(hooks.get('before_agent_finalize')({runId: ctx.runId,
    lastAssistantMessage: '平均205,000です。'}, ctx), undefined);
  const other = setup();
  assert.equal(other.hooks.get('before_agent_finalize')({runId: 'run-1',
    lastAssistantMessage: '平均205,000です。'}, other.ctx), undefined);
});
test('iter2 needs_input in MCP JSON survives agent_end; plain text is not a marker', () => {
  for (const result of [{content: [{type: 'text', text: '{"status":"needs_input"}'}]},
    {structuredContent: {requires_confirmation: true}}]) {
    const {hooks, ctx, send, remember} = setup();
    remember('', {toolName: 'teamagent__omiyage_report_submit', result});
    hooks.get('agent_end')({runId: ctx.runId}, ctx);
    assert.equal(send('対象を選択してください。何に使いたいですか？'), '対象を選択してください。何に使いたいですか？');
  }
  const {send, remember} = setup();
  remember('', {toolName: 'teamagent__search', result: {text: 'status=needs_input'}});
  assert.equal(send('資料です。何に使いたいですか？'), '資料です。');
});

test('iter2 recent evidence is never shared between callers in a shared session', () => {
  const {hooks, ctx, send, remember} = setup();
  receive(hooks, '資料を見せて');
  remember(sheet, {toolName: 'teamagent__search'});
  hooks.get('agent_end')({runId: ctx.runId}, ctx);
  ctx.runId = 'run-2';
  receive(hooks, '続き', {user: 'U87654321', runId: ctx.runId, messageId: '124.456'});
  assert.equal(send(typo), typo);
});

test('iter2 connection tool result preserves the final guidance question', () => {
  const {send, remember} = setup();
  remember(real);
  const text = `${real}\n設定画面を開きますか？`;
  assert.equal(send(text), text);
});

test('iter2 trims an offer after code and cancels an otherwise empty reply', () => {
  const {hooks, ctx, send} = setup();
  assert.equal(send('説明。\n```\n何に使いたいですか？\n```\n資料を確認しますか？'),
    '説明。\n```\n何に使いたいですか？\n```');
  assert.equal(send('説明。\n```\n何に使いたいですか？'), '説明。\n```\n何に使いたいですか？');
  const out = hooks.get('reply_payload_sending')({runId: ctx.runId,
    payload: {text: '何に使いたいですか？'}}, ctx);
  assert.equal(out?.cancel, true);
});
