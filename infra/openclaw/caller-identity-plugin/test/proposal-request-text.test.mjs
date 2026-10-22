import test from 'node:test';
import assert from 'node:assert/strict';
import {canonicalRequestSha256, createCallerIdentityPlugin} from '../dist/index.js';

const TEAM = 'T12345678';
const USER = 'U12345678';
const DM = 'D12345678';
const SESSION = 'agent:teamagent:slack:direct:u12345678';

function setup({now = () => 1000} = {}) {
  const hooks = new Map();
  const logs = [];
  let invocation = 0;
  createCallerIdentityPlugin({now, env: {
    TEAMAGENT_CALLER_CLAIM_SECRET: 'x'.repeat(48), SLACK_TEAM_ID: TEAM,
  }}).register({registerInteractiveHandler() {},
    on: (name, fn) => hooks.set(name, fn),
    logger: {info: message => logs.push(message), warn: message => logs.push(message)}});
  const receive = (content, {runId = 'run-1', messageId = '1000.000001',
    senderId = USER, conversationId = DM, sessionKey = SESSION} = {}) => {
    hooks.get('message_received')({content, senderId, messageId, runId,
      metadata: {guildId: TEAM, to: conversationId}},
    {channelId: 'slack', sessionKey, conversationId, senderId, messageId, runId});
  };
  const bind = (runId, channelId = DM) => hooks.get('before_model_resolve')(
    {prompt: 'model-provided context must not be used as request text'},
    {runId, senderId: USER, sessionKey: SESSION, channelId, chatId: channelId,
      messageProvider: 'slack', trigger: 'user'});
  const call = (tool = 'proposal_builder', {runId = 'run-1', channelId = DM,
    sessionKey = SESSION, params = {client_name: 'サンプル'}, eventRunId = runId} = {}) => {
    const toolCallId = `call-${++invocation}`;
    const toolName = `teamagent__${tool}`;
    return hooks.get('before_tool_call')({toolName, runId: eventRunId, toolCallId, params},
      {toolName, runId, toolCallId, channelId, sessionKey, senderId: USER});
  };
  return {hooks, logs, receive, bind, call};
}

for (const tool of ['proposal_builder', 'proposal_builder_submit']) {
  test(`${tool} injects the run's DM request and overwrites model text before signing`, () => {
    const p = setup();
    const content = '見積は３本・１０万再生・単価５円／回でお願いします。';
    p.receive(content);
    const result = p.call(tool, {params: {client_name: 'サンプル', _user_context: {
      slack_user_id: USER, request_text: '本数999、再生999万、単価999円',
    }}});
    assert.notEqual(result?.block, true);
    assert.equal(result.params._user_context.request_text, content);
    const claim = JSON.parse(Buffer.from(result.params._user_context.caller_claim.split('.')[0],
      'base64url').toString());
    assert.equal(claim.arguments_sha256, canonicalRequestSha256(result.params));
    assert.notEqual(claim.arguments_sha256, canonicalRequestSha256({...result.params,
      _user_context: {...result.params._user_context, request_text: 'tampered'}}));
    assert.ok(p.logs.every(line => !line.includes(content)));
  });
}

test('proposal request text is limited to 4,000 Unicode characters without splitting a character', () => {
  const p = setup();
  p.receive('🙂'.repeat(3999) + '。' + '単価999円');
  const text = p.call().params._user_context.request_text;
  assert.equal([...text].length, 4000);
  assert.equal(text, '🙂'.repeat(3999) + '。');
});

test('truncation discards a trailing numeric token instead of authorizing a partial amount', () => {
  for (const token of ['１００，０００', '1.5万', '５ 円／回', '60%', '３ヶ月']) {
    const p = setup();
    const prefix = 'あ'.repeat(4000 - token.length + 1);
    p.receive(prefix + token + 'で見積');
    assert.equal(p.call().params._user_context.request_text, prefix);
  }
  const p = setup();
  const exact = 'あ'.repeat(3996) + '１０万円';
  p.receive(exact);
  assert.equal(p.call().params._user_context.request_text, exact);
});

test('other tools receive no request text even if the model supplies it', () => {
  const p = setup();
  p.receive('利用者の依頼本文');
  for (const tool of ['search', 'proposal_builder_status', 'proposal_deck']) {
    const result = p.call(tool, {params: {_user_context: {request_text: 'モデルの値'}}});
    assert.notEqual(result?.block, true);
    assert.equal(Object.hasOwn(result.params._user_context, 'request_text'), false);
  }
});

test('missing DM body and non-DM messages never accept model-provided request text', () => {
  for (const [content, conversationId] of [[undefined, DM], ['予算100万円', 'C12345678']]) {
    const p = setup();
    p.receive(content, {conversationId});
    const result = p.call('proposal_builder', {channelId: conversationId,
      params: {_user_context: {request_text: 'モデルの予算200万円'}}});
    assert.notEqual(result?.block, true);
    assert.equal(Object.hasOwn(result.params._user_context, 'request_text'), false);
  }
});

test('pending DM ingress is bound to the latest user message in the same conversation', () => {
  const p = setup();
  p.receive('過去の依頼は１本', {runId: null, messageId: '1000.000001'});
  p.receive('今回の依頼は２本', {runId: null, messageId: '1000.000002'});
  p.receive('別会話は999本', {runId: null, messageId: '1000.000003',
    conversationId: 'D87654321', sessionKey: 'other-session'});
  p.bind('run-current');
  assert.equal(p.call('proposal_builder', {runId: 'run-current'})
    .params._user_context.request_text, '今回の依頼は２本');
});

test('the bound request stays isolated from later messages and other runs', () => {
  const p = setup();
  p.receive('最初の依頼は１本');
  p.receive('次の依頼は２本', {runId: 'run-2', messageId: '1000.000002'});
  assert.equal(p.call().params._user_context.request_text, '最初の依頼は１本');
  assert.equal(p.call('proposal_builder', {runId: 'run-2'})
    .params._user_context.request_text, '次の依頼は２本');
  assert.equal(p.call('proposal_builder', {runId: 'run-2', eventRunId: 'run-1'}).block, true);
  assert.equal(p.call('proposal_builder', {channelId: 'D87654321'}).block, true);
});

for (const runId of [null, 'run-1']) {
  for (const bodies of [[undefined, '依頼は３本'], ['依頼は３本', undefined]]) {
    test(`duplicate ingress retains the DM body when one notification lacks it: ${runId}/${bodies[0]}`, () => {
      const p = setup();
      for (const body of bodies) p.receive(body, {runId});
      if (!runId) p.bind('run-1');
      assert.equal(p.call().params._user_context.request_text, '依頼は３本');
    });
  }
}

test('DM alias binding preserves the request, and expired runs cannot reuse it', () => {
  let nowMs = 1000;
  const p = setup({now: () => nowMs});
  p.receive('見積は４本', {runId: null, conversationId: `user:${USER}`});
  p.bind('run-1');
  assert.equal(p.call().params._user_context.request_text, '見積は４本');
  nowMs += 10 * 60 * 1000 + 1;
  assert.equal(p.call().block, true);
});
