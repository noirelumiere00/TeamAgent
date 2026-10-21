import test from 'node:test';
import assert from 'node:assert/strict';
import {createCallerIdentityPlugin} from '../dist/index.js';

const sheet = 'https://docs.google.com/spreadsheets/d/campaign-actual/edit';
const rows = `📋 伊藤ハム ショート動画実績\n- 動画A：再生507,700、保存127 <${sheet}|施策シート>`;
function setup({direct = true, shared = false, driveFirst = false, documentReceipt = true,
  allowedEmail = 'pilot@example.test', profileEmail = 'pilot@example.test', profileTeam = 'T07MU5P2PBR',
  answer = rows, previousSearch = '前の回答：再生数999,999、保存数888'} = {}) {
  const hooks = new Map(), calls = [], posts = [], tasks = [];
  const conversation = shared ? 'channel:C12345678' : 'user:U12345678';
  const ctx = {runId: 'previous-run', sessionKey: shared
    ? 'agent:teamagent:slack:channel:c12345678' : 'agent:teamagent:slack:direct:u12345678',
    senderId: 'U12345678', channelId: shared ? 'C12345678' : 'U12345678'};
  createCallerIdentityPlugin({env: {
    TEAMAGENT_CALLER_CLAIM_SECRET: 'x'.repeat(48), SLACK_TEAM_ID: 'T07MU5P2PBR',
    TEAMAGENT_MCP_BEARER: 'test-bearer', SLACK_BOT_TOKEN: 'test-token',
    SEARCH_DIRECT_ANSWER: direct ? '1' : '0', SEARCH_DIRECT_ANSWER_ALLOWED_EMAILS: '*',
    SEARCH_DIRECT_ANSWER_KINDS: 'list_fast',
    SEARCH_DRIVE_FIRST: driveFirst ? '1' : '0',
    SEARCH_DRIVE_FIRST_ALLOWED_EMAILS: allowedEmail,
  }, sleepFn: async () => {}, onBackgroundTask: task => tasks.push(task), fetchFn: async (url, options) => {
    const body = JSON.parse(options.body);
    let result;
    if (url.endsWith('/users.info')) result = {ok: true, user: {id: ctx.senderId,
      team_id: profileTeam, profile: {email: profileEmail}}};
    else if (url.endsWith('/conversations.open')) result = {ok: true, channel: {id: 'D12345678'}};
    else if (url.endsWith('/chat.postMessage')) {
      posts.push(body); result = {ok: true};
    } else if (body.method === 'tools/call') {
      calls.push(body.params);
      result = {jsonrpc: '2.0', id: body.id, result: {content: [{type: 'text', text: JSON.stringify({
        answer, found: true, direct_delivery: {answer_kind: 'list_fast', answer,
          ...(driveFirst ? {answer_format: 'list'} : {})},
        video_metrics_evidence: {text: '再生数507,700、保存数127', urls: [sheet]},
      })}]}};
    } else result = {jsonrpc: '2.0', id: body.id, result: {}};
    return {ok: true, text: async () => JSON.stringify(result)};
  }}).register({registerInteractiveHandler() {}, logger: {info() {}, warn() {}},
    on: (name, fn) => hooks.set(name, fn)});
  const receive = (content, messageId) => {
    const target = shared ? conversation : `user:${ctx.senderId}`;
    return hooks.get('message_received')({content, senderId: ctx.senderId, messageId, runId: ctx.runId,
      metadata: {guildId: 'T07MU5P2PBR', to: target}},
    {...ctx, channelId: 'slack', conversationId: target, messageId});
  };
  const call = (tool, id, params = {}) => hooks.get('before_tool_call')({
    toolName: `teamagent__${tool}`, toolCallId: id, runId: ctx.runId, params},
  {...ctx, toolName: `teamagent__${tool}`, toolCallId: id});
  receive('伊藤ハムのハンバーグの施策は今どうなっている？', '1792041600.123456');
  hooks.get('after_tool_call')({toolName: 'teamagent__search', result: driveFirst
    ? {content: [{type: 'text', text: JSON.stringify({document_followup_available: documentReceipt})}]}
    : {text: previousSearch}}, ctx);
  hooks.get('agent_end')({runId: ctx.runId}, ctx);
  ctx.runId = 'list-run';
  const followup = async (query = '一覧で出して', history = []) => {
    receive(query, '1792041601.123456');
    await hooks.get('before_prompt_build')({runId: ctx.runId, messages: [
      {role: 'user', content: '伊藤ハムのハンバーグの施策は今どうなっている？'},
      ...history,
      {role: 'assistant', content: '根拠のない別施策と999,999再生'},
      {role: 'user', content: query},
    ]}, ctx);
  };
  const finalize = (text = '一覧で整理します。📋 伊藤ハム ショート動画実績') =>
    hooks.get('before_agent_finalize')({runId: ctx.runId, lastAssistantMessage: text}, ctx);
  const end = async messages => {
    await hooks.get('agent_end')({runId: ctx.runId, messages, success: true}, ctx);
    await Promise.all(tasks);
  };
  const send = text => hooks.get('reply_payload_sending')({runId: ctx.runId,
    payload: {text}}, ctx);
  return {ctx, hooks, calls, posts, followup, finalize, call, end, send};
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
    assert.equal(p.finalize('動画Bは777,777再生で最も伸びました。')?.action, 'revise');
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

test('shared-channel list searches preserve the requested client instead of importing other participants', async () => {
  const p = setup({direct: false, shared: true});
  await p.followup('一覧で出して', [{role: 'user', content: 'HARIASの資料を確認したい'}]);
  assert.equal(p.finalize('動画Bは777,777再生で最も伸びました。')?.action, 'revise');
  const requested = '伊藤ハムのハンバーグの施策を一覧で';
  const out = p.call('search', 'channel-list', {query: requested});
  assert.ok(out?.params?._user_context?.caller_claim);
  assert.equal(out.params._user_context.channel_id, 'C12345678');
  assert.equal(out.params.query, requested);
  assert.doesNotMatch(out.params.query, /HARIAS/);
});

for (const unrelatedTool of [false, true]) {
  test(`list followup preserves context reply after ${unrelatedTool ? 'one unrelated tool' : 'no tool'} and prior session search`, async () => {
    const p = setup({direct: false});
    await p.followup('一覧で出して');
    if (unrelatedTool) assert.ok(p.call('chitchat', 'empty-retry-unrelated')?.params);
    const original = '先ほど挙げた施策を、投稿のねらい・出演者・構成の順に一覧にします。';
    assert.equal(p.finalize(original), undefined);
    const sent = p.send(original);
    assert.equal(sent?.cancel, undefined);
    assert.equal(sent?.payload?.text ?? original, original);
    await p.end([{role: 'assistant', content: [{type: 'text', text: original}]}]);
    assert.equal(p.posts.length, 0);
    assert.equal(p.calls.length, 0, 'context-only replies need no additional direct search');
  });
}

for (const query of ['一覧で出して', '比較して何が効いた？']) {
  for (const scope of ['same session', 'different session', 'different sender']) {
    test(`session search evidence ${scope} is ${scope === 'same session' ? 'reusable' : 'isolated'} for ${query}`, async () => {
      const p = setup({direct: false});
      if (scope === 'different session') p.ctx.sessionKey = 'agent:other:slack:direct:u12345678';
      if (scope === 'different sender') {
        p.ctx.senderId = 'U99999999';
        p.ctx.channelId = 'U99999999';
        p.ctx.sessionKey = 'agent:teamagent:slack:direct:u99999999';
      }
      await p.followup(query);
      const known = '施策Aは再生数999,999、保存数888です。';
      const result = p.finalize(known);
      if (scope === 'same session') {
        assert.equal(result, undefined);
        const sent = p.send(known);
        assert.equal(sent?.cancel, undefined);
        assert.equal(sent?.payload?.text ?? known, known);
      } else {
        assert.equal(result?.action, 'revise');
      }
    });
  }
  test(`session search evidence requires a new search for unknown numbers in ${query}`, async () => {
    const p = setup({direct: false});
    await p.followup(query);
    const original = '施策Bは再生数777,777、保存数555で最も伸びました。';
    assert.equal(p.finalize(original)?.action, 'revise');
    const emptyRetry = [{role: 'assistant', content: [{type: 'text', text: original}]},
      {role: 'assistant', content: []}];
    await p.end(emptyRetry);
    await p.end(emptyRetry);
    assert.equal(p.posts.length, 1);
    assert.equal(p.posts[0].text, original, 'an empty retry preserves the original reply');
    assert.equal(p.send(original)?.cancel, true);
  });
  test(`session search evidence requires a new search for unsupported comparison in ${query}`, async () => {
    const p = setup({direct: false});
    await p.followup(query);
    const original = '施策Bが勝ちで、冒頭の訴求が効いたと考えられます。';
    assert.equal(p.finalize(original)?.action, 'revise');
    await p.end([{role: 'assistant', content: [{type: 'text', text: original}]},
      {role: 'assistant', content: []}]);
    assert.equal(p.posts.length, 1);
    assert.equal(p.posts[0].text, original);
  });
}

for (const query of ['一覧で出して', '比較して何が効いた？']) {
  test(`direct ON reuses known session comparison without another search for ${query}`, async () => {
    const original = '施策Aが勝ちで、冒頭の商品訴求が効いた。';
    const p = setup({previousSearch: original});
    await p.followup(query);
    assert.equal(await p.finalize(original), undefined);
    assert.equal(p.calls.length, 0);
    assert.equal((await p.send(original))?.payload?.text ?? original, original);
  });
  test(`direct ON reuses known session search numbers without another search for ${query}`, async () => {
    const p = setup();
    await p.followup(query);
    const original = '施策Aは再生数999,999、保存数888です。';
    assert.equal(await p.finalize(original), undefined);
    assert.equal(p.calls.length, 0);
    assert.equal(p.posts.length, 0);
    const sent = await p.send(original);
    assert.equal(sent?.cancel, undefined);
    assert.equal(sent?.payload?.text ?? original, original);
  });
  for (const known of [true, false]) {
    test(`same-run successful search ${known ? 'supports known numbers' : 'does not retry unknown numbers'} for ${query}`, async () => {
      const p = setup({direct: false});
      await p.followup(query);
      p.hooks.get('after_tool_call')({toolName: 'teamagent__search', result: {
        text: '施策Aは再生数205,000、保存数127です。',
      }}, p.ctx);
      const original = known ? '施策Aは再生数205,000、保存数127です。'
        : '施策Bは再生数777,777、保存数555です。';
      const result = p.finalize(original);
      assert.equal(result, undefined, 'a tool result in this run must not trigger another search retry');
      if (known) {
        assert.equal(p.send(original)?.payload?.text ?? original, original);
      }
    });
  }
  for (const known of [true, false]) {
    test(`same-run successful search ${known ? 'supports known comparison' : 'does not retry unsupported comparison'} for ${query}`, async () => {
      const p = setup({direct: false});
      await p.followup(query);
      p.hooks.get('after_tool_call')({toolName: 'teamagent__search', result: {
        text: '施策Aが勝ちで、冒頭の訴求が効いた。',
      }}, p.ctx);
      const original = known ? '施策Aが勝ちで、冒頭の訴求が効いた。'
        : '施策Bが勝ちで、締めの訴求が効いた。';
      const result = p.finalize(original);
      assert.equal(result, undefined, 'a tool result in this run must not trigger another search retry');
      if (known) {
        assert.equal(p.send(original)?.payload?.text ?? original, original);
      }
    });
  }
}

for (const toolName of ['teamagent__chitchat', 'teamagent__arbitrary_lookup']) {
  test(`same-run ${toolName} result avoids another retry and preserves current numeric reply`, async () => {
    const p = setup({direct: false});
    await p.followup('一覧で出して');
    const original = '動画Bは再生数777,777、保存数555です。';
    p.hooks.get('after_tool_call')({toolName, result: {text: original}}, p.ctx);
    assert.equal(p.finalize(original), undefined);
    const sent = p.send(original);
    assert.notEqual(sent?.cancel, true);
    assert.equal(sent?.payload?.text ?? original, original);
    assert.equal(p.calls.length, 0, 'a completed tool result must not start another direct search');
  });
}

test('a contextual video list retains its known metrics through the outbound grounder', async () => {
  const p = setup({direct: false});
  await p.followup('一覧で出して');
  const original = '動画別の実績\n- 動画A：再生数999,999、保存数888';
  assert.equal(p.finalize(original), undefined);
  assert.equal(p.send(original)?.payload?.text ?? original, original);
});

for (const {name, source, draft} of [
  {name: '方が高い', source: '施策Aと施策Bの優劣は未確認です。', draft: '施策Aの方が高い。'},
  {name: 'が上でした', source: '施策Aと施策Bの優劣は未確認です。', draft: '施策Aが上でした。'},
  {name: '優位', source: '施策Aと施策Bの優劣は未確認です。', draft: '施策Aは優位です。'},
  {name: 'winner reversed with identical numbers',
    source: '施策Aは10再生、施策Bは5再生で施策Aが勝ち。',
    draft: '施策Aは10再生、施策Bは5再生で施策Bが勝ち。'},
  {name: 'source denies a known winner', source: '施策Aが勝ちとは判断できません。', draft: '施策Aが勝ちです。'},
  {name: 'winner assertion followed by unknown reason', source: '施策Aと施策Bの優劣は未確認です。',
    draft: '施策Aが勝ちですが、理由は未確認です。'},
  {name: 'one denied comparison cannot excuse another assertion', source: '施策Aと施策Bの優劣は未確認です。',
    draft: '同率ではありませんが、施策Aが上回ります。'},
  {name: 'known winner with unsupported effectiveness reason', source: '施策Aが勝ちです。理由は未確認です。',
    draft: '施策Aが勝ちです。冒頭の訴求が効いたと判断できます。'},
]) {
  test(`prior-session comparison search evidence rejects unsupported claim: ${name}`, async () => {
    const p = setup({direct: false, previousSearch: source});
    await p.followup('比較して何が効いた？');
    assert.equal(p.finalize(draft)?.action, 'revise');
  });
}

test('comparison answer can preserve an explicit inability to identify a winner', async () => {
  const p = setup({direct: false});
  await p.followup('比較して何が効いた？');
  const original = '施策Aが勝ちとは判断できません。';
  assert.equal(p.finalize(original), undefined);
  assert.equal(p.send(original)?.payload?.text ?? original, original);
});
test('document followup sends only the trusted followup text instead of choosing another document', async () => {
  const p = setup({direct: false, driveFirst: true});
  await p.followup('その資料も出して');
  assert.equal(p.finalize()?.action, 'revise');
  const out = p.call('search', 'document-followup', {query: '別案件の資料'});
  assert.equal(out.params.query, 'その資料も出して');
});

test('document followup guard stays off with the new policy disabled', async () => {
  const p = setup({direct: false});
  await p.followup('その資料も出して');
  assert.equal(p.finalize(), undefined);
  const out = p.call('search', 'document-followup-off', {query: 'その資料も出して'});
  assert.equal(out.params.query, 'その資料も出して');
});

test('document followup recovers for its allowed requester without a process-local receipt', async () => {
  const p = setup({direct: false, driveFirst: true, documentReceipt: false});
  await p.followup('その資料も出して');
  assert.equal(p.finalize()?.action, 'revise');
  const out = p.call('search', 'document-followup-after-restart', {query: '別の資料'});
  assert.equal(out.params.query, 'その資料も出して');
});

test('document followup direct search does not append earlier user history', async () => {
  const p = setup({driveFirst: true});
  await p.followup('その資料も出して');
  await p.finalize();
  assert.equal(p.calls[0].arguments.query, 'その資料も出して');
});

for (const denied of [
  {allowedEmail: ''}, {profileEmail: 'outside@example.test'}, {profileTeam: 'T99999999'},
]) {
  test(`document followup does not change behavior for an unverified pilot: ${JSON.stringify(denied)}`, async () => {
    const p = setup({direct: false, driveFirst: true, documentReceipt: false, ...denied});
    await p.followup('その資料も出して');
    assert.equal(p.finalize(), undefined);
    assert.equal(p.calls.length, 0);
  });
}

for (const [url, label, note] of [
  ['https://drive.google.com/file/d/ORIGINAL/view', '資料を見る', '資料の説明です。'],
  ['https://example.slack.com/archives/C12345678/p1792041600123456', '会話の記録',
    'Slackにだけある資料（本文未読）'],
]) {
  test(`direct case format keeps Slack named links and single-star titles: ${label}`, async () => {
    const answer = `*対象案件*（2025年9月）\n• ${note}\n• <${url}|${label}>`;
    const p = setup({driveFirst: true, documentReceipt: false, answer});
    await p.followup('その資料も出して');
    await p.finalize();
    assert.equal(p.posts.length, 1);
    assert.equal(p.posts[0].text, answer);
    assert.doesNotMatch(p.posts[0].text, /&lt;|\*\*対象案件\*\*/u);
  });
}
