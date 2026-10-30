import test from 'node:test';
import assert from 'node:assert/strict';
import {AD_LABEL_ROUTE_CONTEXT, adLabelUrlCount, createCallerIdentityPlugin} from '../dist/index.js';

const TT = 'https://www.tiktok.com/@shop/video/7400000000000000001';
const IG = 'https://www.instagram.com/reel/DX7lzTOJ1p6/';
const YT = 'https://youtu.be/dQw4w9WgXcQ';

test('adLabelUrlCount: 投稿 URL だけ（Slack の <url|label> 形・確認の短い語つき）を数える', () => {
  assert.equal(adLabelUrlCount(`${TT}\n${IG}\n${YT}`), 3);
  assert.equal(adLabelUrlCount(`<${TT}>\n<${IG}|${IG}>`), 2);
  assert.equal(adLabelUrlCount(`タイアップ確認お願いします\n${TT}`), 1);
  assert.equal(adLabelUrlCount('https://vt.tiktok.com/ZSabc/ PRチェック'), 1);
  assert.equal(adLabelUrlCount(`<@U0AICO123> ${TT}`), 1);
  assert.equal(adLabelUrlCount(`@Aico ${YT}`), 1);
  // 2026-10-30 本番の DM: スキーム無しで貼られ、Slack が http の <url|表示名> にする形の両方。
  assert.equal(adLabelUrlCount('tiktok.com/@trend/video/7699768122319949076\ninstagram.com/p/De1ETJtGXpD\nyoutube.com/shorts/hzfcNYdR1JM'), 3);
  assert.equal(adLabelUrlCount('<http://tiktok.com/@trend/video/7699768122319949076|tiktok.com/@trend/video/7699768122319949076>'), 1);
});

test('adLabelUrlCount: 依頼語・他サイト・URL なし・21 件以上は 0（video_analysis 等の領分を奪わない）', () => {
  assert.equal(adLabelUrlCount(`この動画を分析して ${TT}`), 0);
  assert.equal(adLabelUrlCount(`${TT}\nhttps://example.com/a`), 0);
  assert.equal(adLabelUrlCount('https://www.tiktok.com/@shop'), 0);
  assert.equal(adLabelUrlCount('タイアップ確認'), 0);
  assert.equal(adLabelUrlCount(Array.from({length: 21}, (_, i) => `https://youtu.be/${String(i).padStart(11, '0')}`).join('\n')), 0);
  assert.equal(adLabelUrlCount(undefined), 0);
});

function setup() {
  const hooks = new Map();
  createCallerIdentityPlugin({env: {TEAMAGENT_CALLER_CLAIM_SECRET: 'x'.repeat(48), SLACK_TEAM_ID: 'T07MU5P2PBR'}})
    .register({registerInteractiveHandler() {}, logger: {info() {}, warn() {}}, on: (name, fn) => hooks.set(name, fn)});
  const ctx = {runId: 'run-ad', sessionKey: 'agent:teamagent:slack:direct:u12345678',
    senderId: 'U12345678', channelId: 'U12345678'};
  const promptFor = async (content, messageId = '1792041600.123456') => {
    await hooks.get('message_received')({content, senderId: ctx.senderId, messageId, runId: ctx.runId,
      metadata: {guildId: 'T07MU5P2PBR', to: 'user:U12345678'}},
    {...ctx, channelId: 'slack', conversationId: 'user:U12345678', messageId});
    return hooks.get('before_prompt_build')({runId: ctx.runId, messages: [{role: 'user', content}]}, ctx);
  };
  return {promptFor};
}

test('投稿 URL だけの受信は、その回だけ ad_label_check の指示を差し込む', async () => {
  const out = await setup().promptFor(`${TT}\n${IG}`);
  assert.match(out?.appendContext ?? '', /ad_label_check/);
  assert.ok(out.appendContext.includes(AD_LABEL_ROUTE_CONTEXT));
});

test('分析の依頼や普通の発話には差し込まない', async () => {
  for (const content of [`この動画を分析して ${TT}`, '伊藤ハムの施策は今どうなっている？']) {
    const out = await setup().promptFor(content);
    assert.doesNotMatch(out?.appendContext ?? '', /ad_label_check/);
  }
});
