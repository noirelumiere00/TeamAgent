import test from 'node:test';
import assert from 'node:assert/strict';
import {createCallerIdentityPlugin} from '../dist/index.js';

const sheet = 'https://docs.google.com/spreadsheets/d/campaign-actual/edit';
const rows = `📋 伊藤ハム ショート動画実績\n- 動画A：再生507,700、保存127 <${sheet}|施策シート>`;
function setup({direct = true} = {}) {
  const hooks = new Map(), calls = [], posts = [];
  const ctx = {runId: 'previous-run', sessionKey: 'agent:teamagent:slack:direct:u12345678',
    senderId: 'U12345678', channelId: 'U12345678'};
  createCallerIdentityPlugin({env: {
    TEAMAGENT_CALLER_CLAIM_SECRET: 'x'.repeat(48), SLACK_TEAM_ID: 'T07MU5P2PBR',
    TEAMAGENT_MCP_BEARER: 'test-bearer', SLACK_BOT_TOKEN: 'test-token',
    SEARCH_DIRECT_ANSWER: direct ? '1' : '0', SEARCH_DIRECT_ANSWER_ALLOWED_EMAILS: '*',
    SEARCH_DIRECT_ANSWER_KINDS: 'list_fast',
  }, sleepFn: async () => {}, fetchFn: async (url, options) => {
    const body = JSON.parse(options.body);
    let result;
    if (url.endsWith('/conversations.open')) result = {ok: true, channel: {id: 'D12345678'}};
    else if (url.endsWith('/chat.postMessage')) {
      posts.push(body); result = {ok: true};
    } else if (body.method === 'tools/call') {
      calls.push(body.params);
      result = {jsonrpc: '2.0', id: body.id, result: {content: [{type: 'text', text: JSON.stringify({
        answer: rows, found: true, direct_delivery: {answer_kind: 'list_fast', answer: rows},
        video_metrics_evidence: {text: '再生数507,700、保存数127', urls: [sheet]},
      })}]}};
    } else result = {jsonrpc: '2.0', id: body.id, result: {}};
    return {ok: true, text: async () => JSON.stringify(result)};
  }}).register({registerInteractiveHandler() {}, logger: {info() {}, warn() {}},
    on: (name, fn) => hooks.set(name, fn)});
  const receive = (content, messageId) => hooks.get('message_received')({
    content, senderId: ctx.senderId, messageId, runId: ctx.runId,
    metadata: {guildId: 'T07MU5P2PBR', to: `user:${ctx.senderId}`}},
  {...ctx, channelId: 'slack', conversationId: `user:${ctx.senderId}`, messageId});
  const call = (tool, id, params = {}) => hooks.get('before_tool_call')({
    toolName: `teamagent__${tool}`, toolCallId: id, runId: ctx.runId, params},
  {...ctx, toolName: `teamagent__${tool}`, toolCallId: id});
  receive('伊藤ハムのハンバーグの施策は今どうなっている？', '1792041600.123456');
  hooks.get('after_tool_call')({toolName: 'teamagent__search', result: {text:
    '前の回答：再生数999,999、保存数888'}}, ctx);
  hooks.get('agent_end')({runId: ctx.runId}, ctx);
  ctx.runId = 'list-run';
  const followup = async (query = '一覧で出して') => {
    receive(query, '1792041601.123456');
    await hooks.get('before_prompt_build')({runId: ctx.runId, messages: [
      {role: 'user', content: '伊藤ハムのハンバーグの施策は今どうなっている？'},
      {role: 'assistant', content: '根拠のない別施策と999,999再生'},
      {role: 'user', content: query},
    ]}, ctx);
  };
  const finalize = () => hooks.get('before_agent_finalize')({runId: ctx.runId,
    lastAssistantMessage: '一覧で整理します。📋 伊藤ハム ショート動画実績'}, ctx);
  return {ctx, hooks, calls, posts, followup, finalize, call};
}

test('list followup searches fresh in its own run and delivers actual rows once', async () => {
  const p = setup();
  await p.followup();
  assert.equal(await p.finalize(), undefined);
  assert.equal(p.calls.length, 1);
  assert.match(p.calls[0].arguments.query, /一覧で出して/);
  assert.match(p.calls[0].arguments.query, /伊藤ハム.*ハンバーグ/u);
  assert.doesNotMatch(p.calls[0].arguments.query, /根拠のない別施策|999,999/);
  assert.equal(p.posts.length, 1);
  assert.match(p.posts[0].text, /動画A.*507,700.*127/u);
  assert.ok(p.posts[0].text.includes(sheet));
  assert.doesNotMatch(p.posts[0].text, /999,999|888|📊 を参照/u);
  await p.finalize();
  assert.equal(p.calls.length, 1);
});

for (const query of ['一覧で出して', '資料を全部', 'リストアップして']) {
  test(`list followup needs same-run search with direct delivery disabled: ${query}`, async () => {
    const p = setup({direct: false});
    await p.followup(query);
    assert.ok(p.call('chitchat', 'unrelated')?.params);
    assert.equal(p.finalize()?.action, 'revise');
    const out = p.call('search', 'list-search', {query});
    assert.match(out?.params?.query ?? '', /伊藤ハム.*ハンバーグ/u);
    assert.ok(out.params.query.startsWith(query));
    assert.doesNotMatch(out.params.query, /根拠のない別施策/);
  });
}

test('list comparison still follows the lightweight comparison path', async () => {
  const p = setup({direct: false});
  await p.followup('一覧で比較して、何が効いた？');
  assert.equal(p.call('video_algorithm', 'heavy', {query: '伊藤ハム'})?.block, true);
});
