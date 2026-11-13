import test from 'node:test';
import assert from 'node:assert/strict';
import {OMIYAGE_ROUTE_CONTEXTS, createCallerIdentityPlugin, omiyageRouteKind} from '../dist/index.js';

// D1 の decks corpus（tests/routing/decks_routing_corpus.jsonl・PR #905）と同じ発話で
// 判定を凍結する。Bedrock 試験（30 発話×3 回）で聞き返しが 70/90 だった問題への対処
//（2026-11-13 持ち主指示「評価側で聞き返しも正解にはせず、直す方向」）。
const corpusExpectations = [
  ['om-01', '来週初回訪問のあるAフーズに持っていく、TikTokの調査資料をお土産で1冊作って', 'submit'],
  ['om-02', 'うちの商品と競合2社をTikTok検索で比べた比較資料をパワポにして', 'submit'],
  ['om-05', 'さっきのTikTokの調査、パワポにして', 'submit'],
  ['om-06', 'C社のTikTok調査資料まだ?', 'status'],
  ['om-07', 'お土産資料のジョブ、進捗教えて', 'status'],
  ['om-08', 'お土産資料をもう1冊。競合をE社とF社に変えて', 'submit'],
  ['om-09', 'はじめて伺う取引先用にTikTokの市場調査スライドを作ってほしい。中立ワードも選んで', 'submit'],
  ['om-10', '競合のTikTokでの出方を調査して、比較表のある資料にして', 'submit'],
  ['bd-01', '上位動画を分析して、その結果を持っていける資料にして', 'submit'],
  ['bd-02', 'この分析、パワポにして', 'submit'],
  ['bd-03', '資料にして', 'deck_only'],
  ['bd-07', '86枚の提案書をパワポで作って', 'proposal_builder'],
  ['bd-08', 'TikTok調査のお土産資料と、86枚の提案書を両方作って', 'both'],
  ['bd-09', 'なんであの動画がバズったのか分析したレポートをちょうだい', null],
  ['bd-10', '営業に持ってけるようなTikTokの話題の資料', null],
  ['ow-01', 'パワポ', null],
  ['ow-02', 'スライド', null],
  ['pb-01', '新商材の企画提案書を作って。86枚の統合FMTで', 'proposal_builder'],
  ['pb-02', 'Dの商品の提案書をFMTどおりに作って。与件は資料に書いた通り', 'proposal_builder'],
  ['pb-03', '提案書のジョブ、まだ完了してない?', null],
  ['pb-04', '商品発表用の86枚FMTの提案書を非同期で作成して', 'proposal_builder'],
  ['va-01', 'TikTokでハンバーグ市場の上位動画を新しく取得して、何が効いたか分析して', null],
  ['va-02', '競合の上位動画の構成・フック・CTAの共通点を読み解いて', null],
  ['va-03', '動画分析のレポート、まだ?', null],
  ['va-04', '上位10本のテロップの傾向を調べて', null],
  ['bd-04', 'Xでの生活者の声（不満・欲求）を集めて、提案書に貼れる形にして', null],
  ['bd-05', '伊藤ハムに似た過去の提案書を探して', null],
  ['bd-06', '伊藤ハムの提案の骨子を作って', 'proposal_draft'],
];

test('omiyageRouteKind: decks corpus の期待と一致する', () => {
  for (const [id, utterance, expected] of corpusExpectations) {
    assert.equal(omiyageRouteKind(utterance), expected, `${id} ${utterance}`);
  }
});

test('omiyageRouteKind: 引用・コードブロックの中身は判定に使わない', () => {
  assert.equal(omiyageRouteKind('```お土産資料を作って```'), null);
  assert.equal(omiyageRouteKind('「お土産資料を作って」という文を翻訳して'), null);
});

test('omiyageRouteKind: 文字列以外は null', () => {
  assert.equal(omiyageRouteKind(undefined), null);
  assert.equal(omiyageRouteKind(null), null);
});

test('OMIYAGE_ROUTE_CONTEXTS: 種類ごとに文言がある', () => {
  for (const kind of ['submit', 'status', 'proposal_builder', 'proposal_draft', 'both', 'deck_only']) {
    assert.ok(OMIYAGE_ROUTE_CONTEXTS[kind]?.length > 40, kind);
  }
});

function setup(env) {
  const hooks = new Map();
  createCallerIdentityPlugin({
    env: {TEAMAGENT_CALLER_CLAIM_SECRET: 'x'.repeat(48), SLACK_TEAM_ID: 'T12345678', ...env},
  }).register({
    on: (name, fn) => hooks.set(name, fn),
    registerInteractiveHandler() {},
    logger: {info() {}, warn() {}},
  });
  const promptFor = async (content, messageId = '1792041600.123456') => {
    // rememberInbound は channelId が "slack" のときだけ受信を覚える（prod と同じ）。
    await hooks.get('message_received')({content, senderId: 'U12345678', messageId, runId: 'run-omi',
      metadata: {guildId: 'T12345678', to: 'user:U12345678'}},
    {runId: 'run-omi', sessionKey: 'agent:teamagent:slack:direct:u12345678',
      senderId: 'U12345678', channelId: 'slack', conversationId: 'user:U12345678', messageId});
    return hooks.get('before_prompt_build')({runId: 'run-omi',
      messages: [{role: 'user', content}]},
    {runId: 'run-omi', sessionKey: 'agent:teamagent:slack:direct:u12345678', channelId: 'user:U12345678'});
  };
  return {promptFor};
}

test('フラグ ON: お土産資料の依頼に omiyage の指示を差し込む', async () => {
  const {promptFor} = setup({TEAMAGENT_OMIYAGE_ROUTE: '1'});
  const out = await promptFor('来週初回訪問のあるAフーズに持っていく、TikTokの調査資料をお土産で1冊作って');
  assert.match(out?.appendContext ?? '', /omiyage_report_submit/);
  assert.match(out?.appendContext ?? '', /呼ぶ前に項目を聞き返さない/);
});

test('フラグ OFF（既定）: 差し込まない', async () => {
  const {promptFor} = setup({});
  const out = await promptFor('来週初回訪問のあるAフーズに持っていく、TikTokの調査資料をお土産で1冊作って');
  assert.doesNotMatch(out?.appendContext ?? '', /omiyage_report_submit/);
});
