import test from 'node:test';
import assert from 'node:assert/strict';
import {createCallerIdentityPlugin} from '../dist/index.js';

const RECEIPT = '資料の作成を受け付けました。完成後、この会話にお届けします。';
const GENERATORS = ['omiyage_report_submit', 'proposal_builder', 'proposal_builder_submit',
  'proposal_deck', 'proposal_draft', 'proposal_campaign', 'clip_proposal_submit'];
const FLAGS = [{}, {SEARCH_DIRECT_ANSWER: '1', SEARCH_DIRECT_ANSWER_ALLOWED_EMAILS: '*',
  SEARCH_DRIVE_FIRST: '1', SEARCH_DRIVE_FIRST_ALLOWED_EMAILS: '*'},
{SEARCH_DIRECT_ANSWER: '1', SEARCH_DIRECT_ANSWER_ALLOWED_EMAILS: 'excluded@example.test',
  SEARCH_DRIVE_FIRST: '1', SEARCH_DRIVE_FIRST_ALLOWED_EMAILS: 'excluded@example.test'}];

function setup(query, {env = {}, senderId = 'U12345678', channelId = 'D12345678'} = {}) {
  const hooks = new Map();
  const logs = [];
  const ctx = {runId: 'generation-run', sessionKey: `agent:teamagent:slack:${channelId}`,
    senderId, channelId};
  createCallerIdentityPlugin({env: {
    TEAMAGENT_CALLER_CLAIM_SECRET: 'x'.repeat(48), SLACK_TEAM_ID: 'T12345678', ...env,
  }, fetchFn: async () => {throw new Error('unexpected external request');}}).register({
    registerInteractiveHandler() {}, on: (name, fn) => hooks.set(name, fn),
    logger: {info: message => logs.push(message), warn: message => logs.push(message)},
  });
  hooks.get('message_received')({content: query, senderId, runId: ctx.runId,
    messageId: '1792748880.123456', metadata: {guildId: 'T12345678', to: channelId}},
  {...ctx, channelId: 'slack', conversationId: channelId, messageId: '1792748880.123456'});
  const complete = (tool, extra = {}, context = ctx) => hooks.get('after_tool_call')({
    runId: ctx.runId, toolName: `teamagent__${tool}`,
    result: {content: [{type: 'text', text: JSON.stringify({status: 'queued', message: RECEIPT})}]},
    ...extra,
  }, context);
  const call = tool => {
    const toolName = `teamagent__${tool}`, toolCallId = `call-${tool}`;
    const signed = hooks.get('before_tool_call')({runId: ctx.runId, toolName,
      toolCallId, params: {brand: '{brand_a}'}}, {...ctx, toolName, toolCallId});
    assert.ok(signed?.params, 'generation call is signed');
    complete(tool);
  };
  const finalize = (text = RECEIPT) => hooks.get('before_agent_finalize')({
    runId: ctx.runId, lastAssistantMessage: text,
  }, ctx);
  const send = (text = RECEIPT) => {
    const out = hooks.get('reply_payload_sending')({runId: ctx.runId, payload: {text}}, ctx);
    assert.notEqual(out?.cancel, true);
    return out?.payload?.text ?? text;
  };
  const end = () => hooks.get('agent_end')({runId: ctx.runId, success: true,
    messages: [{role: 'assistant', content: [{type: 'text', text: RECEIPT}]}]}, ctx);
  return {hooks, ctx, logs, call, complete, finalize, send, end};
}

for (const query of ['{brand_a}のお土産資料を作って。キーワードはお任せ',
  '{brand_a}の提案資料を作成して', '{brand_a}の初訪資料をお願い',
  '社内でいう{term}のスライドを作って', '社内でいう{term}の提案書をつくって',
  '社内でいう{term}のPPTXを作成してください', '社内でいう{term}の資料作成をお願いします']) {
  test(`creation intent does not require internal search without a tool: ${query}`, () => {
    const p = setup(query);
    assert.equal(p.finalize('対象ブランドを教えてください。'), undefined);
  });
}

for (const [flags, env] of FLAGS.entries()) {
  for (const [senderId, channelId] of [['U12345678', 'D12345678'], ['U87654321', 'C87654321']]) {
    for (const tool of GENERATORS) {
      test(`generation receipt survives finalize and agent_end: ${tool} flags=${flags} ${senderId}`, () => {
        const p = setup('{brand_a}のお土産資料を作って。キーワードはお任せ', {env, senderId, channelId});
        p.call(tool);
        assert.equal(p.finalize(), undefined);
        p.end();
        assert.equal(p.send(), RECEIPT);
        assert.ok(!p.logs.some(line => /search zero-tool revise/.test(line)));
      });
    }
    for (const query of ['お土産資料って何？', '社内でいう{term}って何？']) {
      test(`internal question still retries with zero tool calls: ${query} flags=${flags} ${senderId}`, () => {
        const p = setup(query, {env, senderId, channelId});
        const draft = '社内のメニューです。';
        const result = p.finalize(draft);
        assert.equal(result?.action, 'revise');
        assert.equal(result.retry.idempotencyKey, 'search-zero-tool');
        assert.equal(p.finalize(draft), undefined);
        assert.equal(p.send(draft), '社内資料の確認を完了できませんでした。');
      });
    }
  }
}

for (const tool of GENERATORS) {
  for (const prefix of ['', 'teamagent__']) {
    test(`generator execution bypasses internal classification: ${prefix}${tool}`, () => {
      const p = setup('社内でいう{term}って何？');
      p.complete(tool, {toolName: `${prefix}${tool}`});
      assert.equal(p.finalize(), undefined);
      assert.equal(p.send(), RECEIPT);
    });
  }
}

for (const tool of ['web_research', 'omiyage_report_status', 'proposal_builder_status',
  'clip_proposal_status', 'proposal_research', 'proposal_review']) {
  test(`non-generation tool cannot satisfy internal search: ${tool}`, () => {
    const p = setup('お土産資料って何？');
    p.complete(tool);
    assert.equal(p.finalize()?.action, 'revise');
  });
}

for (const empty of [false, true]) {
  test(`successful generation during a retry preserves the receipt: empty=${empty}`, () => {
    const p = setup('社内でいう{term}って何？');
    assert.equal(p.finalize()?.action, 'revise');
    p.call('omiyage_report_submit');
    assert.equal(p.finalize(), undefined);
    p.end();
    const receipt = empty ? '' : '提案資料の作成を受け付けました。';
    assert.equal(p.send(receipt), empty ? RECEIPT : receipt);
  });
}

for (const extra of [{error: new Error('generation failed')}, {result: {isError: true}}]) {
  test(`generation error does not cause an unrelated search retry: ${Object.keys(extra)}`, () => {
    const p = setup('社内でいう{term}って何？');
    p.complete('omiyage_report_submit', extra);
    const message = '資料の作成を開始できませんでした。';
    assert.equal(p.finalize(message), undefined);
    assert.equal(p.send(message), message);
  });
}

test('another run cannot exempt an internal question from search', () => {
  const p = setup('お土産資料って何？');
  p.complete('omiyage_report_submit', {runId: 'other-run'});
  p.complete('omiyage_report_submit', {}, {...p.ctx, runId: 'other-run'});
  assert.equal(p.finalize()?.action, 'revise');
});
