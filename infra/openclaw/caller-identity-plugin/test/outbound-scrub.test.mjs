import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createCallerIdentityPlugin, scrubOutboundReplyText} from '../dist/index.js';

const cases = JSON.parse(readFileSync(new URL('../../../../tests/mcp_gateway/outbound_reply_cases.json', import.meta.url), 'utf8'));
for (const fixture of cases) {
  test(`outbound scrub: ${fixture.id}`, () => {
    const out = scrubOutboundReplyText(fixture.input, undefined, false,
      new Map(Object.entries(fixture.permanent_urls ?? {}))).text;
    assert.equal(out, fixture.expected);
    assert.equal(scrubOutboundReplyText(out).text, out);
  });
}

function hooks() {
  const hooks = new Map();
  createCallerIdentityPlugin({env: {TEAMAGENT_CALLER_CLAIM_SECRET: 'x'.repeat(48), SLACK_TEAM_ID: 'T12345678'}})
    .register({on: (name, fn) => hooks.set(name, fn), registerInteractiveHandler() {}, logger: {info() {}, warn() {}}});
  return hooks;
}

test('final hook cleans block text as well as the notification', () => {
  const payload = {text: '結果です。', blocks: [{type: 'section', text: {type: 'mrkdwn', text: 'TypeError: private details'}}]};
  const result = hooks().get('reply_payload_sending')({payload}, {});
  assert.equal(result.payload.text, '結果です。\n処理の途中で止まりました。もう一度お試しください。');
  assert.equal(result.payload.blocks, undefined);
  assert.equal(payload.blocks[0].text.text, 'TypeError: private details');
});

test('existing compact budget never leaves a partial link', () => {
  assert.equal(scrubOutboundReplyText('あ'.repeat(3000)).text, 'あ'.repeat(2499) + '…');
  for (const link of ['https://example.test/' + 'x'.repeat(800),
    '[資料](https://example.test/' + 'x'.repeat(800) + ')',
    '<https://example.test/' + 'x'.repeat(800) + '|資料>']) {
    assert.equal(scrubOutboundReplyText('あ'.repeat(2300) + '\n' + link).text, 'あ'.repeat(2300) + '…');
  }
});

test('final hook uses the original from the same result and never another run', () => {
  const h = hooks();
  const temporary = 'https://s3.example.test/a?X-Amz-Signature=secret';
  const original = 'https://drive.google.com/file/d/ORIGINAL/view';
  const ctx = {runId: 'original-run'};
  h.get('after_tool_call')({toolName: 'teamagent__video_analysis',
    result: {content: [{type: 'text', text: JSON.stringify({report_url: temporary, drive_url: original})}]}}, ctx);
  const send = runId => h.get('reply_payload_sending')({runId, payload: {text: temporary}}, {runId}).payload.text;
  assert.equal(send(ctx.runId), original);
  assert.equal(send('another-run'), '（社内リンクは省略しました）');
});
