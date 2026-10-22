import test from 'node:test';
import assert from 'node:assert/strict';
import {createCallerIdentityPlugin, scrubOutboundReplyText} from '../dist/index.js';

const leaked = "3KWのTikTok面チェックは、先に tiktok_acquire(keywords=['ハンバーグ', '冷凍ハンバーグ', '夜ご飯'], videos_per_kw=0) を実行し、完了後に acquire_job_id を渡してください（直接取得は2KWまで）。今回は分析本数は5にしました、競合比較を省きました。";
const receipt = '提案書の作成を受け付けました。';
for (const directive of [leaked, '先に tiktok_acquire(keywords=[\n"ハンバーグ"\n]) を実行します。',
  '`tiktok_acquire` (keywords=["ハンバーグ"]) を実行します。',
  'search(query="ハンバーグ") を呼びます。', 'future_internal_tool(foo=1) を呼びます。',
  '完了後に acquire_job_id を渡してください。', 'job_id を控えてください。',
  'videos_per_kw=0 で進めてください。']) {
  test('drops whole internal instruction paragraph: ' + directive.slice(0, 35), () => {
    const text = `${receipt}\n\n${directive}\n\n完成したらお届けします。`;
    assert.equal(scrubOutboundReplyText(text).text, `${receipt}\n\n完成したらお届けします。`);
    assert.equal(scrubOutboundReplyText(directive).text, '');
  });
}

test('preserves ordinary sentences and URL paths containing internal words', () => {
  for (const text of ['Google search で調べます。', '関数 f(x) の説明です。',
    'https://example.test/acquire_job_id', '[資料](https://example.test/tiktok_acquire(foo))',
    '<https://example.test/acquire_job_id|資料>']) {
    assert.equal(scrubOutboundReplyText(text).text, text);
  }
});

function plugin() {
  const hooks = new Map();
  createCallerIdentityPlugin({env: {TEAMAGENT_CALLER_CLAIM_SECRET: 'x'.repeat(48),
    SLACK_TEAM_ID: 'T07MU5P2PBR'}}).register({registerInteractiveHandler() {},
    logger: {info() {}, warn() {}}, on: (name, fn) => hooks.set(name, fn)});
  return hooks;
}

test('send hook removes leaked paragraph while keeping receipt and payload fields', () => {
  const hooks = plugin();
  const payload = {text: `${receipt}\n\n${leaked}`, marker: 'keep'};
  const result = hooks.get('reply_payload_sending')({payload}, {});
  assert.equal(result.payload.text, receipt);
  assert.equal(result.payload.marker, 'keep');
  assert.equal(payload.text, `${receipt}\n\n${leaked}`);
});

test('send hook cancels text-only reply when all paragraphs were internal instructions', () => {
  const result = plugin().get('reply_payload_sending')({payload: {text: leaked}}, {});
  assert.equal(result.cancel, true);
});

test('send hook keeps attachments when text was only internal instructions', () => {
  const payload = {text: leaked, attachments: [{title: '提案書'}]};
  const result = plugin().get('reply_payload_sending')({payload}, {});
  assert.equal(result.payload.text, '');
  assert.equal(result.payload.attachments, payload.attachments);
  assert.notEqual(result.cancel, true);
});
