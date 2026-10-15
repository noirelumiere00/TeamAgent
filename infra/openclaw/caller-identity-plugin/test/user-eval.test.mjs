import test from 'node:test';
import assert from 'node:assert/strict';
import { createCallerIdentityPlugin } from '../dist/index.js';

function setup() {
  const hooks = new Map();
  const logs = [];
  createCallerIdentityPlugin({ env: {
    TEAMAGENT_CALLER_CLAIM_SECRET: 'x'.repeat(48), SLACK_TEAM_ID: 'T07MU5P2PBR',
  }}).register({ registerInteractiveHandler() {},
    logger: { info: line => logs.push(line), warn: line => logs.push(line) },
    on: (name, fn) => hooks.set(name, fn) });
  const ctx = { runId: 'run-1' };
  const send = (text, eventCtx = ctx, eventRunId = eventCtx.runId) =>
    hooks.get('reply_payload_sending')({ runId: eventRunId, payload: {text} }, eventCtx)?.payload?.text ?? text;
  const remember = (url, event = {}, eventCtx = ctx) => hooks.get('after_tool_call')({
    toolName: 'teamagent__oauth_connect',
    result: {content: [{type: 'text', text: JSON.stringify({slack_url: url})}]}, ...event,
  }, eventCtx);
  return { hooks, ctx, send, remember, logs };
}
const fallback = '連携は Aico の DM に『連携』とだけ送ると、正しいリンクをお出しします';
// 2026-10-15 の実際の文字列（省略記号も URL の一部）。
const fake = 'https://connect.newstv.co.jp/oauth/slack?state=eyJhbG…QEpU';
const real = 'https://connect.newstv.co.jp/slack/oauth/start/issued_token-09==';
const google = 'https://connect.newstv.co.jp/oauth2/start/issued_token-09=';
const renders = [url => url, url => `<${url}|連携>`, url => `[連携](${url})`];
for (const [index, render] of renders.entries()) {
  test(`malformed link is replaced in rendered form ${index}`, () => {
    const { send, logs } = setup();
    assert.equal(send(`説明 ${render(fake)}`), `説明 ${fallback}`);
    assert.ok(logs.some(line => line.includes('diagnostic=CONNECT-Z03')));
    assert.ok(logs.every(line => !line.includes('eyJhbG') && !line.includes('QEpU')));
  });
  test(`allowed start links survive without run information ${index}`, () => {
    const { send, logs } = setup();
    for (const url of [real, google, real.replace(/=+$/, '')]) {
      assert.equal(send(render(url)), render(url));
      assert.equal(send(render(url), {}), render(url));
      assert.equal(send(render(url), {}, 'run-1'), render(url));
      assert.equal(send(render(url), {runId: 'other'}, 'run-1'), render(url));
    }
    assert.ok(!logs.some(line => line.includes('CONNECT-Z03')));
  });
  test(`same run link restores malformed link after agent_end ${index}`, () => {
    const { hooks, ctx, send, remember, logs } = setup();
    remember(real);
    hooks.get('agent_end')({runId: ctx.runId}, ctx);
    assert.equal(send(render(fake)), real);
    assert.ok(logs.some(line => line.includes('diagnostic=CONNECT-Z03')));
    assert.ok(logs.every(line => !line.includes('issued_token') && !line.includes('eyJhbG')));
    ctx.runId = 'run-2';
    assert.equal(send(render(fake)), fallback);
    assert.equal(send(render(real)), render(real));
  });
}

test('Google start link is also usable as the same run replacement', () => {
  const {send, remember} = setup();
  remember(google);
  assert.equal(send('https://connect.newstv.co.jp/oauth/google?state=bad'), google);
});

test('only permitted URL shapes survive, even when malformed URL came from tool', () => {
  const malformed = [
    fake, 'https://connect.newstv.co.jp/oauth/google',
    'https://connect.newstv.co.jp/connect', 'https://connect.newstv.co.jp/help?state=abc',
    `${real}?state=abc`, `${google}?scope=mail`, `${real}#fragment`,
    'https://connect.newstv.co.jp/oauth2/start/',
    'https://connect.newstv.co.jp/oauth2/start/token…tail',
    'https://connect.newstv.co.jp/slack/oauth/start/token...tail',
    'https://connect.newstv.co.jp/oauth2/start/token/extra',
    'https://connect.newstv.co.jp/oauth2/start/token!tail',
    'https://connect.newstv.co.jp/oauth2/start/to=ken',
    'https://connect.newstv.co.jp/oauth2/start/token===',
    'http://connect.newstv.co.jp/oauth2/start/token',
  ];
  for (const url of malformed) {
    const {send, remember} = setup();
    remember(url);
    for (const render of renders) assert.equal(send(render(url)), fallback, url);
  }
});

test('missing, conflicting or unrelated tool results never supply replacement links', () => {
  for (const [event, eventCtx] of [
    [{toolName: 'teamagent__search'}, undefined],
    [{error: 'failed'}, undefined],
    [{result: {isError: true, url: real}}, undefined],
    [{runId: 'other'}, undefined], [{}, {}],
  ]) {
    const {send, remember} = setup();
    remember(real, event, eventCtx);
    assert.equal(send(fake), fallback);
    assert.equal(send(real, {}), real);
  }
  const {send, remember} = setup();
  remember(real);
  assert.equal(send(fake, {}), fallback);
  assert.equal(send(fake, {runId: 'other'}, 'run-1'), fallback);
});

test('unrelated URLs are unchanged', () => {
  const {send} = setup();
  for (const url of ['https://connect.newstv.co.jp/help',
    'https://connect.newstv.co.jp/r/report-token',
    'https://example.test/oauth/slack?state=eyJhbG…QEpU',
    'https://connect.newstv.co.jp.example.test/connect?state=abc']) {
    for (const render of renders) assert.equal(send(render(url)), render(url));
  }
});

test('malformed links in labels and titles are replaced', () => {
  const { send } = setup();
  const fakeUpper = fake.replace('connect.newstv.co.jp', 'CONNECT.NEWSTV.CO.JP');
  for (const text of [`[${fakeUpper}](https://example.test/help)`,
    `<https://example.test/help|${fakeUpper}>`,
    `[説明](https://example.test/help "${fakeUpper}")`]) {
    const out = send(text);
    assert.ok(!out.includes(fakeUpper));
    assert.ok(out.includes(fallback));
    assert.ok(out.includes('https://example.test/help'));
  }
});

test('missing table cells normalize while prose and table syntax retain behavior', () => {
  const { send } = setup();
  assert.equal(send('| ハンバーグ | 6本 | 1,230,000 | 205,000 | 507,700 | — | - | — |'),
    '| ハンバーグ | 6本 | 1,230,000 | 205,000 | 507,700 | なし | なし | なし |');
  assert.equal(send('説明 — 補足'), '説明、補足');
  assert.equal(send('| --- | :---: |'), '| --- | :---: |');
  assert.equal(send('```\n| — | - |\n```'), '```\n| — | - |\n```');
});
