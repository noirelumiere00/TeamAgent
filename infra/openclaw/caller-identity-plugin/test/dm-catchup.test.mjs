import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp, readFile, readdir, rm, stat, symlink, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import * as plugin from '../dist/index.js';

const TEAM = 'T07MU5P2PBR', USER = 'U12345678', BOT = 'U87654321';
const CHANNEL = 'D12345678', NOW = Date.parse('2026-10-19T03:00:00Z');
const TOKEN = 'test-bot-token-do-not-log';
const ts = secondsAgo => `${Math.floor(NOW / 1000) - secondsAgo}.000001`;
const message = (secondsAgo = 60, text = '入れ替え中の質問です', extra = {}) => ({
  type: 'message', channel: CHANNEL, user: USER, ts: ts(secondsAgo), text, ...extra,
});
const reply = (source, secondsAgo = 30, extra = {}) => ({
  type: 'message', channel: CHANNEL, user: BOT, bot_id: 'B12345678',
  app_id: 'A12345678', ts: ts(secondsAgo), text: '回答です',
  metadata: {event_type: 'teamagent.dm_answer_v1', event_payload: {
    source_channel: source.channel, source_ts: source.ts, complete: true,
  }}, ...extra,
});
const response = body => ({ok: true, status: 200, headers: {get: () => null},
  json: async () => body, text: async () => JSON.stringify(body)});

function requestParams(url, options = {}) {
  const parsed = new URL(url), params = Object.fromEntries(parsed.searchParams);
  if (options.body) {
    try { Object.assign(params, JSON.parse(String(options.body))); }
    catch { Object.assign(params, Object.fromEntries(new URLSearchParams(String(options.body)))); }
  }
  return {method: parsed.pathname.split('/').at(-1), params};
}

async function setup(t, {statePath, history = [], channels, historyError,
  hasMore = false, historyWait, dispatch, authTeam = TEAM, allowFrom, now = () => NOW} = {}) {
  assert.equal(typeof plugin.createDmCatchup, 'function', 'createDmCatchup export is required');
  if (!statePath) {
    const dir = await mkdtemp(join(tmpdir(), 'teamagent-dm-catchup-'));
    t.after(() => rm(dir, {recursive: true, force: true}));
    statePath = join(dir, 'journal.json');
  }
  const calls = [], dispatched = [], logs = [];
  let historyStarted;
  const historyReached = new Promise(resolve => {historyStarted = resolve;});
  const fetchFn = async (url, options) => {
    const call = requestParams(url, options);
    calls.push(call);
    if (call.method === 'auth.test') return response({ok: true, team_id: authTeam,
      user_id: BOT, bot_id: 'B12345678', app_id: 'A12345678'});
    if (call.method === 'conversations.list') return response({ok: true,
      channels: channels ?? [{id: CHANNEL, user: USER, is_im: true}],
      response_metadata: {next_cursor: ''}});
    if (call.method === 'conversations.history') {
      historyStarted();
      await historyWait;
      if (historyError) return response({ok: false, error: historyError});
      if (typeof history === 'function') return response(await history(call.params));
      const selected = Array.isArray(history) ? history : history[call.params.channel] ?? [];
      return response({ok: true, messages: selected, has_more: hasMore,
        response_metadata: {next_cursor: hasMore ? 'more-messages' : ''}});
    }
    throw new Error(`Unexpected Slack method: ${call.method}`);
  };
  const api = plugin.createDmCatchup({statePath, teamId: TEAM, botUserId: BOT,
    botToken: TOKEN, fetchFn, now, ...(allowFrom ? {allowFrom} : {}),
    dispatch: async (event, context) => {dispatched.push(event); await dispatch?.(event, context);},
    logger: Object.fromEntries(['debug', 'info', 'warn', 'error'].map(level => [level,
      (...args) => logs.push({level, args})])),
  });
  return {api, statePath, calls, dispatched, logs, historyReached};
}

test('[required-1] downtime DM is recovered exactly once', async t => {
  const question = message();
  const p = await setup(t, {history: [question]});
  await p.api.start();
  await p.api.start();
  assert.deepEqual(p.dispatched.map(event => event.ts), [question.ts]);
  assert.equal(p.dispatched[0].channel, CHANNEL);
  assert.equal(p.dispatched[0].user, USER);
  assert.equal(p.dispatched[0].text, question.text);
});

test('[required-2] a DM before a legacy bot reply is skipped without completion metadata', async t => {
  const question = message();
  const answer = reply(question, 30, {metadata: undefined});
  const p = await setup(t, {history: [answer, question]});
  await p.api.start();
  await p.api.accept(question, {source: 'live'});
  assert.equal(p.dispatched.length, 0);
  const saved = JSON.parse(await readFile(p.statePath, 'utf8'));
  assert.ok(saved.entries.some(row => row.ts === question.ts && row.state === 'done' && row.replyTs === answer.ts));
  const history = p.calls.find(call => call.method === 'conversations.history');
  assert.equal(String(history.params.include_all_metadata), 'true');
});

test('[required-3] a DM older than thirty minutes is skipped', async t => {
  const stale = message(30 * 60 + 1), recent = message(30 * 60 - 1);
  const p = await setup(t, {history: [recent, stale]});
  await p.api.start();
  assert.deepEqual(p.dispatched.map(event => event.ts), [recent.ts]);
});

test('[required-4] simultaneous Socket and history delivery dispatches once',
  {timeout: 3000}, async t => {
    const question = message();
    let releaseHistory;
    const historyWait = new Promise(resolve => {releaseHistory = resolve;});
    const p = await setup(t, {history: [question], historyWait});
    const startup = p.api.start();
    await p.historyReached;
    const live = p.api.accept(question, {source: 'live'});
    releaseHistory();
    await Promise.all([startup, live]);
    assert.deepEqual(p.dispatched.map(event => event.ts), [question.ts]);
  });

test('a digest and incomplete bot reply suppress only questions before the last top-level bot post', async t => {
  const answered = message(180, '一問目です'), pending = message(20, '二問目です');
  const digest = reply(answered, 50, {text: '朝のダイジェスト', metadata: undefined});
  const feedback = reply(pending, 40, {metadata: {event_type: 'teamagent.dm_answer_v1',
    event_payload: {source_channel: CHANNEL, source_ts: pending.ts, complete: false}}});
  const p = await setup(t, {history: [feedback, digest, reply(answered, 90), pending, answered]});
  await p.api.start();
  assert.deepEqual(p.dispatched.map(event => event.ts), [pending.ts]);
});

test('a burst replays only questions after the latest legacy bot post', async t => {
  const questions = [message(240, '一問目です'), message(180, '二問目です'),
    message(90, '三問目です'), message(60, '四問目です')];
  const olderAnswer = reply(questions[0], 210, {metadata: undefined});
  const latestAnswer = reply(questions[1], 120, {metadata: undefined});
  const p = await setup(t, {history: [questions[3], latestAnswer, questions[1], olderAnswer,
    questions[2], questions[0]]});
  await p.api.start();
  for (const question of questions) await p.api.accept(question, {source: 'live'});
  assert.deepEqual(p.dispatched.map(event => event.ts), questions.slice(2).map(question => question.ts));
});

test('a bot reply in a thread cannot suppress the top-level question even with completion metadata', async t => {
  const question = message();
  const p = await setup(t, {history: [reply(question, 30, {thread_ts: question.ts}), question]});
  await p.api.start();
  assert.deepEqual(p.dispatched.map(event => event.ts), [question.ts]);
});

test('a bot root post with its own thread timestamp remains a top-level post', async t => {
  const question = message(), answer = reply(question, 30, {metadata: undefined, thread_ts: ts(30)});
  const p = await setup(t, {history: [answer, question]});
  await p.api.start();
  assert.equal(p.dispatched.length, 0);
});

test('an acknowledged but interrupted DM is retried after restart without a complete reply', async t => {
  const question = message();
  const first = await setup(t);
  await first.api.accept(question, {source: 'live'});
  assert.equal(first.dispatched.length, 1);
  const restarted = await setup(t, {statePath: first.statePath, history: [question]});
  await restarted.api.start();
  await restarted.api.accept(question, {source: 'live'});
  assert.equal(restarted.dispatched.length, 1);
});

test('a completed DM stays completed across two restarts', async t => {
  const question = message(), answer = reply(question);
  const first = await setup(t);
  await first.api.accept(question, {source: 'live'});
  await first.api.markReplied({channel: CHANNEL, sourceTs: question.ts,
    replyTs: answer.ts, kind: 'answer'});
  for (let restart = 0; restart < 2; restart++) {
    const next = await setup(t, {statePath: first.statePath, history: [answer, question]});
    await next.api.start();
    await next.api.accept(question, {source: 'live'});
    assert.equal(next.dispatched.length, 0);
  }
});

test('a legacy Slack reply posted immediately before a crash repairs the missing completion record', async t => {
  const question = message();
  const first = await setup(t);
  await first.api.accept(question, {source: 'live'});
  const second = await setup(t, {statePath: first.statePath,
    history: [reply(question, 30, {metadata: undefined}), question]});
  await second.api.start();
  await second.api.accept(question, {source: 'live'});
  assert.equal(second.dispatched.length, 0);
});

test('missing history scope is reported and does not advance recovery past the question', async t => {
  const blocked = await setup(t, {historyError: 'missing_scope'});
  let failure;
  try { await blocked.api.start(); } catch (error) { failure = error; }
  assert.ok(failure || blocked.logs.some(entry =>
    ['warn', 'error'].includes(entry.level) && /scope|incomplete|unavailable/iu.test(JSON.stringify(entry.args))),
  'scope failure must not look like a successful empty scan');
  await blocked.api.accept(message(), {source: 'live'});
  assert.equal(blocked.dispatched.length, 1, 'history scope failure must preserve normal live delivery');
  const restored = await setup(t, {statePath: blocked.statePath, history: [message()]});
  await restored.api.start();
  assert.equal(restored.dispatched.length, 1);
});

test('a two-hundred-message history limit is reported instead of declaring the DM complete', async t => {
  const history = Array.from({length: 200}, (_, i) => reply(message(300 + i), i + 1,
    {metadata: undefined}));
  const p = await setup(t, {history, hasMore: true});
  await p.api.start();
  assert.equal(p.calls.filter(call => call.method === 'conversations.history').length, 1);
  assert.ok(p.logs.some(entry => ['warn', 'error'].includes(entry.level) &&
    /limit|truncat|incomplete/iu.test(JSON.stringify(entry.args))),
  'unscanned history must remain visible to the operator');
  const saved = JSON.parse(await readFile(p.statePath, 'utf8'));
  assert.ok(saved.unresolved.some(entry => entry.reason === 'history_limit_incomplete' && entry.count > 0));
});

test('foreign authors and foreign-channel bot posts cannot suppress a DM', async t => {
  const question = message();
  for (const candidate of [reply(question, 30, {user: 'U99999999', bot_id: 'B99999999'}),
    reply(question, 30, {user: USER, bot_id: undefined}),
    reply(question, 30, {channel: 'D99999999'})]) {
    const p = await setup(t, {history: [candidate, question]});
    await p.api.start();
    assert.equal(p.dispatched.filter(event => event.ts === question.ts).length, 1);
  }
});

test('an authenticated top-level bot post does not require a matching complete marker', async t => {
  const question = message();
  for (const candidate of [reply({...question, channel: 'D99999999'}), reply(message(100)),
    reply(question, 30, {metadata: {event_type: 'teamagent.dm_answer_v1',
      event_payload: {source_channel: CHANNEL, source_ts: question.ts, complete: false}}})]) {
    const p = await setup(t, {history: [candidate, question]});
    await p.api.start();
    assert.equal(p.dispatched.length, 0);
  }
});

test('recovery ignores bot messages, edits, channel messages and missing canonical identity', async t => {
  const p = await setup(t);
  for (const invalid of [message(60, 'bot', {user: BOT, bot_id: 'B12345678'}),
    message(60, 'edit', {subtype: 'message_changed'}), message(60, 'channel', {channel: 'C12345678'}),
    message(60, 'missing user', {user: undefined}), message(60, 'alias', {channel: 'user:U12345678'}),
    message(60, 'foreign workspace', {team: 'T99999999'}),
    message(60, 'foreign wrapper', {team_id: 'T99999999'})]) {
    await p.api.accept(invalid, {source: 'live'});
  }
  assert.equal(p.dispatched.length, 0);
});

async function storedFiles(path) {
  try { return await readFile(path, 'utf8'); }
  catch (error) {
    if (error.code !== 'EISDIR') throw error;
    return (await Promise.all((await readdir(path)).map(name => storedFiles(join(path, name))))).join('\n');
  }
}

test('question bodies and bot tokens are absent from recovery logs and persistent bookkeeping', async t => {
  const text = '非公開本文PRIVATE-CATCHUP-CONTENT';
  const p = await setup(t, {history: [message(60, text)]});
  await p.api.start();
  const observable = JSON.stringify(p.logs) + await storedFiles(p.statePath);
  assert.ok(!observable.includes(text));
  assert.ok(!observable.includes(TOKEN));
  assert.equal((await stat(p.statePath)).mode & 0o777, 0o600);
});

test('unexpected Slack workspace disables catchup before reading any DM', async t => {
  const p = await setup(t, {history: [message()], authTeam: 'T99999999'});
  await p.api.start();
  assert.equal(p.dispatched.length, 0);
  assert.equal(p.calls.filter(call => call.method === 'conversations.history').length, 0);
  assert.ok(p.logs.some(entry => entry.level === 'warn'));
});

test('journal symlinks are refused without modifying their target', async t => {
  const dir = await mkdtemp(join(tmpdir(), 'teamagent-dm-symlink-'));
  t.after(() => rm(dir, {recursive: true, force: true}));
  const target = join(dir, 'target.json'), statePath = join(dir, 'journal.json');
  await writeFile(target, 'untouched');
  await symlink(target, statePath);
  const p = await setup(t, {statePath, history: [message()]});
  await p.api.start();
  assert.equal(await p.api.accept(message(), {source: 'live'}), false);
  assert.equal(p.dispatched.length, 0);
  assert.equal(await readFile(target, 'utf8'), 'untouched');
});

test('replay is bounded to twenty questions in one DM and runs oldest first', async t => {
  const questions = Array.from({length: 25}, (_, i) => message(60 + i));
  const p = await setup(t, {history: questions});
  await p.api.start();
  assert.deepEqual(p.dispatched.map(event => event.ts), questions.toReversed().slice(0, 20).map(event => event.ts));
  assert.ok(p.logs.some(entry => /limit/iu.test(JSON.stringify(entry.args))));
});

test('replay is bounded to two hundred questions across DMs', async t => {
  const channels = Array.from({length: 11}, (_, i) => ({id: `D${String(i).padStart(8, '0')}`,
    user: `U${String(i).padStart(8, '0')}`, is_im: true}));
  const history = Object.fromEntries(channels.map(channel => [channel.id,
    Array.from({length: 20}, (_, i) => message(60 + i, '質問', {channel: channel.id, user: channel.user}))]));
  const p = await setup(t, {channels, history});
  await p.api.start();
  assert.equal(p.dispatched.length, 200);
  assert.ok(p.logs.some(entry => /limit/iu.test(JSON.stringify(entry.args))));
});

test('failed dispatches still consume the twenty-question per-DM replay budget', async t => {
  const questions = Array.from({length: 25}, (_, i) => message(60 + i));
  const p = await setup(t, {history: questions, dispatch: () => {throw new Error('failed');}});
  await p.api.start();
  assert.equal(p.dispatched.length, 20);
});

test('failed dispatches still consume the two-hundred-question total replay budget', async t => {
  const channels = Array.from({length: 11}, (_, i) => ({id: `D${String(i).padStart(8, '0')}`,
    user: `U${String(i).padStart(8, '0')}`, is_im: true}));
  const history = Object.fromEntries(channels.map(channel => [channel.id,
    Array.from({length: 20}, (_, i) => message(60 + i, '質問', {channel: channel.id, user: channel.user}))]));
  const p = await setup(t, {channels, history, dispatch: () => {throw new Error('failed');}});
  await p.api.start();
  assert.equal(p.dispatched.length, 200);
});

test('unsupported history preparation releases live admission while unknown dispatch failures keep it reserved', async t => {
  const question = message(60, 'この添付を見て', {subtype: 'file_share', files: [{id: 'F12345678'}]});
  for (const [code, liveAllowed] of [['dm_catchup_unsupported_source', true], ['dm_catchup_dispatch_failed', false]]) {
    const p = await setup(t, {history: [question], dispatch: () => {
      throw Object.assign(new Error('failed'), {code});
    }});
    await p.api.start();
    assert.equal(p.dispatched.length, 1);
    assert.equal(await p.api.reserve(question, {source: 'live'}), liveAllowed);
  }
});

test('a completion marker on the second history page prevents replay of the first page', async t => {
  const question = message();
  const p = await setup(t, {history: params => params.cursor
    ? {ok: true, messages: [reply(question)], has_more: false}
    : {ok: true, messages: [question], has_more: true, response_metadata: {next_cursor: 'page-2'}}});
  await p.api.start();
  const calls = p.calls.filter(call => call.method === 'conversations.history');
  assert.equal(calls.length, 2);
  assert.ok(calls.every(call => call.params.limit === 100));
  assert.equal(p.dispatched.length, 0);
});

test('unread history beyond the page budget prevents replay of that DM', async t => {
  const p = await setup(t, {history: [message()], hasMore: true});
  await p.api.start();
  assert.equal(p.dispatched.length, 0, 'an unseen page could contain the matching reply');
  assert.ok(p.logs.some(entry => /incomplete/iu.test(JSON.stringify(entry.args))));
});

test('runtime allowFrom applies equally to history and live messages', async t => {
  const p = await setup(t, {history: [message()], allowFrom: ['U99999999']});
  await p.api.start();
  await p.api.accept(message(), {source: 'live'});
  assert.equal(p.calls.filter(call => call.method === 'conversations.history').length, 0);
  assert.equal(p.dispatched.length, 0);
});

test('recovery keeps its original cutoff while Slack history is being fetched', async t => {
  let current = NOW;
  let dispatchContext;
  const question = message(29 * 60);
  const p = await setup(t, {now: () => current, dispatch: (_event, context) => {dispatchContext = context;}, history: () => {
    current += 2 * 60 * 1000;
    return {ok: true, messages: [question], has_more: false};
  }});
  await p.api.start();
  assert.deepEqual(p.dispatched.map(event => event.ts), [question.ts]);
  assert.deepEqual(dispatchContext, {source: 'replay', cutoff: NOW});
});

test('corrupt bookkeeping is refused and preserved instead of silently resetting completion', async t => {
  const p = await setup(t);
  await writeFile(p.statePath, 'invalid-journal');
  await p.api.start();
  assert.equal(await p.api.accept(message(), {source: 'live'}), false);
  assert.equal(p.api.stateStatus, 'unavailable');
  assert.equal(await readFile(p.statePath, 'utf8'), 'invalid-journal');
});

test('a rejected journal write does not permanently poison later writes after the path is repaired', async t => {
  const p = await setup(t);
  await p.api.reserve(message());
  const saved = await readFile(p.statePath, 'utf8');
  await rm(p.statePath);
  await symlink(join(p.statePath, 'missing-target'), p.statePath);
  assert.equal(await p.api.reserve(message(50)), false);
  assert.equal(p.api.stateStatus, 'unavailable');
  await rm(p.statePath);
  await writeFile(p.statePath, saved);
  assert.equal(await p.api.reserve(message(50)), true);
  assert.equal(p.api.stateStatus, 'ready');
});

test('a nested completion marker preserves existing Slack metadata and skips the answered DM', async t => {
  const question = message();
  const marked = reply(question, 30, {metadata: {event_type: 'slack_assistant_thread', event_payload: {
    assistant_thread: {version: 1}, teamagent_dm_answer_v1: {
      source_channel: CHANNEL, source_ts: question.ts, complete: true,
    },
  }}});
  const p = await setup(t, {history: [marked, question]});
  await p.api.start();
  assert.equal(p.dispatched.length, 0);
});

test('saved completion without the matching Slack reply does not suppress recovery', async t => {
  const question = message();
  const first = await setup(t);
  await first.api.accept(question);
  await first.api.markReplied({channel: CHANNEL, sourceTs: question.ts, replyTs: ts(30)});
  const second = await setup(t, {statePath: first.statePath, history: [question]});
  await second.api.start();
  assert.deepEqual(second.dispatched.map(event => event.ts), [question.ts]);
});

test('saved completion with an unrelated top-level bot reply still skips older questions', async t => {
  const question = message();
  const first = await setup(t);
  await first.api.accept(question);
  await first.api.markReplied({channel: CHANNEL, sourceTs: question.ts, replyTs: ts(30)});
  const second = await setup(t, {statePath: first.statePath,
    history: [reply(message(100)), question]});
  await second.api.start();
  assert.equal(second.dispatched.length, 0);
});

test('file-share messages with a question retain attachments while other subtypes stay excluded', async t => {
  const files = [{id: 'F12345678', name: 'brief.pdf'}];
  const question = message(60, 'この添付を要約して', {subtype: 'file_share', files});
  const p = await setup(t, {history: [question]});
  await p.api.start();
  assert.deepEqual(p.dispatched.map(event => event.files), [files]);
  await p.api.accept(message(50, ' ', {subtype: 'file_share', files}));
  await p.api.accept(message(40, '編集です', {subtype: 'message_changed'}));
  assert.equal(p.dispatched.length, 1);
});

test('unresolved replies are persisted using fixed reasons and numeric counts only', async t => {
  const p = await setup(t);
  assert.equal(await p.api.markUnresolved('no_final_reply', 2), true);
  const saved = JSON.parse(await readFile(p.statePath, 'utf8'));
  assert.deepEqual(saved.unresolved, [{reason: 'no_final_reply', count: 2}]);
  await assert.rejects(p.api.markUnresolved('private@example.test'), /invalid recovery reason/u);
  await assert.rejects(p.api.markUnresolved('no_final_reply', -1), /invalid recovery reason/u);
  assert.ok(!(await readFile(p.statePath, 'utf8')).includes('private@example.test'));
});

test('reserve reports journal unavailability per call without misclassifying a duplicate', async t => {
  const p = await setup(t);
  const question = message();
  assert.equal(await p.api.reserve(question), true);
  await rm(p.statePath);
  await symlink(join(p.statePath, 'missing'), p.statePath);
  let unavailable = 0;
  assert.equal(await p.api.reserve(message(50), {onUnavailable: () => {unavailable++;}}), false);
  assert.equal(unavailable, 1);
  assert.equal(await p.api.reserve(question, {onUnavailable: () => {unavailable++;}}), false);
  assert.equal(unavailable, 1, 'a stale global unavailable status cannot turn a duplicate into live work');
  assert.equal(await p.api.reserve(message(40, 'invalid', {channel: 'user:U12345678'}),
    {onUnavailable: () => {unavailable++;}}), false);
  assert.equal(unavailable, 1);
});

test('complete markers from another workspace or before the question are rejected', async t => {
  const question = message();
  for (const candidate of [reply(question, 30, {team: 'T99999999'}),
    reply(question, 30, {team_id: 'T99999999'}), reply(question, 120)]) {
    const p = await setup(t, {history: [candidate, question]});
    await p.api.start();
    assert.equal(p.api.botUserId, BOT);
    assert.deepEqual(p.dispatched.map(event => event.ts), [question.ts]);
  }
});

test('unsupported source is recorded without exposing the failed question or exception', async t => {
  const question = message(60, 'private-body-private-email@example.com');
  const p = await setup(t, {history: [question], dispatch: async () => {
    const error = new Error(question.text);
    error.code = 'dm_catchup_unsupported_source';
    throw error;
  }});
  await p.api.start();
  const saved = JSON.parse(await readFile(p.statePath, 'utf8'));
  assert.ok(saved.unresolved.some(row => row.reason === 'unsupported_source'));
  assert.equal(saved.entries[0].state, 'started');
  assert.doesNotMatch(JSON.stringify(p.logs) + JSON.stringify(saved), /private-body|example.com/u);
});
