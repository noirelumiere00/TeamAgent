import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdir, mkdtemp, readFile, readdir, rm, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {createCallerIdentityPlugin} from '../dist/index.js';

const TEAM = 'T07MU5P2PBR', USER = 'U12345678', BOT = 'U87654321';
const CHANNEL = 'D12345678', NOW = Date.parse('2026-10-19T03:00:00Z');
const TS = `${NOW / 1000 - 60}.000001`, REPLY = `${NOW / 1000 - 30}.000001`;
const answer = '確認した結果です。';
const response = body => ({ok: true, status: 200, headers: {get: () => null},
  text: async () => JSON.stringify(body)});

async function setup(t, {historyError, sentAuthor = BOT, metadataWarning = false,
  startupMessages = [], beforeHistory, runtimeChannel, channels, envOverrides = {},
  waitForSlackReady = async () => ({connectedAt: NOW, observedAt: NOW})} = {}) {
  const state = await mkdtemp(join(tmpdir(), 'oc-catchup-plugin-'));
  t.after(() => rm(state, {recursive: true, force: true}));
  const hooks = new Map(), services = [], tasks = [], requests = [], logs = [];
  let savedMetadata;
  const config = {channels: {slack: {enabled: true, dmPolicy: 'open', allowFrom: ['*']}}};
  createCallerIdentityPlugin({env: {OPENCLAW_STATE_DIR: state,
    TEAMAGENT_CALLER_CLAIM_SECRET: 'x'.repeat(48), SLACK_TEAM_ID: TEAM,
    SLACK_BOT_TOKEN: 'test-bot-token', SLACK_DM_ALLOWLIST: '*',
    DM_CATCHUP_ENABLED: '1', DM_CATCHUP_ALLOWED_USERS: USER, ...envOverrides}, now: () => NOW,
  waitForSlackReady,
  onBackgroundTask: task => tasks.push(task), fetchFn: async (url, options) => {
    const target = new URL(url), method = target.pathname.split('/').at(-1);
    const body = options.body ? JSON.parse(options.body) : Object.fromEntries(target.searchParams);
    requests.push({method, body});
    if (method === 'auth.test') return response({ok: true, team_id: TEAM, user_id: BOT});
    if (method === 'conversations.list') return response({ok: true, channels: channels ?? [{id: CHANNEL, user: USER, is_im: true}]});
    if (method === 'conversations.history') {
      if (historyError) return response({ok: false, error: historyError});
      if (body.oldest !== REPLY) await beforeHistory?.();
      return response({ok: true, messages: body.oldest === REPLY
        ? [{user: sentAuthor, ts: REPLY, text: answer, metadata: savedMetadata}] : startupMessages});
    }
    if (method === 'conversations.open') return response({ok: true, channel: {id: CHANNEL}});
    if (method === 'chat.update') {
      if (metadataWarning) return response({ok: true, ts: REPLY, warning: 'metadata_ignored'});
      savedMetadata = body.metadata;
      return response({ok: true, ts: REPLY});
    }
    if (method === 'chat.postMessage') return response({ok: true, ts: REPLY});
    throw new Error(`unexpected method ${method}`);
  }}).register({config, runtime: {channel: runtimeChannel ?? {reply: {
    dispatchReplyWithBufferedBlockDispatcher: async () => ({queuedFinal: false}),
  }}}, registerService: service => services.push(service),
  registerInteractiveHandler() {}, on: (name, fn) => hooks.set(name, fn),
  logger: {info: line => logs.push(line), warn: line => logs.push(line)}});
  const event = {from: `slack:${USER}`, content: '質問です', senderId: USER,
    messageId: TS, runId: 'catchup-run',
    metadata: {guildId: TEAM, to: `user:${USER}`, originatingTo: `user:${USER}`}};
  const ctx = {channelId: 'slack', conversationId: `user:${USER}`,
    sessionKey: 'agent:teamagent:slack:direct:u12345678', senderId: USER,
    messageId: TS, runId: 'catchup-run'};
  async function start() {
    assert.equal(services.length, 1, 'startup recovery service must be registered');
    await services[0].start({config, stateDir: state});
    await Promise.all(tasks);
  }
  return {hooks, event, ctx, requests, logs, start, tasks, state, config, services};
}

test('plugin startup registers and starts bounded DM recovery', async t => {
  const p = await setup(t); await p.start();
  assert.ok(p.requests.some(x => x.method === 'conversations.list' && x.body.types === 'im'));
});

test('catchup rollout reads history only for explicitly allowed Slack users', async t => {
  const other = 'U99999999', otherChannel = 'D99999999';
  const p = await setup(t, {channels: [
    {id: otherChannel, user: other, is_im: true}, {id: CHANNEL, user: USER, is_im: true},
  ]});
  await p.start();
  assert.deepEqual(p.requests.filter(x => x.method === 'conversations.history')
    .map(x => x.body.channel), [CHANNEL]);
});

test('catchup wildcard is explicit and includes both allowed native DM users', async t => {
  const p = await setup(t, {envOverrides: {DM_CATCHUP_ALLOWED_USERS: '*'}, channels: [
    {id: 'D99999999', user: 'U99999999', is_im: true}, {id: CHANNEL, user: USER, is_im: true},
  ]});
  await p.start();
  assert.deepEqual(p.requests.filter(x => x.method === 'conversations.history')
    .map(x => x.body.channel), ['D99999999', CHANNEL]);
});

test('catchup enabled with empty or malformed rollout users has no service or readiness work', async t => {
  for (const users of [undefined, '', ' ', 'U12345678,', 'U12345678, U99999999',
    'U12345678,*', 'U12345678,U12345678', 'not-a-slack-id']) {
    let readiness = 0;
    const p = await setup(t, {envOverrides: {DM_CATCHUP_ALLOWED_USERS: users},
      waitForSlackReady: async () => {readiness++;}});
    assert.equal(p.services.length, 0, `users=${String(users)}`);
    assert.equal(p.hooks.has('reply_dispatch'), false);
    assert.equal(p.hooks.has('message_sent'), false);
    await Promise.all(p.tasks);
    assert.equal(readiness, 0);
    assert.deepEqual(p.requests, []);
    assert.deepEqual(await readdir(p.state), []);
  }
});

test('a user outside catchup rollout retains synchronous ordinary live admission and reply bytes', async t => {
  const p = await setup(t, {envOverrides: {DM_CATCHUP_ALLOWED_USERS: 'U99999999'}});
  await p.start();
  for (let attempt = 0; attempt < 2; attempt++) {
    assert.equal(p.hooks.get('inbound_claim')(p.event, p.ctx), undefined);
    assert.equal(p.hooks.get('message_received')(p.event, p.ctx), undefined);
  }
  assert.equal(await p.hooks.get('reply_dispatch')(dispatchEvent(p), {}), undefined);
  p.hooks.get('before_model_resolve')({}, {...p.ctx, requesterSenderId: USER,
    requesterChannel: 'slack', channelId: USER, chatId: CHANNEL});
  const payload = {text: answer};
  const result = p.hooks.get('reply_payload_sending')({runId: p.ctx.runId, kind: 'final', payload}, p.ctx);
  assert.equal(typeof result?.then, 'undefined');
  assert.equal(JSON.stringify(result?.payload ?? payload), JSON.stringify(payload));
  assert.equal(p.requests.some(x => x.method === 'conversations.history'), false);
  await assert.rejects(readFile(join(p.state, 'slack-catchup', 'journal.json')), {code: 'ENOENT'});
});

test('unavailable Socket readiness leaves recovery unresolved and live DM admission available', async t => {
  const p = await setup(t, {waitForSlackReady: async () => {
    throw new Error('private-token-and-question-must-not-be-logged');
  }});
  await p.start();
  assert.equal(p.requests.some(x => x.method === 'conversations.list'), false);
  const journal = JSON.parse(await readFile(join(p.state, 'slack-catchup', 'journal.json'), 'utf8'));
  assert.ok(journal.unresolved.length > 0);
  assert.equal(p.logs.some(line => line.includes('private-token-and-question-must-not-be-logged')), false);
  assert.equal(await p.hooks.get('inbound_claim')(p.event, p.ctx), undefined);
});

test('live DM shares the durable admission gate before plugin work starts', async t => {
  const p = await setup(t); await p.start();
  assert.equal(await p.hooks.get('inbound_claim')(p.event, p.ctx), undefined);
  assert.deepEqual(await p.hooks.get('inbound_claim')(p.event, p.ctx), {handled: true});
});

test('final delivery is correlated to its input ts without replacing Slack formatting', async t => {
  const p = await setup(t); await p.start();
  await p.hooks.get('message_received')(p.event, p.ctx);
  await p.hooks.get('reply_dispatch')({...dispatchEvent(p), runId: p.ctx.runId}, {});
  await p.hooks.get('reply_dispatch')(dispatchEvent(p), {});
  p.hooks.get('before_model_resolve')({}, {...p.ctx, requesterSenderId: USER,
    requesterChannel: 'slack', channelId: USER, chatId: CHANNEL});
  await p.hooks.get('reply_payload_sending')({runId: p.ctx.runId,
    kind: 'final', payload: {text: answer}}, p.ctx);
  assert.equal(typeof p.hooks.get('message_sent'), 'function');
  await p.hooks.get('message_sent')({to: `user:${USER}`, content: answer,
    success: true, messageId: REPLY}, p.ctx);
  const updates = p.requests.filter(x => x.method === 'chat.update');
  assert.equal(updates.length, 1);
  assert.deepEqual(updates[0].body, {channel: CHANNEL, ts: REPLY,
    metadata: {event_type: 'teamagent.dm_answer_v1', event_payload: {
      source_channel: CHANNEL, source_ts: TS, complete: true,
    }}});
  assert.equal(p.requests.filter(x => x.method === 'chat.postMessage').length, 0);
});

test('missing history scope leaves startup and normal live DM processing available', async t => {
  const p = await setup(t, {historyError: 'missing_scope'}); await p.start();
  assert.equal(await p.hooks.get('inbound_claim')(p.event, p.ctx), undefined);
  assert.ok(p.logs.some(line => line.includes('missing_scope')));
});

for (const scenario of ['foreign author', 'metadata ignored']) {
  test(`live reply metadata leaves a durable unresolved record on ${scenario}`, async t => {
    const p = await setup(t, scenario === 'foreign author'
      ? {sentAuthor: USER} : {metadataWarning: true});
    await p.start();
    await p.hooks.get('message_received')(p.event, p.ctx);
    await p.hooks.get('reply_dispatch')({...dispatchEvent(p), runId: p.ctx.runId}, {});
    await p.hooks.get('reply_dispatch')(dispatchEvent(p), {});
    p.hooks.get('before_model_resolve')({}, {...p.ctx, requesterSenderId: USER,
      requesterChannel: 'slack', channelId: USER, chatId: CHANNEL});
    await p.hooks.get('reply_payload_sending')({runId: p.ctx.runId,
      kind: 'final', payload: {text: answer}}, p.ctx);
    await p.hooks.get('message_sent')({to: `user:${USER}`, content: answer,
      success: true, messageId: REPLY}, p.ctx);
    const journal = JSON.parse(await readFile(join(p.state, 'slack-catchup', 'journal.json'), 'utf8'));
    assert.ok(journal.unresolved.some(row => row.reason === 'metadata_update_failed'));
    assert.ok(journal.entries.every(row => row.state !== 'done'));
    if (scenario === 'foreign author') assert.equal(p.requests.filter(x => x.method === 'chat.update').length, 0);
  });
}

const dispatchEvent = (p, changes = {}) => ({ctx: {
  Provider: 'slack', Surface: 'slack', OriginatingChannel: 'slack', ChatType: 'direct',
  NativeChannelId: CHANNEL, MessageSid: TS, SenderId: USER, GroupSpace: TEAM,
  SessionKey: p.ctx.sessionKey, From: `slack:${USER}`, To: `user:${USER}`,
  OriginatingTo: `user:${USER}`, BodyForCommands: '質問です', ...changes,
}});
const blockedDispatch = {handled: true, queuedFinal: false, counts: {tool: 0, block: 0, final: 0}};

test('normal live DM is admitted once at the awaited reply dispatch hook without inbound_claim', async t => {
  const p = await setup(t); await p.start();
  const dispatch = p.hooks.get('reply_dispatch');
  assert.equal(typeof dispatch, 'function');
  const received = p.hooks.get('message_received')(p.event, p.ctx);
  assert.equal(await dispatch(dispatchEvent(p), {}), undefined);
  await received;
  await p.hooks.get('message_received')(p.event, p.ctx);
  assert.deepEqual(await dispatch(dispatchEvent(p), {}), blockedDispatch);
});

test('two questions in one DM do not let a prior source timestamp through again', async t => {
  const p = await setup(t); await p.start();
  const dispatch = p.hooks.get('reply_dispatch');
  assert.equal(typeof dispatch, 'function');
  for (const ts of [TS, `${NOW / 1000 - 50}.000001`]) {
    await p.hooks.get('message_received')({...p.event, messageId: ts, runId: undefined},
      {...p.ctx, messageId: ts, runId: undefined});
    assert.equal(await dispatch(dispatchEvent(p, {MessageSid: ts}), {}), undefined);
  }
  await p.hooks.get('message_received')(p.event, p.ctx);
  assert.deepEqual(await dispatch(dispatchEvent(p), {}), blockedDispatch);
});

test('journal failure leaves a valid live DM available to normal dispatch', async t => {
  const p = await setup(t);
  await writeFile(join(p.state, 'slack-catchup'), 'not a directory');
  await p.start();
  const dispatch = p.hooks.get('reply_dispatch');
  assert.equal(typeof dispatch, 'function');
  assert.equal(await dispatch(dispatchEvent(p), {}), undefined);
  assert.ok(p.logs.some(line => line.includes('live_tracking_unavailable')));
});

test('the exact dispatch gate rejects foreign or malformed Slack DM identities', async t => {
  const p = await setup(t); await p.start();
  const dispatch = p.hooks.get('reply_dispatch');
  assert.equal(typeof dispatch, 'function');
  for (const changes of [{GroupSpace: 'T99999999'}, {SenderId: 'invalid'},
    {MessageSid: 'invalid'}, {NativeChannelId: undefined}]) {
    assert.deepEqual(await dispatch(dispatchEvent(p, changes), {}), blockedDispatch);
  }
});

test('unauthorized live DM cannot use journal failure to bypass the dispatch gate', async t => {
  const p = await setup(t);
  p.config.channels.slack.dmPolicy = 'allowlist';
  p.config.channels.slack.allowFrom = ['U99999999'];
  await writeFile(join(p.state, 'slack-catchup'), 'not a directory');
  await p.start();
  assert.deepEqual(await p.hooks.get('reply_dispatch')(dispatchEvent(p), {}), blockedDispatch);
  assert.ok(p.logs.every(line => !line.includes('live_tracking_unavailable')));
});

test('non-Slack and non-DM dispatch remain outside the catchup gate', async t => {
  const p = await setup(t); await p.start();
  const dispatch = p.hooks.get('reply_dispatch');
  for (const ctx of [{Provider: 'telegram', ChatType: 'direct'},
    {Provider: 'slack', ChatType: 'group', NativeChannelId: 'C12345678'}]) {
    assert.equal(await dispatch({ctx}, {}), undefined);
  }
});

test('native Slack commands with non-message IDs remain outside the DM catchup gate', async t => {
  const p = await setup(t); await p.start();
  const dispatch = p.hooks.get('reply_dispatch');
  assert.equal(await dispatch(dispatchEvent(p, {CommandSource: 'native',
    MessageSid: 'slash-command-request'}), {}), undefined);
  assert.deepEqual(await dispatch(dispatchEvent(p, {MessageSid: 'slash-command-request'}), {}),
    blockedDispatch);
});

test('startup replay can claim a dispatch after an earlier live admission was denied', async t => {
  let p, dispatched = 0;
  const runtimeChannel = {
    routing: {resolveAgentRoute: () => ({agentId: 'teamagent', accountId: 'default',
      sessionKey: p.ctx.sessionKey})},
    inbound: {buildContext: args => ({...dispatchEvent(p).ctx, MessageSid: args.messageId})},
    session: {resolveStorePath: () => '/test/sessions.json', recordInboundSession: async () => {}},
    reply: {dispatchReplyWithBufferedBlockDispatcher: async args => {
      const result = await p.hooks.get('reply_dispatch')({ctx: args.ctx, runId: args.replyOptions.runId}, {});
      if (result?.handled) return result;
      const runId = args.replyOptions.runId, toolCallId = 'startup-replay-tool', toolName = 'teamagent__search';
      const agentCtx = {runId, sessionKey: p.ctx.sessionKey, senderId: USER,
        messageProvider: 'slack', trigger: 'user', channel: 'slack', channelId: USER, chatId: USER};
      p.hooks.get('before_model_resolve')({}, agentCtx);
      const signed = p.hooks.get('before_tool_call')({runId, toolCallId, toolName,
        params: {query: '元の質問', _user_context: {}}}, {...agentCtx, toolCallId, toolName});
      assert.notEqual(signed?.block, true, signed?.blockReason);
      const claim = JSON.parse(Buffer.from(signed.params._user_context.caller_claim.split('.')[0], 'base64url'));
      assert.equal(claim.message, TS);
      assert.equal(claim.channel, CHANNEL);
      assert.equal(claim.run_id, runId);
      dispatched++;
      await args.dispatcherOptions.deliver({text: answer}, {kind: 'final'});
      return {queuedFinal: true, counts: {tool: 0, block: 0, final: 1}};
    }},
  };
  p = await setup(t, {runtimeChannel,
    startupMessages: [{type: 'message', user: USER, ts: TS, text: '質問です'}],
    beforeHistory: async () => {
      await p.hooks.get('message_received')(p.event, p.ctx);
      assert.deepEqual(await p.hooks.get('reply_dispatch')(dispatchEvent(p), {}), blockedDispatch);
    }});
  const directory = join(p.state, 'slack-catchup');
  await mkdir(directory);
  await writeFile(join(directory, 'journal.json'), JSON.stringify({v: 1, team: TEAM,
    entries: [{channel: CHANNEL, ts: TS, state: 'done', replyTs: REPLY}]}));
  await p.start();
  assert.equal(dispatched, 1, 'the history-owned replay must not reuse the stale live denial');
  assert.deepEqual(await p.hooks.get('reply_dispatch')(dispatchEvent(p), {}), blockedDispatch);
});

test('concurrent dispatch hooks allow exactly one owner for the same DM source', async t => {
  const p = await setup(t); await p.start();
  const dispatch = p.hooks.get('reply_dispatch');
  const results = await Promise.all([dispatch(dispatchEvent(p), {}), dispatch(dispatchEvent(p), {})]);
  assert.equal(results.filter(value => value === undefined).length, 1);
  assert.deepEqual(results.find(value => value !== undefined), blockedDispatch);
});

for (const scenario of ['no message_received', 'newer question pending', 'late message_received']) {
  test(`dispatch synchronously binds the exact source claim with ${scenario}`, async t => {
    const p = await setup(t); await p.start();
    const runId = 'catchup-exact-run';
    if (scenario === 'newer question pending') {
      const newer = `${NOW / 1000 - 10}.000001`;
      await p.hooks.get('message_received')({...p.event, messageId: newer, runId: undefined},
        {...p.ctx, messageId: newer, runId: undefined});
    }
    assert.equal(await p.hooks.get('reply_dispatch')({...dispatchEvent(p), runId}, {}), undefined);
    if (scenario === 'late message_received') {
      await p.hooks.get('message_received')({...p.event, runId}, {...p.ctx, runId});
    }
    const agentCtx = {runId, sessionKey: p.ctx.sessionKey, senderId: USER,
      messageProvider: 'slack', trigger: 'user', channel: 'slack', channelId: USER, chatId: USER};
    p.hooks.get('before_model_resolve')({}, agentCtx);
    const toolName = 'teamagent__search', toolCallId = 'catchup-exact-tool';
    const signed = p.hooks.get('before_tool_call')({runId, toolCallId, toolName,
      params: {query: '元の質問', _user_context: {}}}, {...agentCtx, toolCallId, toolName});
    assert.notEqual(signed?.block, true, signed?.blockReason);
    const context = signed.params._user_context;
    const claim = JSON.parse(Buffer.from(context.caller_claim.split('.')[0], 'base64url').toString());
    assert.equal(claim.message, TS);
    assert.equal(claim.channel, CHANNEL);
    assert.equal(context.channel_id, CHANNEL);
    assert.equal(claim.sub, USER);
    assert.equal(claim.run_id, runId);
  });
}

test('dispatch starts the existing connect guarantee once before any generic receive notification', async t => {
  const p = await setup(t); await p.start();
  const runId = 'catchup-connect-run';
  await p.hooks.get('reply_dispatch')({...dispatchEvent(p, {BodyForCommands: '連携'}), runId}, {});
  await Promise.all(p.tasks);
  assert.equal(p.requests.filter(request => request.method === 'chat.postMessage').length, 1);
  await p.hooks.get('message_received')({...p.event, content: '連携', runId}, {...p.ctx, runId});
  await Promise.all(p.tasks);
  assert.equal(p.requests.filter(request => request.method === 'chat.postMessage').length, 1);
});

test('ordinary live DM binds the model-generated run and completes metadata without an early run ID', async t => {
  const p = await setup(t); await p.start();
  await p.hooks.get('message_received')({...p.event, runId: undefined}, {...p.ctx, runId: undefined});
  assert.equal(await p.hooks.get('reply_dispatch')(dispatchEvent(p), {}), undefined);
  const runId = 'model-generated-live-run', toolCallId = 'ordinary-live-tool', toolName = 'teamagent__search';
  const agentCtx = {runId, sessionKey: p.ctx.sessionKey, senderId: USER,
    messageProvider: 'slack', trigger: 'user', channel: 'slack', channelId: USER, chatId: USER};
  p.hooks.get('before_model_resolve')({}, agentCtx);
  const signed = p.hooks.get('before_tool_call')({runId, toolCallId, toolName,
    params: {query: '元の質問', _user_context: {}}}, {...agentCtx, toolCallId, toolName});
  assert.notEqual(signed?.block, true, signed?.blockReason);
  const claim = JSON.parse(Buffer.from(signed.params._user_context.caller_claim.split('.')[0], 'base64url'));
  assert.equal(claim.message, TS);
  assert.equal(claim.channel, CHANNEL);
  assert.equal(claim.run_id, runId);
  p.hooks.get('agent_end')({runId, success: true}, agentCtx);
  await p.hooks.get('reply_payload_sending')({runId, kind: 'final', payload: {text: answer}}, agentCtx);
  await p.hooks.get('message_sent')({to: `user:${USER}`, content: answer,
    success: true, messageId: REPLY}, {channelId: 'slack', conversationId: `user:${USER}`});
  const updates = p.requests.filter(request => request.method === 'chat.update');
  assert.equal(updates.length, 1);
  assert.equal(updates[0].body.metadata.event_payload.source_ts, TS);
  const journal = JSON.parse(await readFile(join(p.state, 'slack-catchup', 'journal.json'), 'utf8'));
  assert.ok(journal.entries.some(row => row.ts === TS && row.state === 'done' && row.replyTs === REPLY));

  const nextTs = `${NOW / 1000 - 10}.000001`;
  await p.hooks.get('message_received')({...p.event, messageId: nextTs, runId: undefined},
    {...p.ctx, messageId: nextTs, runId: undefined});
  await p.hooks.get('reply_dispatch')(dispatchEvent(p, {MessageSid: nextTs}), {});
  await p.hooks.get('message_received')({...p.event, runId: undefined}, {...p.ctx, runId: undefined});
  const nextCtx = {...agentCtx, runId: 'next-model-generated-run', toolCallId: 'next-live-tool', toolName};
  p.hooks.get('before_model_resolve')({}, nextCtx);
  const nextSigned = p.hooks.get('before_tool_call')({runId: nextCtx.runId,
    toolCallId: nextCtx.toolCallId, toolName, params: {query: '次の質問', _user_context: {}}}, nextCtx);
  assert.notEqual(nextSigned?.block, true, nextSigned?.blockReason);
  const nextClaim = JSON.parse(Buffer.from(nextSigned.params._user_context.caller_claim.split('.')[0], 'base64url'));
  assert.equal(nextClaim.message, nextTs, 'a rejected old retry must not become the new run source');
  assert.equal(nextClaim.channel, CHANNEL);
});

test('a connect link alone cannot complete a compound question', async t => {
  const p = await setup(t); await p.start();
  const event = {...p.event, content: '連携\n今日の予定を教えて'};
  await p.hooks.get('message_received')(event, p.ctx);
  await Promise.all(p.tasks);
  const posts = p.requests.filter(x => x.method === 'chat.postMessage');
  assert.equal(posts.length, 1);
  assert.equal(posts[0].body.metadata.event_payload.complete, false);
  const journal = JSON.parse(await readFile(join(p.state, 'slack-catchup', 'journal.json'), 'utf8'));
  assert.ok(journal.entries.every(row => row.state !== 'done'));
});

test('overlapping questions cannot mark the newest input as answered by an older run', async t => {
  const p = await setup(t); await p.start();
  await p.hooks.get('message_received')(p.event, p.ctx);
  await p.hooks.get('reply_dispatch')({...dispatchEvent(p), runId: p.ctx.runId}, {});
  const secondTs = `${NOW / 1000 - 50}.000001`;
  const second = {...p.event, messageId: secondTs, runId: undefined};
  const secondCtx = {...p.ctx, messageId: secondTs, runId: undefined};
  await p.hooks.get('message_received')(second, secondCtx);
  await p.hooks.get('reply_dispatch')(dispatchEvent(p, {MessageSid: secondTs}), {});
  const agentCtx = {...p.ctx, requesterSenderId: USER, requesterChannel: 'slack',
    channelId: USER, chatId: CHANNEL};
  p.hooks.get('before_model_resolve')({}, agentCtx);
  await p.hooks.get('reply_payload_sending')({runId: p.ctx.runId,
    kind: 'final', payload: {text: answer}}, p.ctx);
  await p.hooks.get('message_sent')({to: `user:${USER}`, content: answer,
    success: true, messageId: REPLY}, p.ctx);
  assert.equal(p.requests.filter(x => x.method === 'chat.update').length, 0);
  const journal = JSON.parse(await readFile(join(p.state, 'slack-catchup', 'journal.json'), 'utf8'));
  assert.ok(journal.entries.every(row => row.state !== 'done'));
  assert.ok(journal.unresolved.some(row => row.reason === 'metadata_update_failed'));
});

for (const scenario of ['no session in sent hook', 'trimmed sent content']) {
  test(`native metadata correlates exact recipient and source with ${scenario}`, async t => {
    const p = await setup(t); await p.start();
    await p.hooks.get('message_received')(p.event, p.ctx);
    await p.hooks.get('reply_dispatch')({...dispatchEvent(p), runId: p.ctx.runId}, {});
    p.hooks.get('before_model_resolve')({}, {...p.ctx, requesterSenderId: USER,
      requesterChannel: 'slack', channelId: USER, chatId: CHANNEL});
    await p.hooks.get('reply_payload_sending')({runId: p.ctx.runId,
      kind: 'final', payload: {text: scenario === 'trimmed sent content' ? ` ${answer}\n` : answer}}, p.ctx);
    await p.hooks.get('message_sent')({to: `user:${USER}`, content: answer,
      success: true, messageId: REPLY}, scenario === 'no session in sent hook'
      ? {channelId: 'slack', conversationId: `user:${USER}`} : p.ctx);
    assert.equal(p.requests.filter(x => x.method === 'chat.update').length, 1);
  });
}

const legacyOffHooks = ['inbound_claim', 'message_received', 'before_agent_reply',
  'before_model_resolve', 'before_prompt_build', 'before_tool_call', 'after_tool_call',
  'before_agent_finalize', 'reply_payload_sending', 'agent_end'];
const legacyOffBanner = 'teamagent-caller-identity: registered hooks=[inbound_claim,message_received,before_agent_reply,before_model_resolve,before_prompt_build,before_tool_call,after_tool_call,before_agent_finalize,reply_payload_sending,agent_end] trace=off mcp_bearer=no slack_bot_token=yes button_direct=no personal_memory=off first_message_restore=on answer_feedback=off';

for (const flag of [undefined, '0', 'false', 'true', '', ' 1', '1 ']) {
  test(`catchup OFF byte invariant for flag ${JSON.stringify(flag)}`, async t => {
    let readinessCalls = 0;
    const p = await setup(t, {envOverrides: {DM_CATCHUP_ENABLED: flag},
      waitForSlackReady: async () => { readinessCalls += 1; throw new Error('must remain unused'); }});
    assert.equal(p.services.length, 0, 'OFF must not register recovery service');
    assert.deepEqual([...p.hooks.keys()], legacyOffHooks);
    assert.equal(p.logs[0], legacyOffBanner);
    const payload = {text: answer, channelData: {slack: {blocks: [
      {type: 'section', text: {type: 'mrkdwn', text: answer}},
    ]}}};
    const bytes = JSON.stringify(payload);
    await p.hooks.get('message_received')(p.event, p.ctx);
    const result = p.hooks.get('reply_payload_sending')({runId: p.ctx.runId,
      kind: 'final', payload}, p.ctx);
    assert.equal(result, undefined, 'OFF preserves the synchronous ordinary reply result');
    assert.equal(JSON.stringify(payload), bytes);
    await Promise.all(p.tasks);
    assert.equal(readinessCalls, 0);
    assert.deepEqual(p.requests, [], 'OFF must not read history or attach reply metadata');
    assert.deepEqual(await readdir(p.state), [], 'OFF must not create catchup state');
  });
}

for (const enabled of [false, true]) {
  test(`catchup alias scope ${enabled ? 'ON allows' : 'OFF rejects'} repeated DM alias binding`, async t => {
    const p = await setup(t, {envOverrides: {DM_CATCHUP_ENABLED: enabled ? '1' : '0'}});
    const runId = 'alias-scope-run';
    if (enabled) {
      await p.start();
      assert.equal(await p.hooks.get('reply_dispatch')({...dispatchEvent(p), runId}, {}), undefined);
    } else {
      await p.hooks.get('message_received')({...p.event, runId: undefined}, {...p.ctx, runId: undefined});
    }
    const agentCtx = {runId, sessionKey: p.ctx.sessionKey, senderId: USER,
      messageProvider: 'slack', trigger: 'user', channel: 'slack', channelId: CHANNEL, chatId: CHANNEL};
    p.hooks.get('before_model_resolve')({}, agentCtx);
    // The old path rejects a repeated run when its channel representation changes.
    // Only the explicitly enabled catchup path needs the canonical-D/DM:user alias bridge.
    const aliasCtx = {...agentCtx, channelId: USER, chatId: USER};
    p.hooks.get('before_model_resolve')({}, aliasCtx);
    const toolName = 'teamagent__search', toolCallId = 'alias-scope-tool';
    const signed = p.hooks.get('before_tool_call')({runId, toolCallId, toolName,
      params: {query: '質問', _user_context: {}}}, {...aliasCtx, toolCallId, toolName});
    if (!enabled) {
      assert.equal(signed?.block, true, 'OFF preserves the original strict repeat comparison');
      assert.ok(p.logs.some(line => line.includes('reason=mismatched_repeat')));
      assert.deepEqual(await readdir(p.state), []);
    } else {
      assert.notEqual(signed?.block, true, signed?.blockReason);
      const claim = JSON.parse(Buffer.from(signed.params._user_context.caller_claim.split('.')[0], 'base64url'));
      assert.equal(claim.channel, CHANNEL);
      assert.equal(claim.message, TS);
    }
  });
}
