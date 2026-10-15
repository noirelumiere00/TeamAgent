import test from 'node:test';
import assert from 'node:assert/strict';
import {createCallerIdentityPlugin} from '../dist/index.js';

function setup(query = '比較して何が効いた？', {postWait, conversation = 'user:U12345678', threadId} = {}) {
  const hooks = new Map();
  const tasks = [];
  const posts = [];
  let started;
  const postStarted = new Promise(resolve => {started = resolve;});
  const ctx = {runId: 'retry-run'};
  createCallerIdentityPlugin({env: {
    TEAMAGENT_CALLER_CLAIM_SECRET: 'x'.repeat(48), SLACK_TEAM_ID: 'T07MU5P2PBR',
    SLACK_BOT_TOKEN: 'test-token',
  }, onBackgroundTask: task => tasks.push(task), fetchFn: async (url, options) => {
    const body = JSON.parse(options.body);
    if (url.endsWith('/chat.postMessage')) {
      posts.push(body);
      started();
      await postWait;
    }
    return {ok: true, text: async () => JSON.stringify({ok: true, channel: {id: 'D12345678'}})};
  }}).register({registerInteractiveHandler() {}, on: (name, fn) => hooks.set(name, fn),
    logger: {info() {}, warn() {}}});
  hooks.get('message_received')({content: query, senderId: 'U12345678',
    messageId: '1792041600.123456', runId: ctx.runId,
    threadId, metadata: {guildId: 'T07MU5P2PBR', to: conversation}},
  {...ctx, channelId: 'slack', sessionKey: 'agent:teamagent:slack:direct:u12345678',
    conversationId: conversation, senderId: 'U12345678', messageId: '1792041600.123456'});
  const finalize = text => hooks.get('before_agent_finalize')({runId: ctx.runId,
    lastAssistantMessage: text}, ctx);
  const send = text => hooks.get('reply_payload_sending')({runId: ctx.runId,
    payload: {text}}, ctx);
  const end = async messages => {
    await hooks.get('agent_end')({runId: ctx.runId, messages, success: true}, ctx);
    await Promise.all(tasks);
  };
  return {hooks, ctx, posts, tasks, finalize, send, end, postStarted};
}

for (const content of [[], [{type: 'text', text: '   '}], [{type: 'text', text: 'NO_REPLY'}],
  [{type: 'thinking', thinking: 'done'}]]) {
  test(`iter3 empty revised pass delivers exactly one fallback: ${JSON.stringify(content)}`, async () => {
    const p = setup();
    const draft = '平均171,325再生で施策Aが勝ちです。';
    assert.equal(p.finalize(draft)?.action, 'revise');
    // Production never calls finalize/reply_payload_sending for an empty second pass.
    const messages = [{role: 'user', content: '比較して何が効いた？'},
      {role: 'assistant', content: [{type: 'text', text: draft}]}, {role: 'assistant', content}];
    await p.end(messages);
    await p.end(messages);
    assert.equal(p.posts.length, 1);
    assert.match(p.posts[0].text, /search.*確認/u);
    assert.ok(!p.posts[0].text.includes('171,325'));
    assert.equal(p.send('遅れて届いた本文')?.cancel, true);
  });
}

test('iter3 retry explicitly overrides no-rerun instruction and requires a visible answer', () => {
  const p = setup();
  const out = p.finalize('施策Aです。');
  assert.match(out.retry.instruction, /明示的にツール実行を要求します/u);
  assert.match(out.retry.instruction, /teamagent__search/u);
  assert.match(out.retry.instruction, /(?:NO_REPLY|空).*禁止/u);
  assert.equal(out.retry.maxAttempts, 1);
});

test('iter3 a second pass with no tools cannot publish the unverified comparison', () => {
  const p = setup();
  p.finalize('施策Aです。');
  assert.equal(p.finalize('181,200再生で勝ちました。'), undefined);
  const out = p.send('181,200再生で勝ちました。');
  assert.match(out?.payload?.text ?? '', /search.*確認/u);
  assert.ok(!out.payload.text.includes('181,200'));
  assert.equal(p.send('さらに168,900再生です。')?.cancel, true);
});

test('iter3 a successful revised search keeps its visible answer without fallback', async () => {
  const p = setup();
  p.finalize('施策Aです。');
  p.hooks.get('after_tool_call')({toolName: 'teamagent__search', result: {text: '平均205,000再生'}}, p.ctx);
  const answer = '資料では平均205,000再生です。';
  // A completed tool call can skip before_agent_finalize in the upstream runtime.
  await p.end([{role: 'assistant', content: [{type: 'text', text: answer}]}]);
  assert.equal(p.posts.length, 0);
  assert.equal(p.send(answer)?.payload?.text ?? answer, answer);
});

test('iter3 retained rejected draft is not treated as the revised final answer', async () => {
  const p = setup();
  const draft = '施策Aが勝ちです。';
  p.finalize(draft);
  await p.end([{role: 'assistant', content: [{type: 'text', text: draft}]}]);
  assert.equal(p.posts.length, 1);
});

test('iter3 search can validate the unchanged draft without another finalize', async () => {
  const p = setup();
  const text = '平均205,000再生です。';
  p.finalize(text);
  p.hooks.get('after_tool_call')({toolName: 'teamagent__search', result: {text}}, p.ctx);
  await p.end([{role: 'toolResult', content: [{type: 'text', text}]},
    {role: 'assistant', content: [{type: 'text', text}]}]);
  assert.equal(p.posts.length, 0);
  assert.equal(p.send(text)?.payload?.text ?? text, text);
});

test('iter3 successful search without a final answer still gets empty-pass rescue', async () => {
  const p = setup();
  const text = '施策Aです。';
  p.finalize(text);
  p.hooks.get('after_tool_call')({toolName: 'teamagent__search', result: {text}}, p.ctx);
  await p.end([{role: 'assistant', content: [{type: 'text', text}]},
    {role: 'toolResult', content: [{type: 'text', text}]}]);
  assert.equal(p.posts.length, 1);
});

test('iter3 progress payload does not replace or consume the final reply', async () => {
  for (const options of [{kind: 'block'}, {payload: {isCommentary: true}},
    {payload: {isReasoning: true}}]) {
    const p = setup();
    p.finalize('施策Aです。');
    const text = '確認しています。';
    const event = {runId: p.ctx.runId, ...options, payload: {text, ...options.payload}};
    const progress = p.hooks.get('reply_payload_sending')(event, p.ctx);
    assert.equal(progress?.payload?.text ?? text, text);
    p.hooks.get('after_tool_call')({toolName: 'teamagent__search', result: {text: '平均205,000再生'}}, p.ctx);
    const answer = '平均205,000再生です。';
    await p.end([{role: 'assistant', content: [{type: 'text', text: answer}]}]);
    assert.equal(p.posts.length, 0);
    assert.equal(p.send(answer)?.payload?.text ?? answer, answer);
  }
});

test('iter3 in-flight direct rescue and a late payload deliver one reply together', async () => {
  let release;
  const postWait = new Promise(resolve => {release = resolve;});
  const p = setup('比較して何が効いた？', {postWait});
  p.finalize('施策Aが勝ちです。');
  const ended = p.end([]);
  await p.postStarted;
  const outgoing = p.send('遅れた未検証の比較です。');
  release();
  await ended;
  assert.equal((await outgoing)?.cancel, true);
  assert.equal(p.posts.length, 1);
});

test('iter3 payload arriving before direct posting takes over without a duplicate post', async () => {
  const p = setup();
  p.finalize('施策Aが勝ちです。');
  p.hooks.get('agent_end')({runId: p.ctx.runId, messages: []}, p.ctx);
  const out = p.send('未確認の比較です。');
  assert.match(out?.payload?.text ?? '', /search.*確認/u);
  await Promise.all(p.tasks);
  assert.equal(p.posts.length, 0);
});

test('iter3 channel rescue replies in the original request thread', async () => {
  for (const threadId of [undefined, '1792041500.654321']) {
    const p = setup('比較して何が効いた？', {conversation: 'C12345678', threadId});
    p.finalize('施策Aです。');
    await p.end([]);
    assert.equal(p.posts.length, 1);
    assert.equal(p.posts[0].channel, 'C12345678');
    assert.equal(p.posts[0].thread_ts, threadId ?? '1792041600.123456');
  }
});

test('iter3 empty payload is rescued once even before agent_end', async () => {
  const p = setup();
  p.finalize('施策Aです。');
  assert.match(p.send('')?.payload?.text ?? '', /search.*確認/u);
  await p.end([]);
  assert.equal(p.posts.length, 0);
});

test('iter3 any revise gets empty-pass rescue while video refusal behavior stays unchanged', async () => {
  for (const query of ['この動画を分析して https://youtu.be/test123', 'こんにちは']) {
    const p = setup(query);
    const draft = query === 'こんにちは' ? 'https://connect.openclaw.ai/oauth/google?user_id=fake' : '動画は取得できません。';
    assert.equal(p.finalize(draft)?.action, 'revise');
    await p.end([{role: 'assistant', content: []}]);
    assert.equal(p.posts.length, 1);
  }
  const p = setup('この動画を分析して https://youtu.be/test123');
  p.finalize('動画は取得できません。');
  const refusal = '動画を取得できませんでした。';
  assert.equal(p.finalize(refusal), undefined);
  await p.end([{role: 'assistant', content: [{type: 'text', text: refusal}]}]);
  assert.equal(p.posts.length, 0);
  assert.equal(p.send(refusal)?.payload?.text ?? refusal, refusal);
});

test('iter3 unrelated empty runs and mismatched run events do not post', async () => {
  const p = setup('こんにちは');
  await p.end([]);
  assert.equal(p.posts.length, 0);
  const other = setup();
  other.finalize('施策Aです。');
  await other.hooks.get('agent_end')({runId: 'other-run', messages: []}, other.ctx);
  await Promise.all(other.tasks);
  assert.equal(other.posts.length, 0);
});

for (const query of ['昨日のあなたの返事ではなく、元の資料を根拠に説明して',
  '資料をもとに説明して', 'ソースを確認して説明して']) {
  test(`iter3 searches before returning source retrieval to user: ${query}`, () => {
    const p = setup(query);
    const out = p.finalize('この会話に添付が無いので、ファイルを添付してください／URLを貼ってください。');
    assert.equal(out?.action, 'revise');
    assert.match(out.retry.instruction, /teamagent__search/u);
    assert.equal(p.finalize('資料のURLを送ってください。'), undefined);
  });
}
test('iter3 truly missing PDF attachment can still request the attachment', () => {
  const p = setup('このPDFを要約して');
  assert.equal(p.finalize('PDFを添付してください。'), undefined);
});

for (const tail of ['詳しく見ますか？', '比較表を作りましょうか？', 'この案はいかがですか？',
  '詳細は必要ですか？', '資料を確認しますか？']) {
  test(`iter3 removes unsolicited ending: ${tail}`, () => {
    const p = setup('こんにちは');
    assert.equal(p.send(`結果をまとめました。${tail}`)?.payload?.text, '結果をまとめました。');
  });
}
