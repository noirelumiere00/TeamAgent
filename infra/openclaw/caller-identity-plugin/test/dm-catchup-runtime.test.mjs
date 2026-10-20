import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp, readFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import * as plugin from '../dist/index.js';

const TEAM = 'T07MU5P2PBR', USER = 'U12345678', CHANNEL = 'D12345678';
const NOW = Date.parse('2026-10-19T03:00:00Z');
const SOURCE_TS = `${Math.floor(NOW / 1000) - 60}.000001`;
const TOKEN = 'test-bot-token-never-log';
const config = {session: {dmScope: 'per-channel-peer'}, channels: {slack: {
  enabled: true, dmPolicy: 'allowlist', allowFrom: [USER],
}}};
const source = extra => ({type: 'message', channel: CHANNEL, user: USER,
  ts: SOURCE_TS, text: '添付を調べてください', ...extra});
const response = payload => ({ok: true, status: 200, headers: {get: () => null},
  text: async () => JSON.stringify(payload)});

function setup({deliveries = [{kind: 'final', payload: {text: '回答です'}}], fetchError,
  swallowDeliveryError = false, prepareInCore, responseExtra = {}, beforeDispatchReturns} = {}) {
  const calls = [], prepared = [], records = [], dispatches = [], hookCalls = [];
  const sessionKey = `agent:teamagent:slack:direct:${USER.toLowerCase()}`;
  const api = {runtime: {channel: {
    routing: {resolveAgentRoute(args) {
      assert.equal(args.cfg, config);
      assert.deepEqual(args.peer, {kind: 'direct', id: USER});
      assert.equal(args.teamId, TEAM);
      return {agentId: 'teamagent', accountId: 'default', sessionKey, mainSessionKey: sessionKey};
    }},
    inbound: {buildContext(args) {
      prepared.push(args);
      return {From: args.from, To: args.reply.to, OriginatingTo: args.reply.originatingTo,
        SessionKey: args.route.routeSessionKey, MessageSid: args.messageId,
        GroupSpace: args.conversation.spaceId, NativeChannelId: args.conversation.nativeChannelId,
        SenderId: args.sender.id, Provider: args.channel, Surface: args.channel,
        ChatType: args.conversation.kind, RawBody: args.message.rawBody,
        Body: args.message.body, BodyForAgent: args.message.bodyForAgent};
    }},
    session: {resolveStorePath: () => '/test/sessions.json',
      recordInboundSession: async args => {records.push(args);}},
    reply: {dispatchReplyWithBufferedBlockDispatcher: async args => {
      dispatches.push(args);
      assert.equal(records.length, 1, 'session is recorded before normal processing');
      for (const {kind, payload} of deliveries) {
        try {
          // The pinned public dispatcher runs the global reply_payload_sending hook before deliver.
          hookCalls.push({payload, kind});
          const result = await prepareInCore?.(payload, kind);
          if (!result?.cancel) await args.dispatcherOptions.deliver(result?.payload ?? payload, {kind});
        }
        catch (error) {
          if (!swallowDeliveryError) throw error;
          args.dispatcherOptions.onError(error, {kind});
        }
      }
      beforeDispatchReturns?.(calls);
      return {queuedFinal: true, counts: {tool: 0, block: 0, final: 1}};
    }},
  }}};
  const fetchFn = async (url, options) => {
    if (fetchError) throw fetchError;
    const method = new URL(url).pathname.split('/').at(-1);
    const body = JSON.parse(options.body);
    calls.push({method, body});
    assert.equal(options.headers.Authorization, `Bearer ${TOKEN}`);
    return response({ok: true, channel: CHANNEL,
      ts: `${Math.floor(NOW / 1000)}.${String(calls.length).padStart(6, '0')}`, ...responseExtra});
  };
  const dispatch = (message = source(), changes = {}) => {
    assert.equal(typeof plugin.dispatchCaughtUpDm, 'function', 'public replay adapter is required');
    return plugin.dispatchCaughtUpDm({api, config, message, teamId: TEAM,
      botToken: TOKEN, fetchFn, now: () => NOW, ...changes});
  };
  return {dispatch, calls, prepared, records, dispatches, sessionKey, hookCalls};
}

test('catchup uses the public inbound route and preserves Slack caller identity through final delivery', async () => {
  const blocks = [{type: 'section', text: {type: 'mrkdwn', text: '回答です'}}];
  const h = setup({deliveries: [
    {kind: 'tool', payload: {text: '確認中です'}},
    {kind: 'final', payload: {text: '回答です', channelData: {slack: {blocks}}}},
  ]});
  const result = await h.dispatch();
  const {ctx, cfg} = h.dispatches[0];
  assert.equal(cfg, config);
  assert.equal(ctx.MessageSid, SOURCE_TS);
  assert.equal(ctx.SenderId, USER);
  assert.equal(ctx.GroupSpace, TEAM);
  assert.equal(ctx.NativeChannelId, CHANNEL);
  assert.equal(ctx.From, `slack:${USER}`);
  assert.equal(ctx.To, `user:${USER}`);
  assert.equal(ctx.OriginatingTo, `user:${USER}`, 'caller DM alias matches the session-derived model hook alias');
  assert.equal(ctx.SessionKey, h.sessionKey);
  assert.equal(ctx.ReplyToMode, 'off');
  assert.match(ctx.Body, new RegExp(`slack message id: ${SOURCE_TS} channel: ${CHANNEL}`));
  assert.equal(h.prepared[0].access.commands.authorized, false);
  assert.deepEqual(h.records[0].updateLastRoute, {sessionKey: h.sessionKey, channel: 'slack',
    to: `user:${USER}`, accountId: 'default'});
  assert.deepEqual(h.calls.map(call => call.method), ['chat.postMessage', 'chat.postMessage']);
  assert.deepEqual(h.calls[1].body.blocks, blocks);
  assert.equal(h.calls[0].body.metadata.event_payload.complete, false);
  assert.deepEqual(h.calls[1].body.metadata, {event_type: 'teamagent.dm_answer_v1', event_payload: {
    source_channel: CHANNEL, source_ts: SOURCE_TS, complete: true,
  }});
  assert.equal(result.finalReplyTs, `${Math.floor(NOW / 1000)}.000002`);
  assert.equal(result.replyTs, result.finalReplyTs);
  assert.equal(result.receipts.length, 2);
});

test('each replay supplies a unique run ID without replacing the original Slack message ID', async () => {
  const first = setup(), second = setup();
  const original = source();
  await first.dispatch(original);
  await second.dispatch(source());
  const firstRun = first.dispatches[0].replyOptions.runId;
  const secondRun = second.dispatches[0].replyOptions.runId;
  assert.match(firstRun, /^catchup-[a-f0-9]{32}$/u);
  assert.match(secondRun, /^catchup-[a-f0-9]{32}$/u);
  assert.notEqual(firstRun, secondRun);
  assert.equal(first.dispatches[0].ctx.MessageSid, SOURCE_TS);
  assert.equal(second.dispatches[0].ctx.MessageSid, SOURCE_TS);
  assert.equal(original.ts, SOURCE_TS);
  assert.equal(original.runId, undefined);
});

test('untrusted or unsupported history messages fail before dispatch or Slack writes', async () => {
  const invalid = [source({channel: 'C12345678'}), source({team: 'T99999999'}),
    source({user: 'U99999999'}), source({bot_id: 'B12345678'}), source({subtype: 'message_changed'}),
    source({thread_ts: '1.000001'}), source({text: '', files: [{id: 'F12345678'}]}),
    source({text: '/new'}), source({ts: '1.000001'})];
  for (const message of invalid) {
    const h = setup();
    await assert.rejects(() => h.dispatch(message), /catchup/iu);
    assert.equal(h.calls.length, 0);
    assert.equal(h.dispatches.length, 0);
  }
});

test('unsupported outbound media and swallowed delivery failures cannot produce a completed answer', async () => {
  const h = setup({deliveries: [{kind: 'final', payload: {text: '回答です', mediaUrl: '/secret/file'}}],
    swallowDeliveryError: true});
  await assert.rejects(() => h.dispatch(), /catchup/iu);
  assert.equal(h.calls.length, 0);
  const failed = setup({fetchError: new Error(`${TOKEN}: private question`), swallowDeliveryError: true});
  await assert.rejects(() => failed.dispatch(), error => {
    assert.doesNotMatch(error.message, new RegExp(`${TOKEN}|private question`));
    return true;
  });
  assert.equal(failed.calls.length, 0);
});

test('a later delivery failure prevents all buffered final posts', async () => {
  const h = setup({deliveries: [{kind: 'final', payload: {text: '前半'}},
    {kind: 'final', payload: {mediaUrl: '/unsupported'}}], swallowDeliveryError: true});
  await assert.rejects(() => h.dispatch(), /catchup/iu);
  assert.equal(h.calls.length, 0);
});

test('final posts are buffered until core returns and only the last post is complete', async () => {
  const h = setup({deliveries: [{kind: 'final', payload: {text: '前半'}},
    {kind: 'final', payload: {text: '後半'}}], beforeDispatchReturns: calls => {
      assert.equal(calls.length, 0, 'a crash before core returns must not leave an answered-but-unmarked final');
    }});
  const result = await h.dispatch();
  assert.deepEqual(h.calls.map(call => call.method), ['chat.postMessage', 'chat.postMessage']);
  assert.deepEqual(h.calls.map(call => call.body.metadata.event_payload.complete), [false, true]);
  assert.equal(result.finalReplyTs, `${Math.floor(NOW / 1000)}.000002`);
});

test('replay sends the once-prepared public dispatcher payload and respects cancellation', async () => {
  const h = setup({deliveries: [{kind: 'final', payload: {text: 'cancelled duplicate'}},
    {kind: 'final', payload: {text: 'unsafe tool_name'}}], prepareInCore: (payload) => {
      return payload.text.startsWith('cancelled') ? {cancel: true} : {payload: {text: 'scrubbed answer'}};
    }});
  await h.dispatch();
  assert.equal(h.hookCalls.length, 2);
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].body.text, 'scrubbed answer');
  assert.equal(h.calls[0].body.metadata.event_payload.complete, true);
});

test('Slack warnings or conflicting returned metadata never count as a delivery receipt', async () => {
  for (const responseExtra of [{warning: 'metadata_ignored'},
    {response_metadata: {warnings: ['metadata_ignored']}},
    {message: {metadata: {event_type: 'wrong', event_payload: {complete: true}}}}]) {
    const h = setup({responseExtra});
    await assert.rejects(() => h.dispatch(), /catchup/iu);
    assert.equal(h.calls.length, 1, 'ambiguous post is not retried');
  }
});

test('attachment questions stay unresolved rather than read a later question attachment', async () => {
  const h = setup();
  await assert.rejects(() => h.dispatch(source({subtype: 'file_share', files: [{id: 'F12345678'}]})), /unsupported_source/u);
  assert.equal(h.dispatches.length, 0);
  assert.equal(h.calls.length, 0);
});

function readyHarness({states, requestError, hanging = false, startError, timeoutMs = 2000} = {}) {
  let clock = NOW, options, stopped = 0, requests = 0;
  const status = connected => ({channelDefaultAccountId: {slack: 'default'}, channelAccounts: {slack: [
    {accountId: 'default', connected, lastConnectedAt: NOW},
  ]}});
  const responses = states ?? [status(false), status(true)];
  class FakeGatewayClient {
    constructor(value) {options = value;}
    start() {
      if (startError) throw startError;
      queueMicrotask(() => options.onHelloOk({}));
    }
    async request(method, params, requestOptions) {
      requests++;
      assert.equal(method, 'channels.status');
      assert.deepEqual(params, {channel: 'slack', probe: false});
      assert.ok(requestOptions.timeoutMs > 0 && requestOptions.timeoutMs <= timeoutMs);
      assert.ok(requestOptions.signal instanceof AbortSignal);
      if (requestError) throw requestError;
      if (hanging) return new Promise(() => {});
      return responses[Math.min(requests - 1, responses.length - 1)];
    }
    async stopAndWait(opts) {assert.deepEqual(opts, {timeoutMs: 1000}); stopped++;}
  }
  const wait = changes => {
    assert.equal(typeof plugin.waitForSlackTransportReady, 'function');
    return plugin.waitForSlackTransportReady({config: {gateway: {port: 18789}},
      env: {OPENCLAW_GATEWAY_TOKEN: TOKEN, OPENCLAW_GATEWAY_URL: 'wss://untrusted.invalid'},
      loadClient: async () => FakeGatewayClient, now: () => clock,
      sleepFn: async ms => {clock += ms;}, timeoutMs, pollIntervalMs: 1000, ...changes});
  };
  return {wait, status, get options() {return options;}, get stopped() {return stopped;},
    get requests() {return requests;}, get clock() {return clock;}};
}

test('readiness waits for Socket connected status using a scoped loopback client, then closes it', async () => {
  const h = readyHarness();
  const result = await h.wait();
  assert.deepEqual(result, {connectedAt: NOW, observedAt: NOW + 1000});
  assert.equal(h.requests, 2);
  assert.equal(h.options.url, 'ws://127.0.0.1:18789');
  assert.equal(h.options.token, TOKEN);
  assert.equal(h.options.clientName, 'gateway-client');
  assert.equal(h.options.mode, 'backend');
  assert.equal(h.options.role, 'operator');
  assert.deepEqual(h.options.scopes, ['operator.read']);
  assert.equal(h.options.deviceIdentity, null);
  assert.doesNotThrow(() => h.options.hostDeps.logError(`${TOKEN} private question`));
  assert.equal(h.stopped, 1);
});

test('readiness failures and disconnected snapshots stay bounded and expose only a fixed error', async () => {
  const disconnected = readyHarness({states: [{channelDefaultAccountId: {slack: 'default'},
    channelAccounts: {slack: [{accountId: 'default', connected: false, lastConnectedAt: NOW}]}}]});
  const rejected = readyHarness({requestError: new Error(`${TOKEN}: private question`)});
  const hanging = readyHarness({hanging: true, timeoutMs: 20});
  for (const h of [disconnected, rejected, hanging]) {
    await assert.rejects(() => h.wait(), error => {
      assert.equal(error.code, 'dm_catchup_transport_not_ready');
      assert.doesNotMatch(error.message, new RegExp(`${TOKEN}|private question`));
      return true;
    });
    assert.equal(h.stopped, 1);
  }
});

test('a ready Slack account other than the configured default cannot start replay', async () => {
  const h = readyHarness({states: [{channelDefaultAccountId: {slack: 'default'}, channelAccounts: {slack: [
    {accountId: 'other', connected: true, lastConnectedAt: NOW},
  ]}}]});
  await assert.rejects(() => h.wait(), {code: 'dm_catchup_transport_not_ready'});
  assert.equal(h.stopped, 1);
});

test('startup authenticates, waits for Socket ready, then fixes the history cutoff', async t => {
  const directory = await mkdtemp(join(tmpdir(), 'oc-catchup-ready-'));
  t.after(() => rm(directory, {recursive: true, force: true}));
  const h = readyHarness(), requests = [], dispatched = [];
  const afterBootTs = `${Math.floor((NOW + 500) / 1000)}.500000`;
  const controller = plugin.createDmCatchup({statePath: join(directory, 'journal.json'), teamId: TEAM,
    botToken: TOKEN, now: () => h.clock, dispatch: async message => dispatched.push(message),
    waitForTransportReady: async () => {
      assert.deepEqual(requests, ['auth.test']);
      return await h.wait();
    }, fetchFn: async (url, opts) => {
      const method = url.split('/').at(-1), body = JSON.parse(opts.body);
      requests.push(method);
      if (method === 'auth.test') return response({ok: true, team_id: TEAM, user_id: 'U87654321'});
      if (method === 'conversations.list') return response({ok: true, channels: [{id: CHANNEL, user: USER}]});
      assert.equal(method, 'conversations.history');
      assert.equal(body.latest, `${Math.floor((NOW + 1000) / 1000)}.000000`);
      return response({ok: true, messages: [source({ts: afterBootTs})]});
    }});
  const result = await controller.start();
  assert.equal(result.replayed, 1);
  assert.equal(dispatched[0].ts, afterBootTs);
  assert.equal(h.requests, 2);
  const failed = plugin.createDmCatchup({statePath: join(directory, 'failed.json'), teamId: TEAM,
    botToken: TOKEN, now: () => NOW, dispatch: async () => assert.fail('disconnected replay'),
    waitForTransportReady: async () => {throw new Error(`${TOKEN} private question`);},
    fetchFn: async url => {
      assert.equal(url.split('/').at(-1), 'auth.test');
      return response({ok: true, team_id: TEAM, user_id: 'U87654321'});
    }});
  assert.deepEqual(await failed.start(), {replayed: 0, scanned: 0});
  const journal = JSON.parse(await readFile(join(directory, 'failed.json'), 'utf8'));
  assert.ok(journal.unresolved.some(entry => entry.reason === 'transport_not_ready' && entry.count >= 1));
  assert.equal(await failed.reserve(source(), {source: 'live'}), true, 'normal live tracking remains available');
});
