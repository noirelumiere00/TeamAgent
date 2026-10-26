import test from 'node:test';
import assert from 'node:assert/strict';
import {createCallerIdentityPlugin, scrubOutboundReplyText} from '../dist/index.js';

for (const tail of ['何か他に知りたいことはありますか？', '何か試してみたいことはありますか？',
  '他にお手伝いできることはありますか？', '資料が必要でしたら、お知らせください。',
  '詳しい内容をご覧になりたい場合は、お知らせください。']) {
  test(`common outbound removes optional offer: ${tail}`, () => {
    const text = `Slack で検索・要約できます。\n${tail}`;
    const hooks = new Map();
    createCallerIdentityPlugin({env: {TEAMAGENT_CALLER_CLAIM_SECRET: 'x'.repeat(48), SLACK_TEAM_ID: 'T12345678'}}).register({on: (name, fn) => hooks.set(name, fn),
      registerInteractiveHandler() {}, logger: {info() {}, warn() {}}});
    const blocks = [{type: 'section', text: {type: 'plain_text', text: '添付'}}];
    const payload = {text, blocks, files: [{id: 'F123'}]};
    const result = hooks.get('reply_payload_sending')({payload}, {});
    assert.equal(result?.payload?.text, 'Slack で検索・要約できます。');
    assert.equal(result.payload.blocks, blocks);
    assert.equal(result.payload.files, payload.files);
    assert.equal(payload.text, text);
    assert.equal(scrubOutboundReplyText(result.payload.text).text, result.payload.text);
  });
}

for (const text of ['対象はどの会社ですか？', 'どの資料を修正しますか？',
  '削除してよろしいですか？', '不足している公式URLを教えてください。',
  '「何か試してみたいことはありますか？」という文を削除します。',
  '> 何か他に知りたいことはありますか？', '```\n何か試してみたいことはありますか？\n```',
  '<https://example.test/help|何か試してみたいことはありますか？>']) {
  test(`common outbound preserves clarification and quoted text: ${text}`, () => {
    assert.equal(scrubOutboundReplyText(text).text, text);
  });
}

test('removes consecutive final offers without deleting a required question', () => {
  const question = '対象はどの会社ですか？';
  assert.equal(scrubOutboundReplyText(`${question}\n何か他に知りたいことはありますか？\n何か試してみたいことはありますか？`).text, question);
});
