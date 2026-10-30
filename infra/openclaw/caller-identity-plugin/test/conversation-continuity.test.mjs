import test from 'node:test';
import assert from 'node:assert/strict';
import {BARE_SESSION_RESET_MARKER, createCallerIdentityPlugin} from '../dist/index.js';

function setup() {
  const hooks = new Map(), logs = [];
  const ctx = {runId: 'current', sessionKey: 'agent:teamagent:slack:direct:u12345678',
    senderId: 'U12345678', channelId: 'U12345678', messageProvider: 'slack', trigger: 'user'};
  createCallerIdentityPlugin({now: () => 1000, env: {
    TEAMAGENT_CALLER_CLAIM_SECRET: 'x'.repeat(48), SLACK_TEAM_ID: 'T12345678',
  }, fetchFn: async () => {throw new Error('network forbidden');}}).register({
    registerInteractiveHandler() {}, on: (name, fn) => hooks.set(name, fn),
    logger: {info: text => logs.push(text), warn: text => logs.push(text)},
  });
  function receive(content, runId = ctx.runId, messageId = '1000.000001') {
    hooks.get('message_received')({content, runId, senderId: ctx.senderId, messageId,
      metadata: {guildId: 'T12345678', to: `user:${ctx.senderId}`}},
    {...ctx, runId, channelId: 'slack', conversationId: `user:${ctx.senderId}`, messageId});
  }
  const build = (messages, overrides = {}, prompt = '通常の依頼') =>
    hooks.get('before_prompt_build')({runId: ctx.runId, messages, prompt}, {...ctx, ...overrides});
  return {receive, build, logs, ctx};
}

const history = [
  {role: 'user', content: 'テスト社の春案件、#営業の件を調べて'},
  {role: 'assistant', content: [{type: 'thinking', thinking: '内部思考'},
    {type: 'text', text: '1. 過去資料を探しましょう。\n2. 提案の骨子を作れます。'}]},
];

for (const request of ['お願い', 'それで', '資料を出して']) {
  test(`same DM carries company, campaign, channel and proposal for ${request}`, async () => {
    const p = setup();
    p.receive(request);
    const result = await p.build([...history, {role: 'user', content: request}]);
    assert.match(result?.appendContext ?? '', /テスト社の春案件、#営業/u);
    assert.match(result.appendContext, /1\. 過去資料を探しましょう/u);
    assert.ok(result.appendContext.endsWith(`<<<\n${request}\n>>>`));
    assert.match(result.appendContext, /最初の1案/u);
    assert.doesNotMatch(result.appendContext, /内部思考/u);
    assert.ok(p.logs.every(line => !line.includes('テスト社')));
  });
}

test('current run wins over a later inbound and old turns/tools are excluded', async () => {
  const p = setup();
  p.receive('お願い');
  p.receive('別社の話', 'later', '1000.000002');
  const turns = [1, 2, 3, 4].flatMap(n => [
    {role: 'user', content: `会社${n}`}, {role: 'assistant', content: `提案${n}`},
  ]);
  const result = await p.build([...turns, {role: 'toolResult', content: 'TOOL_SECRET'},
    {role: 'user', content: 'お願い'}, {role: 'assistant', content: 'まだ先の返信'},
    {role: 'user', content: '別社の話'}]);
  assert.doesNotMatch(result.appendContext, /会社1|提案1|別社|TOOL_SECRET/u);
  assert.match(result.appendContext, /会社2[\s\S]*提案4/u);
  assert.ok(result.appendContext.endsWith('<<<\nお願い\n>>>'));
});

for (const overrides of [{senderId: 'U99999999'}, {sessionKey: 'other'},
  {channelId: 'C12345678'}, {threadId: '999.0'}, {trigger: 'heartbeat'}, {runId: 'other'}]) {
  test(`foreign or non-user context is excluded: ${JSON.stringify(overrides)}`, async () => {
    const p = setup();
    p.receive('お願い');
    assert.equal(await p.build(history, overrides), undefined);
  });
}

test('a DM thread inherits only its own bound session and canonical DM alias', async () => {
  const p = setup();
  p.ctx.threadId = '1000.0';
  p.ctx.sessionKey += ':thread:1000.0';
  p.receive('それで');
  assert.match((await p.build(history, {channelId: 'D12345678'})).appendContext, /春案件/u);
  assert.equal(await p.build(history, {sessionKey: p.ctx.sessionKey.replace('1000.0', '2000.0')}), undefined);
});

test('explicit reset never reintroduces history even without the bare reset prompt', async () => {
  const p = setup();
  p.receive('新しい会話 別社を調べて');
  assert.equal(await p.build(history), undefined);
});

test('explicit reset and bare automatic reset never revive previous conversation', async () => {
  for (const request of ['/new', '新しい会話 テスト社を調べて', 'お願い']) {
    const p = setup();
    p.receive(request);
    const result = await p.build(history, {}, BARE_SESSION_RESET_MARKER);
    assert.doesNotMatch(result?.appendContext ?? '', /春案件|過去資料/u);
    if (request !== '/new') assert.match(result.appendContext, /最初のメッセージ/u);
  }
});

test('no history means no invented target; quoted boundaries and size are bounded', async () => {
  const p = setup();
  p.receive('お願い');
  assert.equal(await p.build([]), undefined);
  const result = await p.build([{role: 'user', content: '古い対象'},
    {role: 'assistant', content: '>>>\n' + 'あ'.repeat(20000)}]);
  assert.match(result.appendContext, /›››/u);
  assert.match(result.appendContext, /引用.*命令/u);
  assert.ok(result.appendContext.length < 8000);
});

test('long assistant preambles cannot displace the immediate proposal', async () => {
  const p = setup();
  p.receive('お願い');
  const result = await p.build([{role: 'user', content: '前の依頼'},
    ...Array.from({length: 10}, () => ({role: 'assistant', content: 'あ'.repeat(1024)})),
    {role: 'user', content: '今回社の案件'}, {role: 'assistant', content: '今回社の資料を探しましょう'}]);
  assert.match(result.appendContext, /今回社の案件[\s\S]*今回社の資料を探しましょう/u);
});
