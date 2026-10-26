import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
import {createHmac} from 'node:crypto';
import {createCallerIdentityPlugin, classifyInboundRoute} from '../dist/index.js';

const scenarios = JSON.parse(readFileSync(new URL('./fixtures/router-scenarios.json', import.meta.url)));
const wrap = data => ({content: [{type: 'text', text: JSON.stringify(data)}]});
const reply = data => ({ok: true, text: async () => JSON.stringify(data)});
const answer = '取得した資料では施策は実施済みです。';
const result = {found: true, answer, answer_format: 'compact', displayed_refs: [],
  direct_delivery: {answer_kind: 'list_fast', answer, answer_format: 'compact'}};
const deferred = () => { let resolve; const promise = new Promise(r => {resolve = r;}); return {promise, resolve}; };
function setup({query = '社内でいうTTOとは？', mode = 'enforce', email = 'owner@example.test',
  profile = {}, allowlist = 'owner@example.test', conversation = 'D11111111', search = result,
  searchWait, profileWait, searchFails = false, searchTimeout = false, env = {}, now} = {}) {
  const hooks = new Map(), calls = [], logs = [], posts = [], tasks = [], registrations = [];
  const ctx = {runId: 'router-run', sessionKey: 'agent:teamagent:slack:direct:u11111111',
    senderId: 'U11111111', channelId: conversation};
  const options = {env: {TEAMAGENT_CALLER_CLAIM_SECRET: 'x'.repeat(48), SLACK_TEAM_ID: 'T11111111',
    TEAMAGENT_MCP_BEARER: 'fixture-bearer', SLACK_BOT_TOKEN: 'fixture-token',
    TEAMAGENT_INBOUND_ROUTER: mode, SEARCH_DIRECT_ANSWER: '1',
    SEARCH_DIRECT_ANSWER_ALLOWED_EMAILS: allowlist,
    SEARCH_DIRECT_ANSWER_KINDS: 'not_found,comparison,list_fast,video_metrics', ...env},
    ...(now ? {now} : {}), ...(searchTimeout ? {buttonTimeoutMs: 5} : {}), sleepFn: async () => {}, onBackgroundTask: t => tasks.push(t),
    fetchFn: async (url, options) => {
      const body = JSON.parse(options.body);
      if (url.endsWith('/users.info')) { await profileWait; return reply({ok: true,
        user: {id: 'U11111111', team_id: 'T11111111', profile: {email}, ...profile}}); }
      if (url.endsWith('/conversations.open')) return reply({ok: true, channel: {id: 'D11111111'}});
      if (url.endsWith('/chat.postMessage')) { posts.push(body); return reply({ok: true}); }
      if (body.method === 'tools/call') {
        calls.push(body.params);
        if (searchTimeout) await new Promise((_, reject) => {
          const guard = setTimeout(() => reject(new Error('abort signal was ignored')), 100);
          options.signal.addEventListener('abort', () => {clearTimeout(guard); reject(options.signal.reason);}, {once: true});
        });
        await searchWait;
        if (searchFails) throw new Error('fixture unavailable');
        return reply({jsonrpc: '2.0', id: body.id, result: wrap(search)});
      }
      return reply({jsonrpc: '2.0', id: body.id, result: {}});
    }};
  createCallerIdentityPlugin(options).register({registerInteractiveHandler() {},
    on: (name, fn, options) => {hooks.set(name, fn); registrations.push({name, options});},
    logger: {info: line => logs.push(line), warn: line => logs.push(line)}});
  const inbound = (hook = 'message_received', content = query, runId = ctx.runId, extra = {}) => hooks.get(hook)({
    content, senderId: 'U11111111', messageId: '1792041600.111111', runId,
    metadata: {guildId: 'T11111111', to: conversation}, ...extra}, {...ctx, runId,
    channelId: 'slack', conversationId: conversation, messageId: extra.messageId ?? '1792041600.111111'});
  inbound();
  const prompt = (messages = [], c = ctx) => hooks.get('before_prompt_build')({messages}, c);
  const send = (text = '検索前の下書き。999件です。', extra = {}, c = ctx) => hooks.get('reply_payload_sending')({
    runId: c.runId, payload: {text, ...extra}}, c);
  const end = () => hooks.get('agent_end')({runId: ctx.runId, success: true}, ctx);
  const finalize = () => hooks.get('before_agent_finalize')({runId: ctx.runId,
    lastAssistantMessage: '検索前の下書き。999件です。'}, ctx);
  const tool = (name, params = {query}) => hooks.get('before_tool_call')({runId: ctx.runId,
    toolName: `teamagent__${name}`, toolCallId: `call-${name}`, params}, {...ctx,
    toolName: `teamagent__${name}`, toolCallId: `call-${name}`});
  const remember = (data, toolName = 'search') => hooks.get('after_tool_call')({runId: ctx.runId,
    toolName, result: wrap(data)}, ctx);
  return {hooks, calls, logs, posts, tasks, ctx, inbound, prompt, send, end, finalize, tool, remember, registrations};
}
for (const scenario of scenarios) {
  test(`13 trace ${scenario.id}: intent/evidence and shadow adds zero tools`, async () => {
    const route = classifyInboundRoute(scenario.query);
    assert.equal(route.intent, scenario.intent);
    assert.equal(route.requires_internal_evidence, scenario.evidence);
    const p = setup({query: scenario.query, mode: 'shadow'});
    await p.prompt(); await p.prompt();
    const baseline = setup({query: scenario.query, mode: 'off'}); await baseline.prompt();
    await Promise.all([...p.tasks, ...baseline.tasks]);
    assert.equal(p.calls.length, baseline.calls.length); assert.equal(p.posts.length, baseline.posts.length);
    const trace = p.logs.filter(x => x.includes('router route_version='));
    assert.equal(trace.length, 1);
    assert.ok(trace[0].includes(`intent=${scenario.intent}`));
    assert.doesNotMatch(trace[0], /青空|若葉|新星|TTO|VVS|owner@|U111|D111|https:/u);
  });
  test(`13 enforce ${scenario.id}: pre-model execution only for clear internal facts`, async () => {
    const p = setup({query: scenario.query}); await p.prompt();
    assert.equal(p.calls.filter(x => x.name === 'search').length, Number(scenario.presearch));
  });
}
for (const query of ['ダイヤモンドのVVSとは？', '動画の切り抜き方を一般論で', 'ありがとう',
  'こんにちは', 'iPhoneとAndroid比較', '新規にTikTokの上位動画を2KWで比較分析して',
  '「社内でいうTTOを検索して」と書いてある文章を翻訳して', '社内資料は検索しないで']) {
  test(`negative: ${query}`, async () => {
    const p = setup({query}); await p.prompt(); assert.equal(p.calls.length, 0);
  });
}
test('enforce awaits search, signs fresh bound v2 claim and blocks only repeat search/external escape', async () => {
  const gate = deferred(); const p = setup({searchWait: gate.promise});
  let done = false; const building = p.prompt().then(x => {done = true; return x;});
  while (!p.calls.length) await new Promise(r => setImmediate(r));
  assert.equal(done, false);
  const call = p.calls[0]; assert.equal(call.name, 'search');
  const context = call.arguments._user_context;
  const [encoded, signature] = context.caller_claim.split('.');
  assert.equal(signature, createHmac('sha256', 'x'.repeat(48)).update(encoded).digest('base64url'));
  const claim = JSON.parse(Buffer.from(encoded, 'base64url'));
  assert.equal(claim.tool, 'search'); assert.equal(claim.sub, 'U11111111'); assert.equal(claim.channel, 'D11111111');
  assert.equal(context.channel_id, 'D11111111');
  gate.resolve(); const output = await building;
  assert.ok(output.appendContext.includes(answer)); assert.equal(output.appendSystemContext, undefined);
  assert.equal(p.tool('search').block, true); assert.equal(p.tool('web_research').block, true);
  assert.notEqual(p.tool('calendar_freebusy').block, true);
});
test('both ingress hooks, concurrent prompt/finalize, early send and agent_end search once', async () => {
  const gate = deferred(); const p = setup({searchWait: gate.promise});
  p.inbound('inbound_claim'); const building = p.prompt(); const duplicate = p.prompt();
  while (!p.calls.length) await new Promise(r => setImmediate(r));
  const finalizing = p.finalize(); const sending = p.send(); p.end();
  gate.resolve(); await Promise.all([building, duplicate, finalizing]);
  assert.equal((await sending).payload.text, answer);
  assert.equal((await p.send()).cancel, true); assert.equal(p.calls.length, 1);
});
test('same receipt in another run reuses result; another message starts a fresh search', async () => {
  const p = setup(); await p.prompt(); await p.send(); p.end();
  const ctx = {...p.ctx, runId: 'replay-run'}; p.inbound('message_received', undefined, ctx.runId);
  await p.prompt([], ctx); assert.equal(p.calls.length, 1); assert.equal((await p.send('', {}, ctx)).cancel, true);
  p.inbound('message_received', undefined, 'new-run', {messageId: '1792041600.222222'});
  await p.prompt([], {...ctx, runId: 'new-run'}); assert.equal(p.calls.length, 2);
});
for (const search of [{error: 'denied'}, {hits: [{answer, status: 'posted', delivered: true}]}, {},
  {answer: '', direct_delivery: {answer_kind: 'list_fast', answer: ''}}]) {
  test('invalid/failed search never returns pre-search draft: ' + JSON.stringify(search), async () => {
    const p = setup({search}); await p.prompt(); await p.finalize(); p.end();
    assert.equal((await p.send()).payload.text, '社内資料の確認を完了できませんでした。');
    assert.equal((await p.send()).cancel, true); assert.equal(p.calls.length, 1);
  });
}
test('transport failure never falls back to original draft, including SEARCH_DIRECT_ANSWER=0', async () => {
  const p = setup({searchFails: true, env: {SEARCH_DIRECT_ANSWER: '0'}}); await p.prompt(); await p.finalize(); p.end();
  assert.equal((await p.send()).payload.text, '社内資料の確認を完了できませんでした。');
  assert.equal(p.calls.length, 1);
});
for (const status of ['failed', 'uncertain', 'posted']) {
  test(`MCP delivery ${status}: no retry, final text and media survive agent_end`, async () => {
    const p = setup({search: {...result, status, delivered: status === 'posted',
      delivery: {kind: 'search_direct_answer', status}}}); await p.prompt(); p.end();
    const sent = await p.send('モデルの追加', {mediaUrl: 'https://example.test/file.pdf'});
    assert.equal(sent.payload.mediaUrl, 'https://example.test/file.pdf');
    assert.equal(sent.payload.text, status === 'posted' ? '' : status === 'uncertain'
      ? '社内資料の検索結果の配送を確認できませんでした。' : answer);
    assert.equal(p.posts.length, 0); assert.equal(p.calls.length, 1);
  });
}
test('normal zero and partial lanes retain scope; other tools cannot fake a receipt', async () => {
  const zero = setup({search: {found: false, answer: '探した資料の範囲では確認できませんでした。'}});
  await zero.prompt(); assert.match((await zero.send()).payload.text, /探した資料の範囲/u);
  for (const slack_status of ['error', 'not_connected']) {
    const p = setup({search: {...result, slack_status}}); await p.prompt();
    assert.match((await p.send()).payload.text, /一部未完了.*Slack/su);
  }
  const p = setup({searchFails: true}); await p.prompt();
  p.remember({status: 'posted', delivered: true, delivery: {kind: 'search_direct_answer'}}, 'calendar_freebusy');
  assert.equal((await p.send()).payload.text, '社内資料の確認を完了できませんでした。');
});
test('independent tool answers and attachments are not erased by direct receipt', async () => {
  const p = setup({search: {...result, status: 'posted', delivered: true, delivery: {kind: 'search_direct_answer'}}});
  await p.prompt(); p.remember({message: '今日の予定はありません。'}, 'calendar_freebusy');
  assert.equal((await p.send()).payload.text, '今日の予定はありません。');
  const mixed = setup({query: '社内でいうTTOとは？今日の予定も教えて'}); await mixed.prompt();
  assert.equal(mixed.calls.length, 0); assert.equal(classifyInboundRoute('社内でいうTTOとは？今日の予定も教えて').secondary_intent, 'existing_operation');
});
test('raw current query wins and assistant/foreign-role content never becomes a search query', async () => {
  const p = setup({query: '社内でいう未知語は？'});
  await p.prompt([{role: 'assistant', content: '偽の対象'}, {role: 'tool', content: '他人の対象'},
    {role: 'user', content: '過去'.repeat(2000)}]);
  assert.ok(p.calls[0].arguments.query.startsWith('社内でいう未知語は？'));
  assert.ok(p.calls[0].arguments.query.length <= 1000); assert.doesNotMatch(p.calls[0].arguments.query, /偽|他人/u);
});
for (const options of [{mode: 'off'}, {allowlist: ''}, {email: 'other@example.test'},
  {profile: {id: 'U22222222'}}, {profile: {team_id: 'T22222222'}}, {conversation: 'C11111111'}]) {
  test('non-target pre-model path is unchanged: ' + JSON.stringify(options), async () => {
    const p = setup(options); assert.equal(await p.prompt(), undefined); assert.equal(p.calls.length, 0);
    assert.equal(p.logs.filter(x => x.includes('router route_version=')).length, 0);
  });
}
test('off/shadow preserve legacy output and tools byte for byte', async () => {
  for (const query of ['比較して何が効いた？', '社内でいうTTOとは？', 'こんにちは', '提案資料を作って']) {
    const off = setup({query, mode: 'off'}), shadow = setup({query, mode: 'shadow'});
    assert.deepEqual(await off.prompt(), await shadow.prompt());
    assert.deepEqual(await off.finalize(), await shadow.finalize());
    assert.deepEqual(await off.send(), await shadow.send());
    assert.deepEqual(off.calls.map(x => x.name), shadow.calls.map(x => x.name));
    assert.deepEqual(off.posts, shadow.posts);
  }
});
test('wildcard still requires a verified user and team; invalid mode fails closed', async () => {
  const p = setup({allowlist: '*', profile: {id: 'U22222222'}}); await p.prompt(); assert.equal(p.calls.length, 0);
  assert.throws(() => setup({mode: 'enabled'}), /invalid TEAMAGENT_INBOUND_ROUTER/u);
});

// Execute unmodified functions from the pinned upstream package, not a hook-runner mock.
test('pinned upstream awaits the actual plugin through its real runner and prompt builder',
  {skip: !process.env.OPENCLAW_DIST_DIR}, async () => {
    const dist = process.env.OPENCLAW_DIST_DIR;
    const lock = JSON.parse(readFileSync(new URL('../../plugins-lock.json', import.meta.url)));
    const pkg = JSON.parse(readFileSync(join(dist, '../package.json')));
    assert.equal(pkg.version, lock.openclaw.version);
    const runnerSource = readFileSync(join(dist, 'hook-runner-global-Cucx8m-W.js'), 'utf8');
    const promptSource = readFileSync(join(dist, 'attempt.prompt-helpers-Bx-dobiv.js'), 'utf8');
    const selection = readFileSync(join(dist, 'selection-8ixiqbew.js'), 'utf8');
    assert.match(selection, /const hookResult = isRawModelRun \? void 0 : await resolvePromptBuildHookResult\(/u);
    assert.ok(selection.indexOf('await resolvePromptBuildHookResult(') < selection.indexOf('await promptActiveSession(promptForSession'));
    const numberHelpers = await import(pathToFileURL(join(dist, 'number-coercion-CJQ8TR--.js')));
    const textHelpers = runnerSource.slice(runnerSource.indexOf('function concatOptionalTextSegments('),
      runnerSource.indexOf('//#region src/plugins/hook-decision-types.ts'));
    const runnerBody = runnerSource.slice(runnerSource.indexOf('const DEFAULT_VOID_HOOK_TIMEOUT_MS_BY_HOOK'),
      runnerSource.indexOf('//#region src/plugins/hook-runner-global.ts'));
    const createRunner = new Function('clampPositiveTimerTimeoutMs', 'resolveTimerTimeoutMs',
      textHelpers + runnerBody + '\nreturn createHookRunner;')(numberHelpers.d, numberHelpers.j);
    const buildBody = promptSource.slice(promptSource.indexOf('async function resolvePromptBuildHookResult('),
      promptSource.indexOf('function resolvePromptModeForSession('));
    const runtimeSource = readFileSync(join(dist, 'runtime-pr_AayQr.js'), 'utf8');
    const injectionHelpers = runtimeSource.slice(runtimeSource.indexOf('function normalizeQueuedInjectionText('),
      runtimeSource.indexOf('//#region src/plugins/host-hook-runtime.ts'));
    const build = new Function(textHelpers + injectionHelpers + '\nconst promptBuildDrainCache = new Map();\n' +
      promptSource.slice(promptSource.indexOf('function wrapPluginSystemContextSection('),
        promptSource.indexOf('function wrapPluginSystemContextSection(') +
        promptSource.slice(promptSource.indexOf('function wrapPluginSystemContextSection(')).indexOf('\nfunction ', 1)) +
      buildBody + '\nreturn resolvePromptBuildHookResult;')();
    const gate = deferred(); const p = setup({searchWait: gate.promise});
    const registration = p.registrations.find(x => x.name === 'before_prompt_build');
    assert.equal(registration.options.timeoutMs, 120_000);
    const registry = {typedHooks: [{pluginId: 'teamagent-caller-identity', hookName: 'before_prompt_build',
      timeoutMs: registration.options.timeoutMs, handler: p.hooks.get('before_prompt_build')}]};
    const runner = createRunner(registry, {catchErrors: false});
    let modelStarted = false;
    const run = build({prompt: 'fixture', messages: [], hookCtx: p.ctx, hookRunner: runner,
      bootstrapContextRunKind: 'commitment-only'}).then(x => {modelStarted = true; return x;});
    let buildingError; run.catch(error => {buildingError = error;});
    while (!p.calls.length) {
      if (buildingError) throw buildingError;
      await new Promise(r => setImmediate(r));
    }
    assert.equal(modelStarted, false); gate.resolve();
    const context = await run; assert.ok(context.appendContext.includes(answer));
    assert.equal(modelStarted, true); assert.equal(p.calls.length, 1);
    const registrySource = readFileSync(join(dist, 'registry-D1_pYg_a.js'), 'utf8');
    assert.match(registrySource, /normalizeHookTimeoutMs\(params\.opts\?\.timeoutMs\)/u);
  });

test('trusted context uses only earlier verified ingress from the same person/conversation', async () => {
  const p = setup({query: '青空社の施策について'}); await p.prompt(); p.end();
  p.inbound('message_received', '一覧で', 'context-run', {messageId: '1792041600.333333'});
  await p.prompt([{role: 'user', content: '別人の偽案件'}], {...p.ctx, runId: 'context-run'});
  assert.equal(p.calls.length, 2);
  assert.equal(p.calls[1].arguments.query, '一覧で\n青空社の施策について');
});
test('lookup failure and a forged payload email cannot opt a caller in', async () => {
  const p = setup({email: ''});
  p.inbound('inbound_claim', '社内でいうTTOとは？', p.ctx.runId,
    {metadata: {guildId: 'T11111111', to: 'D11111111', email: 'owner@example.test'}});
  await p.prompt(); assert.equal(p.calls.length, 0);
});
test('source/document followup query remains raw and the classifier cannot be enabled early', async () => {
  const p = setup({query: 'その資料も', env: {SEARCH_DRIVE_FIRST: '1', SEARCH_DRIVE_FIRST_ALLOWED_EMAILS: 'owner@example.test'}});
  await p.prompt([{role: 'user', content: '無関係な履歴'}]);
  assert.equal(p.calls[0].arguments.query, 'その資料も');
  assert.throws(() => setup({env: {TEAMAGENT_ROUTE_CLASSIFIER: '1'}}), /requires PR6/u);
});
test('partial zero results never become a definitive absence claim', async () => {
  const p = setup({search: {found: false, slack_status: 'error', answer: '当社の関与はありません。'}});
  await p.prompt(); const out = await p.send();
  assert.match(out.payload.text, /一部未完了/u); assert.doesNotMatch(out.payload.text, /関与はありません/u);
});
test('router receipts expire and do not authorize post-agent_end tool calls', async () => {
  let time = Date.now(); const p = setup({now: () => time}); await p.prompt(); p.end();
  assert.equal(p.tool('calendar_freebusy').block, true);
  time += 11 * 60 * 1000;
  assert.notEqual((await p.send())?.cancel, true);
});

test('caller claim is minted after identity lookup, with the original v2 TTL', async () => {
  const gate = deferred(); let time = Date.now();
  const p = setup({profileWait: gate.promise, now: () => time});
  const building = p.prompt(); time += 90_000; gate.resolve(); await building;
  const claim = JSON.parse(Buffer.from(p.calls[0].arguments._user_context.caller_claim.split('.')[0], 'base64url'));
  assert.equal(claim.v, 2); assert.equal(claim.iat, Math.floor(time / 1000));
  assert.equal(claim.exp - claim.iat, 60);
});
test('agent_end during identity lookup cannot create a late signed invocation', async () => {
  const gate = deferred(); const p = setup({profileWait: gate.promise});
  const building = p.prompt(); p.end(); gate.resolve(); await building;
  assert.equal(p.calls.length, 0);
  assert.equal((await p.send()).payload.text, '社内資料の確認を完了できませんでした。');
});
test('enforced fixed responses use shared scrubbing and escape notification syntax', async () => {
  const p = setup({search: {...result, answer: 'teamagent__search 社内の資料です。 <@U22222222> <!here>',
    direct_delivery: {answer_kind: 'list_fast', answer: 'teamagent__search 社内の資料です。 <@U22222222> <!here>'}}});
  await p.prompt(); const out = await p.send();
  assert.match(out.payload.text, /社内の資料です/u); assert.doesNotMatch(out.payload.text, /teamagent__|<@|<!/u);
});

test('the real MCP deadline ends a pending search without retry or draft fallback', async () => {
  const p = setup({searchTimeout: true}); await p.prompt();
  assert.equal(p.calls.length, 1);
  assert.ok(p.logs.some(line => line.includes('outcome=failed reason=timeout')));
  assert.equal((await p.send()).payload.text, '社内資料の確認を完了できませんでした。');
});

for (const status of ['failed', 'uncertain', 'posted']) {
  test(`PR3 pre-model case_cards receipt ${status} preserves cards and delivery boundaries`, async () => {
    const url = 'https://drive.google.com/file/d/CASE/view';
    const cards = `*匿名案件*（2026年9月）\n• 紹介動画を配信。\n• 結果を確認。\n• <${url}|資料を見る>`;
    const p = setup({query: '事例出して', env: {SEARCH_DIRECT_ANSWER_KINDS: 'case_cards'}, search: {
      found: true, answer: cards, answer_format: 'list', displayed_refs: [{kind: 'drive', url}],
      direct_delivery: {answer_kind: 'case_cards', answer: cards, answer_format: 'list'},
      status, delivered: status === 'posted', delivery: {kind: 'search_direct_answer', answer_kind: 'case_cards', status},
    }});
    await p.prompt(); await p.prompt(); p.end();
    const sent = await p.send('モデルの追加', {mediaUrl: 'https://example.test/file.pdf'});
    assert.equal(sent.payload.text, status === 'posted' ? '' : status === 'uncertain'
      ? '社内資料の検索結果の配送を確認できませんでした。' : cards);
    assert.equal(sent.payload.mediaUrl, 'https://example.test/file.pdf');
    assert.equal(p.calls.length, 1); assert.equal(p.posts.length, 0);
  });
}

for (const tail of ['資料が必要でしたら、お知らせください。', '詳しい内容をご覧になりたい場合は、お知らせください。', '何か他に知りたいことはありますか？']) {
  test(`eval12 enforced reply uses final drive guard without OC drive env: ${tail}`, async () => {
    const p = setup({search: {found: true, answer: '配信サービスです。' + tail, displayed_refs: []}});
    await p.prompt();
    const sent = await p.send();
    assert.match(sent.payload.text, /配信サービスです/u);
    assert.doesNotMatch(sent.payload.text, /お知らせ|ありますか/u);
  });
}
