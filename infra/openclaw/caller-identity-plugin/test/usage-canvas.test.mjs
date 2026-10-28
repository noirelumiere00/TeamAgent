import test from 'node:test';
import assert from 'node:assert/strict';
import {createCallerIdentityPlugin} from '../dist/index.js';

const url = 'https://vector-workspcae.slack.com/docs/T07MU5P2PBR/F0CCGL1SFPW';
const link = `<${url}|Aico の使い方>`;
const question = 'Slack 連携で何ができて、どう使い始めるか教えて';
const answer = 'Slack で検索・要約できます。';

function setup(content = question) {
  const hooks = new Map();
  const ctx = {runId: 'usage-run', sessionKey: 'agent:teamagent:slack:direct:u12345678',
    senderId: 'U12345678', channelId: 'U12345678'};
  createCallerIdentityPlugin({env: {
    TEAMAGENT_CALLER_CLAIM_SECRET: 'x'.repeat(48), SLACK_TEAM_ID: 'T07MU5P2PBR',
  }}).register({registerInteractiveHandler() {}, logger: {info() {}, warn() {}},
    on: (name, fn) => hooks.set(name, fn)});
  const receive = (body, runId = ctx.runId, messageId = '1793080500.123456') =>
    hooks.get('message_received')({content: body, senderId: ctx.senderId,
      messageId, runId, metadata: {guildId: 'T07MU5P2PBR', to: `user:${ctx.senderId}`}},
    {...ctx, runId, channelId: 'slack', conversationId: `user:${ctx.senderId}`, messageId});
  receive(content);
  const send = (payload = {text: answer}, fields = {}, context = ctx) => {
    const result = hooks.get('reply_payload_sending')({runId: ctx.runId, payload, ...fields}, context);
    assert.notEqual(result?.cancel, true);
    return result?.payload ?? payload;
  };
  return {hooks, ctx, receive, send};
}

for (const content of [question, 'Aico の使い方を教えて', 'Aico は何ができる？',
  'Aico をどう使い始めればいいですか？', 'Slack連携について知りたい', '使い方を教えて', '何ができる？']) {
  test(`usage question appends one canvas line: ${content}`, () => {
    const p = setup(content);
    const payload = {text: answer + '\n何か他に知りたいことはありますか？', marker: 'keep'};
    assert.deepEqual(p.send(payload), {text: `${answer}\n${link}`, marker: 'keep'});
    assert.ok(payload.text.endsWith('ありますか？'));
  });
}

for (const existing of [link, url, `[使い方](${url})`, `<${url}|社内ガイド>`]) {
  test(`existing canvas URL is not duplicated: ${existing}`, () => {
    const payload = {text: `${answer}\n${existing}`};
    assert.deepEqual(setup().send(payload), payload);
  });
}

for (const content of ['提案資料を作って', '過去の事例を検索して', 'この会議を要約して',
  'Aico の使い方の資料を作って', 'Slack連携の説明を検索して', 'Aico の使い方を要約して',
  'Excel の使い方を教えて', 'Slack連携のリンクを送って', 'ありがとう']) {
  test(`other request does not append a canvas: ${content}`, () => {
    const payload = {text: answer};
    assert.deepEqual(setup(content).send(payload), payload);
  });
}

for (const extra of [
  {blocks: [{type: 'section', text: {type: 'mrkdwn', text: answer}}]},
  {attachments: [{title: '資料', text: answer}]}, {files: [{id: 'F123'}]},
  {mediaUrl: 'https://example.test/guide.pdf'}, {mediaUrls: ['https://example.test/guide.pdf']},
  {channelData: {slack: {blocks: [{type: 'divider'}]}}},
]) {
  test(`Block Kit and attachments stay unchanged: ${Object.keys(extra)[0]}`, () => {
    const payload = {text: answer, ...extra};
    const snapshot = structuredClone(payload);
    assert.deepEqual(setup().send(payload), snapshot);
    assert.deepEqual(payload, snapshot);
  });
}

for (const fields of [{kind: 'tool'}, {kind: 'block'}]) {
  test(`non-final reply stays unchanged: ${fields.kind}`, () => {
    assert.equal(setup().send({text: answer}, fields).text, answer);
  });
}
for (const flag of ['isCommentary', 'isReasoning']) {
  test(`progress reply stays unchanged: ${flag}`, () => {
    const payload = {text: answer, [flag]: true};
    assert.deepEqual(setup().send(payload), payload);
  });
}

test('only the current bound request controls the canvas', () => {
  const p = setup();
  p.receive('資料を作って', 'next-run', '1793080501.123456');
  assert.equal(p.send({text: answer}, {runId: 'next-run'}, {...p.ctx, runId: 'next-run'}).text, answer);
  assert.equal(p.send({text: answer}, {}, {...p.ctx, runId: 'wrong-run'}).text, answer);
  assert.equal(p.send({text: answer}, {runId: undefined}).text, answer);
});

for (const first of [null, question]) {
  test(`bodyless repeated notification preserves usage intent: ${first === null ? 'first' : 'last'}`, () => {
    const p = setup(first);
    p.receive(first === null ? question : undefined);
    p.hooks.get('agent_end')({runId: p.ctx.runId}, p.ctx);
    assert.equal(p.send().text, `${answer}\n${link}`);
  });
}
