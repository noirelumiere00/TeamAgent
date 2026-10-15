import test from 'node:test';
import assert from 'node:assert/strict';
import { createCallerIdentityPlugin } from '../dist/index.js';

function setup(options = {}) {
  const hooks = new Map();
  createCallerIdentityPlugin({...options, env: {
    TEAMAGENT_CALLER_CLAIM_SECRET: 'x'.repeat(48), SLACK_TEAM_ID: 'T07MU5P2PBR',
  }}).register({registerInteractiveHandler() {}, logger: {info() {}, warn() {}},
    on: (name, fn) => hooks.set(name, fn)});
  const ctx = {runId: 'run-1'};
  const remember = (result, event = {}, eventCtx = ctx) => hooks.get('after_tool_call')({
    toolName: 'teamagent__search', result, ...event,
  }, eventCtx);
  const send = (text, eventCtx = ctx, eventRunId = eventCtx.runId) => {
    const out = hooks.get('reply_payload_sending')({runId: eventRunId, payload: {text}}, eventCtx);
    assert.notEqual(out?.cancel, true);
    return out?.payload?.text ?? text;
  };
  return {hooks, ctx, remember, send};
}

const table = values => ['| アカウント | 再生数 | 保存数 |', '| --- | ---: | ---: |',
  ...values.map(([name, plays, saves]) => `| ${name} | ${plays} | ${saves} |`)].join('\n');
const fabricated = table([['@creator-a', '181,200', '127'], ['@creator-b', '168,900', '96']]);
const actual = table([['@creator-a', '331,300', '127'], ['@creator-b', '184,100', '96']]);
const evidence = {content: [{type: 'text', text: JSON.stringify({
  result: {posts: [{play_count: 331300, save_count: 127}, {play_count: 184100, save_count: 96}]},
})}]};

test('iter3 removes fabricated per-creator metrics after agent_end and preserves prose', () => {
  const {hooks, ctx, remember, send} = setup();
  remember(evidence);
  hooks.get('agent_end')({runId: ctx.runId}, ctx);
  assert.equal(send(actual), actual);
  const out = send(`共通する訴求は時短です。\n\n${fabricated}\n\n素材の確認もできました。`);
  assert.doesNotMatch(out, /181,200|168,900|@creator/);
  assert.match(out, /📊/);
  assert.match(out, /共通する訴求は時短です。/);
  assert.match(out, /素材の確認もできました。/);
});

test('iter3 preserves exact supported metrics and canonicalizes counts like NumberGrounder', () => {
  const {remember, send} = setup();
  remember(evidence);
  assert.equal(send(actual), actual);
  const canonical = table([['@creator-a', '３３．１３万', '１２７'], ['@creator-b', '18.410万', '９６']]);
  assert.equal(send(canonical), canonical);
  assert.doesNotMatch(send(table([['@creator-a', '１８１，２００', '１２７']])), /１８１，２００/);
});

test('iter3 metric lists are grounded even when count labels occur only in the heading', () => {
  for (const text of [
    '動画ごとの結果：\n- @creator-a：再生181,200、保存127\n- @creator-b：168,900再生、保存96',
    '投稿者ごとの再生数：\n1. creator-a：181,200\n2. creator-b：168,900',
    '上位の投稿：\n1. 181,200 再生・保存 127、creator-a\n2. 168,900 再生・保存 96、creator-b',
  ]) {
    const {remember, send} = setup();
    remember(evidence);
    const out = send(text);
    assert.doesNotMatch(out, /181,200|168,900/);
    assert.match(out, /📊/);
  }
});

test('iter3 exact metric lists and unrelated comparison tables stay untouched', () => {
  const {remember, send} = setup();
  remember(evidence);
  const text = '動画ごとの結果：\n- @creator-a：再生331,300、保存127\n- @creator-b：184,100再生、保存96';
  assert.equal(send(text), text);
  const comparison = '| 施策 | 本数 | 再生合計 |\n| --- | --- | --- |\n| 施策A | 6 | 1,230,000 |';
  assert.equal(send(comparison), comparison);
});

test('iter3 evidence never comes from URLs, failed tools, another run or stale session', () => {
  for (const [result, event, eventCtx] of [
    [{url: 'https://example.test/181200?plays=168900', saves: [127, 96]}, {}, undefined],
    [fabricated, {error: 'failed'}, undefined],
    [{isError: true, text: fabricated}, {}, undefined],
    [fabricated, {runId: 'other'}, undefined],
    [fabricated, {}, {runId: 'other'}],
  ]) {
    const {remember, send} = setup();
    remember(result, event, eventCtx);
    assert.doesNotMatch(send(fabricated), /181,200|168,900/);
  }
  const {hooks, ctx, remember, send} = setup();
  remember(fabricated);
  assert.equal(send(fabricated), fabricated);
  hooks.get('agent_end')({runId: ctx.runId}, ctx);
  ctx.runId = 'run-2';
  assert.doesNotMatch(send(fabricated), /181,200|168,900/);
});

test('iter3 absent or conflicting authoritative run does not alter metric replies', () => {
  const {send} = setup();
  assert.equal(send(fabricated, {}), fabricated);
  assert.equal(send(fabricated, {runId: 'other'}, 'run-1'), fabricated);
});

test('iter3 checks save-only tables and unsupported per-video ratios', () => {
  const {remember, send} = setup();
  remember(evidence);
  for (const text of [
    '| 投稿者 | 保存数 |\n| --- | --- |\n| creator-a | 1000 |',
    '| 動画 | 再生数 | 倍率 |\n| --- | --- | --- |\n| A | 331,300 | 5倍 |',
    '動画ごとの再生数は5倍近い差があります。',
    '動画ごとの保存率は3%でした。',
  ]) {
    assert.match(send(text), /📊/);
    assert.doesNotMatch(send(text), /1000|5倍|3%/);
  }
  remember({text: '動画ごとの再生数は5倍、保存率は3%'});
  assert.equal(send('動画ごとの再生数は5倍です。'), '動画ごとの再生数は5倍です。');
});

test('iter3 preserves creator names and captions that contain unrelated numbers', () => {
  const {remember, send} = setup();
  remember(evidence);
  const text = '動画ごとの結果：\n- 料理365：再生331,300、保存127、投稿文は30分時短\n- @creator2026：再生184,100、保存96';
  assert.equal(send(text), text);
});

test('iter3 guards plain creator-metric lines and table-only metric headings', () => {
  const {remember, send} = setup();
  remember(evidence);
  for (const text of [
    '投稿者ごとの結果：\ncreator-a：181,200再生、保存127\ncreator-b：168,900再生、保存96',
    '動画ごとの結果：\n| 再生数 | 保存数 |\n| --- | --- |\n| 181,200 | 127 |',
  ]) {
    assert.match(send(text), /📊/);
    assert.doesNotMatch(send(text), /181,200|168,900/);
  }
});

test('iter3 excludes nested error results and leaves code and campaign comparisons alone', () => {
  const {remember, send} = setup();
  remember({content: [{type: 'text', text: JSON.stringify({isError: true, text: fabricated})}]});
  assert.doesNotMatch(send(fabricated), /181,200|168,900/);
  const example = `例：\n\`\`\`\n${fabricated}\n\`\`\``;
  assert.equal(send(example), example);
  const campaign = '動画の集計結果：\n| 施策 | 本数 | 再生合計 |\n| --- | --- | --- |\n| 施策A | 6 | 1,230,000 |';
  assert.equal(send(campaign), campaign);
});


test('iter3 numeric evidence is isolated between turns of the same bound conversation and expires', () => {
  let nowMs = 1000;
  const {hooks, ctx, remember, send} = setup({now: () => nowMs});
  function receive(messageId) {
    const senderId = 'U12345678';
    hooks.get('message_received')({content: '動画ごとの結果', senderId, messageId, runId: ctx.runId,
      metadata: {guildId: 'T07MU5P2PBR', to: `user:${senderId}`}},
    {channelId: 'slack', sessionKey: 'agent:teamagent:slack:direct:u12345678',
      conversationId: `user:${senderId}`, senderId, messageId, runId: ctx.runId});
  }
  receive('123.456');
  remember({text: fabricated, url: 'https://example.test/source'});
  assert.equal(send(fabricated), fabricated);
  nowMs += 10 * 60 * 1000 + 1;
  assert.doesNotMatch(send(fabricated), /181,200|168,900/);
  remember(fabricated);
  hooks.get('agent_end')({runId: ctx.runId}, ctx);
  ctx.runId = 'run-2';
  receive('124.456');
  assert.doesNotMatch(send(fabricated), /181,200|168,900/);
});
