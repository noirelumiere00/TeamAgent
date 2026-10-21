import test from 'node:test';
import assert from 'node:assert/strict';
import {createCallerIdentityPlugin} from '../dist/index.js';

function setup() {
  const hooks = new Map();
  let nowMs = Date.now();
  createCallerIdentityPlugin({now: () => nowMs, env: {
    TEAMAGENT_CALLER_CLAIM_SECRET: 'x'.repeat(48), SLACK_TEAM_ID: 'T07MU5P2PBR',
  }}).register({registerInteractiveHandler() {}, logger: {info() {}, warn() {}},
    on: (name, fn) => hooks.set(name, fn)});
  function receive(content, {runId = 'run-1', messageId = '1784424000.000001',
    user = 'U12345678', session = `agent:teamagent:slack:direct:${user.toLowerCase()}`} = {}) {
    hooks.get('message_received')({content, senderId: user, messageId, runId,
      metadata: {guildId: 'T07MU5P2PBR', to: `user:${user}`}},
    {channelId: 'slack', sessionKey: session, conversationId: `user:${user}`,
      senderId: user, messageId, runId});
    return {runId, sessionKey: session, channelId: user, senderId: user};
  }
  function call(ctx, tool, id, params = {}) {
    const toolName = `teamagent__${tool}`;
    return hooks.get('before_tool_call')({toolName, toolCallId: id, runId: ctx.runId, params},
      {...ctx, toolName, toolCallId: id});
  }
  return {hooks, receive, call, advance: milliseconds => {nowMs += milliseconds;}};
}

for (const content of ['比較して何が効いた？', '動画ごとの結果を比較して、どちらが効いた？',
  '新しく動画を取得せず、手元の数字で比較して']) {
  test(`existing comparison blocks heavy analysis: ${content}`, () => {
    const {receive, call} = setup();
    const ctx = receive(content);
    for (const tool of ['video_algorithm', 'video_analysis', 'tiktok_acquire', 'search_surface_check']) {
      const out = call(ctx, tool, `call-${tool}`, {query: '伊藤ハム ハンバーグ'});
      assert.equal(out?.block, true);
      assert.match(out.blockReason, /search/);
    }
    assert.ok(call(ctx, 'search', 'call-search', {query: '伊藤ハム 施策 比較'})?.params);
  });
}

test('unsupported comparison still requires search after an unrelated tool succeeds', () => {
  const {receive, call, hooks} = setup();
  const ctx = receive('比較して何が効いた？');
  assert.ok(call(ctx, 'chitchat', 'call-chat', {})?.params);
  const out = hooks.get('before_agent_finalize')({runId: ctx.runId,
    lastAssistantMessage: '施策Aは平均171,325再生で、施策Bより高いです。'}, ctx);
  assert.equal(out?.action, 'revise');
  assert.match(out.retry.instruction, /search/);
});

for (const content of ['TikTokで競合の上位動画を新しく取得して比較して',
  '実映像を見て分析して、どちらが効いたか比較して',
  'この動画を分析して比較して https://www.tiktok.com/@sample/video/123456789']) {
  test(`explicit fresh analysis remains allowed: ${content}`, () => {
    const {receive, call} = setup();
    const ctx = receive(content);
    assert.ok(call(ctx, 'video_algorithm', 'call-video', {query: '競合'})?.params);
  });
}

test('same inbound cannot launch two jobs with different model queries, even across runs', () => {
  const {receive, call, hooks} = setup();
  const ctx = receive('TikTokで新宿ランチの上位動画を分析して');
  assert.ok(call(ctx, 'video_algorithm', 'call-first', {query: '新宿ランチ'})?.params);
  assert.equal(call(ctx, 'video_algorithm', 'call-second', {query: '新宿 グルメ'})?.block, true);
  hooks.get('agent_end')({runId: ctx.runId}, ctx);
  const next = receive('TikTokで新宿ランチの上位動画を分析して', {runId: 'run-2'});
  assert.equal(call(next, 'video_algorithm', 'call-third', {query: '新宿 おすすめ'})?.block, true);
});

test('explicit multiple keyword analyses each run once and reject model-added keywords', () => {
  const {receive, call} = setup();
  const ctx = receive('TikTokの上位動画を「新宿ランチ」「渋谷ランチ」の各キーワードでそれぞれ分析して比較して');
  assert.ok(call(ctx, 'video_algorithm', 'call-shinjuku', {query: '新宿ランチ'})?.params);
  assert.ok(call(ctx, 'video_algorithm', 'call-shibuya', {query: '渋谷ランチ'})?.params);
  assert.equal(call(ctx, 'video_algorithm', 'call-repeat', {query: '新宿ランチ'})?.block, true);
  assert.equal(call(ctx, 'video_algorithm', 'call-other', {query: '新宿 グルメ'})?.block, true);
});

test('quoted document and existing video campaign comparisons never authorize heavy analyses', () => {
  for (const content of ['「提案書」「カルテ」をそれぞれ比較して',
    '「ハンバーグ」「ステーキ」の動画結果をそれぞれ比較して']) {
    const {receive, call} = setup();
    const ctx = receive(content);
    assert.equal(call(ctx, 'video_algorithm', 'heavy', {query: '提案書'})?.block, true);
  }
});

test('all explicit user keyword analyses remain available beyond ten keywords', () => {
  const {receive, call} = setup();
  const keywords = Array.from({length: 12}, (_, i) => `検索語${i + 1}`);
  const ctx = receive(`TikTokの上位動画を${keywords.map(word => `「${word}」`).join('')}の各キーワードでそれぞれ分析して`);
  for (const [i, query] of keywords.entries()) {
    assert.ok(call(ctx, 'video_algorithm', `keyword-${i}`, {query})?.params, query);
  }
});

test('malformed multi-keyword query is blocked without throwing or consuming the request', () => {
  const {receive, call} = setup();
  const ctx = receive('TikTokの上位動画を「新宿ランチ」「渋谷ランチ」の各キーワードでそれぞれ分析して');
  for (const query of [{toString: 42}, ['新宿ランチ'], null, 42]) {
    let result;
    assert.doesNotThrow(() => {result = call(ctx, 'video_algorithm', 'bad-query', {query});});
    assert.equal(result?.block, true);
  }
  assert.ok(call(ctx, 'video_algorithm', 'valid-query', {query: '新宿ランチ'})?.params);
});

test('explicitly watching supplied videos authorizes analysis for their comparison', () => {
  const {receive, call} = setup();
  const ctx = receive('この2本の動画を見比べて、どちらが効いた？ https://www.tiktok.com/@sample/video/123456789 https://www.tiktok.com/@sample/video/123456790');
  assert.ok(call(ctx, 'video_analysis', 'watch-supplied', {query: '比較'})?.params);
});

test('supplied-video watching cannot finish without actually invoking analysis', () => {
  const {receive, hooks} = setup();
  const ctx = receive('この2本の動画を見比べて https://www.tiktok.com/@sample/video/123456789 https://www.tiktok.com/@sample/video/123456790');
  const out = hooks.get('before_agent_finalize')({runId: ctx.runId,
    lastAssistantMessage: 'どちらも完成度が高いです。'}, ctx);
  assert.equal(out?.action, 'revise');
  assert.match(out.retry.instruction, /video_analysis/);
});

for (const content of ['伊藤ハムのハンバーグとステーキの施策を見比べて',
  '伊藤ハムの施策を比べて',
  'この2本の動画の施策資料を見比べて https://docs.google.com/document/d/campaign-a/edit https://docs.google.com/document/d/campaign-b/edit']) {
  test(`unsupported campaign comparisons require search when actual videos were not supplied: ${content}`, () => {
    const {receive, call, hooks} = setup();
    const ctx = receive(content);
    assert.equal(call(ctx, 'video_analysis', 'heavy-analysis', {query: '比較'})?.block, true);
    assert.equal(hooks.get('before_agent_finalize')({runId: ctx.runId,
      lastAssistantMessage: '施策Aは平均171,325再生で、施策Bより高いです。'}, ctx)?.action, 'revise');
  });
}

test('a separate inbound and caller can still start a job, and invalid signing never consumes it', () => {
  const {receive, call} = setup();
  const ctx = receive('上位動画を分析して');
  assert.equal(call(ctx, 'video_algorithm', 'call-forged', {
    _user_context: {slack_user_id: 'U87654321'}, query: '新宿'})?.block, true);
  assert.ok(call(ctx, 'video_algorithm', 'call-valid', {query: '新宿'})?.params);
  const other = receive('上位動画を分析して', {runId: 'other', user: 'U87654321'});
  assert.ok(call(other, 'video_algorithm', 'call-other', {query: '新宿'})?.params);
  const next = receive('もう一度分析して', {runId: 'next', messageId: '1784424001.000001'});
  assert.ok(call(next, 'video_algorithm', 'call-new-message', {query: '新宿'})?.params);
});

test('duplicate guard survives a twenty-one-minute detached job and eventually expires', () => {
  const {receive, call, hooks, advance} = setup();
  let ctx = receive('上位動画を分析して');
  assert.ok(call(ctx, 'video_algorithm', 'call-first', {query: '新宿'})?.params);
  hooks.get('agent_end')({runId: ctx.runId}, ctx);
  advance(21 * 60 * 1000);
  ctx = receive('上位動画を分析して', {runId: 'delayed'});
  assert.equal(call(ctx, 'video_algorithm', 'call-delayed', {query: '新宿 別表現'})?.block, true);
  hooks.get('agent_end')({runId: ctx.runId}, ctx);
  advance(25 * 60 * 1000);
  ctx = receive('上位動画を分析して', {runId: 'expired'});
  assert.ok(call(ctx, 'video_algorithm', 'call-expired', {query: '新宿'})?.params);
});
