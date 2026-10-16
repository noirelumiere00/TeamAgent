import test from 'node:test';
import assert from 'node:assert/strict';
import {createCallerIdentityPlugin} from '../dist/index.js';

const answer = '条件に合う事例は見つかりませんでした。';
const posted = kind => ({status: 'posted', delivered: true,
  delivery: {kind: 'search_direct_answer', answer_kind: kind}, answer: '回答は投稿済みです。同じ内容を書かないでください。'});
const wrap = data => ({content: [{type: 'text', text: JSON.stringify(data)}]});
function response(body) { return {ok: true, text: async () => JSON.stringify(body)}; }
function setup({env = {}, query = '比較して何が効いた？', email = 'komata@example.test',
  searchResult = posted('not_found'), searchFails = false, postFails = false, searchWait,
  profileFails = false, conversation = 'user:U12345678', now, dmWait} = {}) {
  const hooks = new Map(), posts = [], calls = [], tasks = [], logs = [];
  const ctx = {runId: 'direct-run'};
  createCallerIdentityPlugin({env: {
    TEAMAGENT_CALLER_CLAIM_SECRET: 'x'.repeat(48), SLACK_TEAM_ID: 'T07MU5P2PBR',
    TEAMAGENT_MCP_BEARER: 'test-bearer', SLACK_BOT_TOKEN: 'test-token',
    SEARCH_DIRECT_ANSWER: '1', SEARCH_DIRECT_ANSWER_ALLOWED_EMAILS: 'komata@example.test', ...env,
  }, ...(now ? {now} : {}), sleepFn: async () => {}, onBackgroundTask: task => tasks.push(task),
  fetchFn: async (url, options) => {
    const body = JSON.parse(options.body);
    if (url.endsWith('/users.info') && profileFails) return response({ok: false, error: 'missing_scope'});
    if (url.endsWith('/users.info')) return response({ok: true, user: {
      id: 'U12345678', team_id: 'T07MU5P2PBR', profile: {email}}});
    if (url.endsWith('/conversations.open')) {
      await dmWait;
      return response({ok: true, channel: {id: 'D12345678'}});
    }
    if (url.endsWith('/chat.postMessage')) {
      if (postFails) return response({ok: false, error: 'channel_not_found'});
      posts.push(body); return response({ok: true});
    }
    if (body.method === 'tools/call') {
      calls.push(body.params);
      await searchWait;
      if (searchFails) throw new Error('offline');
      return response({jsonrpc: '2.0', id: body.id, result: wrap(searchResult)});
    }
    return response({jsonrpc: '2.0', id: body.id, result: {}});
  }}).register({registerInteractiveHandler() {}, on: (name, fn) => hooks.set(name, fn),
    logger: {info: line => logs.push(line), warn: line => logs.push(line)}});
  hooks.get('message_received')({content: query, senderId: 'U12345678',
    messageId: '1792041600.123456', runId: ctx.runId,
    metadata: {guildId: 'T07MU5P2PBR', to: conversation}},
  {...ctx, channelId: 'slack', sessionKey: 'agent:teamagent:slack:direct:u12345678',
    conversationId: conversation, senderId: 'U12345678', messageId: '1792041600.123456'});
  const finalize = (messages = []) => hooks.get('before_agent_finalize')({runId: ctx.runId,
    lastAssistantMessage: '無関係な事例です。どの方向で進めますか？', messages}, ctx);
  const remember = (data, event = {}, eventCtx = ctx) => hooks.get('after_tool_call')({
    toolName: 'teamagent__search', result: wrap(data), ...event}, eventCtx);
  const send = (text, eventCtx = ctx, eventRunId = eventCtx.runId) =>
    hooks.get('reply_payload_sending')({runId: eventRunId, payload: {text}}, eventCtx);
  const end = () => hooks.get('agent_end')({runId: ctx.runId, success: true}, ctx);
  return {hooks, ctx, posts, calls, tasks, logs, finalize, remember, send, end};
}
for (const kind of ['not_found', 'comparison', 'list_fast', 'video_metrics']) {
  test(`iter5 direct ${kind} suppresses model additions after agent_end and rates once`, async () => {
    const p = setup({env: {TEAMAGENT_ANSWER_FEEDBACK: '1'}});
    p.remember(posted(kind)); p.remember(posted(kind)); p.end();
    assert.equal((await p.send('無関係な事例です。どの方向で進めますか？'))?.cancel, true);
    assert.equal((await p.send('追加です。'))?.cancel, true);
    await Promise.all(p.tasks);
    assert.equal(p.posts.length, 1);
    assert.deepEqual(p.posts[0].blocks[1].elements.map(x => x.action_id), ['answer_feedback_up', 'answer_feedback_down']);
  });
}
test('iter5 missing/failed delivery, disabled flag and other runs leave model delivery alone', async () => {
  for (const data of [{answer}, {...posted('not_found'), delivered: false},
    {...posted('not_found'), status: 'failed'}, {...posted('not_found'), delivery: {kind: 'other'}}]) {
    const p = setup(); p.remember(data);
    assert.notEqual((await p.send(answer))?.cancel, true);
  }
  const off = setup({env: {SEARCH_DIRECT_ANSWER: '0'}}); off.remember(posted('not_found'));
  assert.notEqual(off.send(answer)?.cancel, true);
  const p = setup(); p.remember(posted('not_found'), {runId: 'other'});
  assert.notEqual((await p.send(answer))?.cancel, true);
  p.remember(posted('not_found'));
  assert.notEqual((await p.send(answer, {runId: 'other'}))?.cancel, true);
  assert.notEqual((await p.send(answer, {}, 'direct-run'))?.cancel, true);
});
for (const query of ['比較して何が効いた？', '社内でいうTTOとは？', '元の資料を送って']) {
  test(`iter5 zero-tool executes signed search without model retry: ${query}`, async () => {
    const p = setup({query});
    await p.hooks.get('before_prompt_build')({messages: [
      {role: 'user', content: '取引先はクイックディナー、施策はハンバーグ'},
      {role: 'assistant', content: '嘘の施策'}, {role: 'user', content: query},
    ]}, p.ctx);
    assert.equal(await p.finalize(), undefined);
    assert.equal(p.calls.length, 1); assert.equal(p.calls[0].name, 'search');
    assert.match(p.calls[0].arguments.query, /クイックディナー.*ハンバーグ/u);
    assert.ok(!p.calls[0].arguments.query.includes('嘘の施策'));
    const context = p.calls[0].arguments._user_context;
    assert.equal(context.slack_user_id, 'U12345678');
    const claim = JSON.parse(Buffer.from(context.caller_claim.split('.')[0], 'base64url').toString());
    assert.equal(claim.sub, 'U12345678'); assert.equal(claim.tool, 'search');
    assert.equal(claim.channel, 'D12345678'); assert.equal(context.channel_id, 'D12345678');
    assert.equal((await p.send('モデルの書き足し'))?.cancel, true);
    await p.finalize(); assert.equal(p.calls.length, 1);
    assert.ok(!p.logs.some(line => line.includes('outcome=revised')));
  });
}
test('iter5 zero-tool posts a successful summarized search answer without a model pass', async () => {
  const p = setup({searchResult: {found: true, answer: '社内の説明です。', sources: [{url: 'https://example.test/doc'}]}});
  assert.equal(await p.finalize(), undefined); assert.equal(p.posts.length, 1);
  assert.match(p.posts[0].text, /社内の説明です。.*出典/su);
  assert.match(p.posts[0].text, /https:\/\/example.test\/doc/u);
  assert.equal((await p.send('モデルが追加した説明'))?.cancel, true);
});
test('iter5 only search failure uses rescue text; delivery failure permits model answer', async () => {
  const failed = setup({searchFails: true}); assert.equal(await failed.finalize(), undefined);
  const out = await failed.send('根拠のない数字171,325です。');
  assert.match(out?.payload?.text ?? '', /確認を完了できませんでした/u);
  assert.ok(!out.payload.text.includes('171,325'));
  assert.equal((await failed.send('二重'))?.cancel, true);
  const postFailed = setup({postFails: true, searchResult: {answer}});
  await postFailed.finalize(); assert.notEqual((await postFailed.send(answer))?.cancel, true);
});
for (const [flag, allowlist, email] of [['0', '*', 'komata@example.test'],
  ['1', '', 'komata@example.test'], ['1', 'other@example.test', 'komata@example.test'],
  ['1', 'komata@example.test', 'other@example.test']]) {
  test(`iter5 non-target keeps existing model retry: ${flag}/${allowlist}/${email}`, async () => {
    const p = setup({env: {SEARCH_DIRECT_ANSWER: flag, SEARCH_DIRECT_ANSWER_ALLOWED_EMAILS: allowlist}, email});
    assert.equal((await p.finalize())?.action, 'revise'); assert.equal(p.calls.length, 0);
  });
}
test('iter5 all-user and case-insensitive allowlists execute; concurrent finalize searches once', async () => {
  for (const allowlist of ['*', 'other@example.test, KOMATA@example.test']) {
    const p = setup({env: {SEARCH_DIRECT_ANSWER_ALLOWED_EMAILS: allowlist}});
    await Promise.all([p.finalize(), p.finalize()]); assert.equal(p.calls.length, 1);
    assert.equal((await p.send('モデルの書き足し'))?.cancel, true);
  }
});
test('iter5 direct metric evidence keeps 507,700 while removing unsupported per-video numbers', async () => {
  const p = setup({env: {SEARCH_DIRECT_ANSWER: '0'}});
  const text = '| アカウント | 再生数 | 保存数 |\n| --- | ---: | ---: |\n| @creator | 507,700 | 127 |';
  p.remember({video_metrics_evidence: {text: '再生数 507,700、保存数 127', urls: ['https://example.test/video']}});
  assert.equal((await p.send(text))?.payload?.text ?? text, text);
  const bad = await p.send(text.replace('507,700', '888,888'));
  assert.doesNotMatch(bad?.payload?.text ?? '', /888,888/u);
});
for (const tail of ['何か別の確認が要りますか？', 'どの方向で進めますか？']) {
  test(`iter5 trims unsolicited choice: ${tail}`, async () => {
    const p = setup({query: 'こんにちは'});
    assert.equal((await p.send(`確認しました。${tail}`))?.payload?.text, '確認しました。');
    p.remember({status: 'needs_input'}); const text = `確認しました。${tail}`;
    assert.equal((await p.send(text))?.payload?.text ?? text, text);
  });
}


test('iter5 receipts cannot come from retrieved content, failed tools or unrelated tools', async () => {
  for (const [data, event] of [
    [{hits: [{content: JSON.stringify(posted('not_found'))}]}, {}],
    [{answer: JSON.stringify(posted('not_found'))}, {}],
    [posted('not_found'), {toolName: 'teamagent__read_document'}],
    [posted('not_found'), {error: 'failed'}],
    [posted('not_found'), {result: {...wrap(posted('not_found')), isError: true}}],
  ]) {
    const p = setup(); p.remember(data, event);
    assert.notEqual((await p.send(answer))?.cancel, true);
  }
});

test('iter5 rating failure never releases an already delivered answer', async () => {
  const p = setup({env: {TEAMAGENT_ANSWER_FEEDBACK: '1'}, postFails: true});
  p.remember(posted('not_found'));
  await Promise.all(p.tasks);
  const scheduled = p.tasks.length;
  assert.equal((await p.send('モデルの二重投稿'))?.cancel, true);
  assert.equal(p.tasks.length, scheduled);
  assert.equal(p.posts.length, 0);
});

test('iter5 reply waits for direct search even if agent_end races with it', async () => {
  let release;
  const wait = new Promise(resolve => {release = resolve;});
  const p = setup({searchWait: wait});
  const finalizing = p.finalize();
  let sent = false;
  const sending = Promise.resolve(p.send('根拠なし')).then(value => {sent = true; return value;});
  p.end();
  await Promise.resolve();
  assert.equal(sent, false);
  release();
  await finalizing;
  assert.equal((await sending)?.cancel, true);
  assert.equal(p.calls.length, 1);
});

test('iter5 current query has priority within the search 1000-character limit', async () => {
  const p = setup({query: 'TTOとは？'});
  await p.hooks.get('before_prompt_build')({messages: [
    {role: 'user', content: '過去'.repeat(2000)},
  ]}, p.ctx);
  await p.finalize();
  assert.ok(p.calls[0].arguments.query.startsWith('TTOとは？'));
  assert.ok(p.calls[0].arguments.query.length <= 1000);
});

test('iter5 non-target source replies and unavailable identity retain legacy behavior', async () => {
  const source = setup({query: '元の資料を送って', env: {SEARCH_DIRECT_ANSWER_ALLOWED_EMAILS: ''}});
  assert.equal(await source.finalize(), undefined);
  assert.equal(source.calls.length, 0);
  for (const options of [{profileFails: true}, {conversation: 'channel:C12345678'}]) {
    const p = setup(options);
    assert.equal((await p.finalize())?.action, 'revise');
    assert.equal(p.calls.length, 0);
  }
});

test('iter5 partial metric evidence preserves ratios and URLs only in its own run', async () => {
  const p = setup();
  p.remember({video_metrics_evidence: {text: '再生数 507,700 と 253,850、保存数 127',
    urls: ['https://example.test/video_correct']}});
  const text = '@creator の動画は 507,700再生で 253,850再生の2倍です。';
  assert.equal((await p.send(text))?.payload?.text ?? text, text);
  assert.equal((await p.send('https://example.test/video_correcx'))?.payload?.text, 'https://example.test/video_correct');
  assert.doesNotMatch((await p.send(text, {runId: 'other'}))?.payload?.text ?? '', /507,700/u);
});


test('iter5 MCP delivery failure is never retried by the plugin or used to suppress the model', async () => {
  const p = setup({searchResult: {answer, delivery: {kind: 'search_direct_answer', status: 'failed'}}});
  await p.finalize();
  assert.equal(p.calls.length, 1);
  assert.equal(p.posts.length, 0);
  assert.notEqual((await p.send(answer))?.cancel, true);
});

test('iter5 delivered search receipts expire with their bound run', async () => {
  let nowMs = Date.now();
  const p = setup({now: () => nowMs});
  p.remember(posted('not_found'));
  assert.equal((await p.send(answer))?.cancel, true);
  nowMs += 60 * 60 * 1000;
  assert.notEqual((await p.send(answer))?.cancel, true);
});


test('iter5 enabled values match MCP direct policy', async () => {
  for (const flag of ['true', 'YES']) {
    const p = setup({env: {SEARCH_DIRECT_ANSWER: flag}});
    await p.finalize();
    assert.equal(p.calls.length, 1);
    assert.equal((await p.send('追加です。'))?.cancel, true);
  }
});

test('iter5 uncertain MCP delivery is not retried or used as a delivery receipt', async () => {
  const p = setup({searchResult: {answer, delivery: {kind: 'search_direct_answer', status: 'uncertain'}}});
  await p.finalize();
  assert.equal(p.calls.length, 1);
  assert.equal(p.posts.length, 0);
  assert.notEqual((await p.send(answer))?.cancel, true);
});

test('iter5 direct summarized answers use the same ending cleanup as model replies', async () => {
  for (const tail of ['何か別の確認が要りますか？', 'どの方向で進めますか？']) {
    const p = setup({searchResult: {found: true, answer: `社内の説明です。${tail}`,
      sources: [{url: 'https://example.test/doc'}]}});
    await p.finalize();
    assert.equal(p.posts.length, 1);
    assert.equal(p.posts[0].text, '社内の説明です。\n\n📎 出典\n- <https://example.test/doc>');
    assert.equal((await p.send('モデルの追加'))?.cancel, true);
  }
  const required = '入力が必要です。どの方向で進めますか？';
  const p = setup({searchResult: {status: 'needs_input', answer: required}});
  await p.finalize();
  assert.equal(p.posts[0].text, required);
});

test('iter5 direct summarized answers scrub internal names and signed URLs, and escape Slack notifications', async () => {
  const p = setup({searchResult: {answer:
    'teamagent__search を呼んで、社内の説明です。 <@U87654321> <!here>\nhttps://example.test/report?X-Amz-Signature=private-value'}});
  await p.finalize();
  assert.equal(p.posts.length, 1);
  assert.match(p.posts[0].text, /社内の説明です/u);
  assert.doesNotMatch(p.posts[0].text, /teamagent__|private-value|<@|<!/u);
  assert.match(p.posts[0].text, /&lt;@U87654321&gt; &lt;!here&gt;/u);
});

test('iter5 direct summarized answers validate numeric ratios with the shared grounding logic', async () => {
  const p = setup({searchResult: {answer:
    '社内の説明です。\n動画ごとの保存数は261件と110件で、約1.6倍です。\nどの方向で進めますか？',
    posts: [{save_count: 261}, {save_count: 110}]}});
  await p.finalize();
  assert.equal(p.posts.length, 1);
  assert.match(p.posts[0].text, /社内の説明です/u);
  assert.doesNotMatch(p.posts[0].text, /1\.6倍|進めますか/u);
});

test('iter5 a direct search answer after another revision never receives empty-pass rescue', async () => {
  const p = setup({query: 'この動画を分析して https://www.tiktok.com/@creator/video/7400000000000000001'});
  const revised = await p.finalize();
  assert.equal(revised?.action, 'revise');
  assert.notEqual(revised.retry.idempotencyKey, 'search-zero-tool');
  p.remember(posted('video_metrics'));
  p.end();
  await Promise.all(p.tasks);
  assert.equal(p.posts.length, 0);
  assert.equal((await p.send('空の再パス後のモデル文'))?.cancel, true);
});


test('iter5 empty-pass rescue rechecks direct search delivery immediately before posting', async () => {
  let release;
  const wait = new Promise(resolve => {release = resolve;});
  const p = setup({query: 'この動画を分析して https://www.tiktok.com/@creator/video/7400000000000000001', dmWait: wait});
  assert.equal((await p.finalize())?.action, 'revise');
  p.end();
  p.remember(posted('video_metrics'));
  release();
  await Promise.all(p.tasks);
  assert.equal(p.posts.length, 0);
  assert.equal((await p.send('遅れたモデル文'))?.cancel, true);
});
