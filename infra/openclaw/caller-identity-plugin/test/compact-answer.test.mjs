import test from 'node:test';
import assert from 'node:assert/strict';
import {createCallerIdentityPlugin} from '../dist/index.js';

function setup() {
  const hooks = new Map();
  const ctx = {runId: 'short-run', sessionKey: 'agent:teamagent:slack:direct:u12345678',
    senderId: 'U12345678', channelId: 'U12345678'};
  createCallerIdentityPlugin({env: {
    TEAMAGENT_CALLER_CLAIM_SECRET: 'x'.repeat(48), SLACK_TEAM_ID: 'T07MU5P2PBR',
    SEARCH_DIRECT_ANSWER: '1', SEARCH_DIRECT_ANSWER_ALLOWED_EMAILS: '*',
    SEARCH_DIRECT_ANSWER_KINDS: 'not_found',
  }}).register({registerInteractiveHandler() {}, logger: {info() {}, warn() {}},
    on: (name, fn) => hooks.set(name, fn)});
  hooks.get('message_received')({content: '施策は今どうなっている？', senderId: ctx.senderId,
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
