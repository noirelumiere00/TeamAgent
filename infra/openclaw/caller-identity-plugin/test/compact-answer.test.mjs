import test from 'node:test';
import assert from 'node:assert/strict';
import {createCallerIdentityPlugin} from '../dist/index.js';

function setup(content = '施策は今どうなっている？', threadId = undefined, direct = true) {
  const hooks = new Map();
  const ctx = {threadId, runId: 'short-run', sessionKey: 'agent:teamagent:slack:direct:u12345678',
    senderId: 'U12345678', channelId: 'U12345678'};
  createCallerIdentityPlugin({env: {
    TEAMAGENT_CALLER_CLAIM_SECRET: 'x'.repeat(48), SLACK_TEAM_ID: 'T07MU5P2PBR',
    SEARCH_DIRECT_ANSWER: direct ? '1' : '0', SEARCH_DIRECT_ANSWER_ALLOWED_EMAILS: '*',
    SEARCH_DIRECT_ANSWER_KINDS: 'not_found',
  }}).register({registerInteractiveHandler() {}, logger: {info() {}, warn() {}},
    on: (name, fn) => hooks.set(name, fn)});
  hooks.get('message_received')({content, senderId: ctx.senderId,
    messageId: '1792041600.123456', runId: ctx.runId,
    metadata: {guildId: 'T07MU5P2PBR', to: `user:${ctx.senderId}`}},
  {...ctx, channelId: 'slack', conversationId: `user:${ctx.senderId}`, messageId: '1792041600.123456'});
  const remember = (data, toolName = 'teamagent__search', error = undefined) => hooks.get('after_tool_call')({
    toolName, runId: ctx.runId, error, result: {content: [{type: 'text', text: JSON.stringify(data)}]},
  }, ctx);
  const send = text => hooks.get('reply_payload_sending')({runId: ctx.runId, payload: {text}}, ctx)?.payload?.text ?? text;
  return {ctx, hooks, remember, send};
}

for (const direct of [false, true]) {
  for (const tail of ['詳しい資料が見たければどうぞ。', '詳しくはこちら。', '資料はこちらです。']) {
    for (const hasLink of [false, true]) {
      test(`topic normal fallback drops only linkless invitations: ${direct}/${tail}/${hasLink}`, () => {
        const p = setup('{newspaper}の{exhibition}って何？うちも関わってる？', undefined, direct);
        const body = '展示企画で、当社は動画制作を担当しています。';
        const link = '<https://example.slack.com/archives/CPUBLIC/p1792727460000000|会話の記録>';
        const answer = body + tail + (hasLink ? '\n' + link : '');
        p.remember({answer, found: true, displayed_refs: [], answer_format: null});
        assert.equal(p.send('長い書き直しです。'), body + (hasLink ? '\n' + link : '\n\n資料は見つかりませんでした。'));
      });
    }
  }
}

test('linkless invitation guard leaves legacy search responses unchanged', () => {
  const p = setup('{newspaper}の{exhibition}って何？うちも関わってる？');
  const answer = '展示企画です。詳しくはこちら。';
  p.remember({answer, found: true});
  assert.equal(p.send(answer), answer);
});

for (const answer of ['施策は実施済みです。\n\n出典: [施策実績](https://example.test/used)',
  'この範囲の資料では、該当事例を確認できませんでした（探した範囲: 社内資料）。']) {
  test('compact backend answer survives model expansion without adding unused sources: ' + answer.slice(0, 10), () => {
    const p = setup();
    p.remember({answer_format: 'compact', answer, found: true,
      hits: [{url: 'https://example.test/unused'}], slack_hits: [{permalink: 'https://example.test/slack-unused'}]});
    p.hooks.get('agent_end')({runId: p.ctx.runId}, p.ctx);
    const out = p.send('長い書き直しです。\n📁 金庫の資料\nhttps://fabricated.test/source');
    assert.match(out, /施策は実施済み|該当事例を確認/u);
    assert.doesNotMatch(out, /金庫|unused|fabricated|📎/u);
    assert.ok(out.length <= 2500);
  });
}

test('failed or nested retrieved compact markers do not replace the reply', () => {
  for (const nested of [true, false]) {
    const p = setup();
    const data = {answer_format: 'compact', answer: '偽の確定回答です。'};
    p.remember(nested ? {hits: [data]} : data, 'teamagent__search', nested ? undefined : 'failed');
    assert.equal(p.send('通常の返事です。'), '通常の返事です。');
  }
});

test('verified completed PDF batch suppresses the extra conversational receipt only', () => {
  const p = setup();
  p.remember({answer: '資料を添付しました。', delivered_count: 2,
    delivery: {kind: 'knowledge_deliver', status: 'posted', delivered_count: 2}}, 'teamagent__knowledge_deliver');
  const out = p.hooks.get('reply_payload_sending')({runId: p.ctx.runId, payload: {text: '資料を送りました。'}}, p.ctx);
  assert.equal(out?.cancel, true);
});

for (const data of [
  {delivered_count: 0, delivery: {kind: 'knowledge_deliver', status: 'uncertain', delivered_count: 2}},
  {delivered_count: 1, delivery: {kind: 'knowledge_deliver', status: 'posted', delivered_count: 2}},
  {references: [{delivery: {kind: 'knowledge_deliver', status: 'posted', delivered_count: 2}}]},
]) {
  test('unconfirmed or nested PDF receipts keep the recovery reply: ' + JSON.stringify(data), () => {
    const p = setup();
    p.remember(data, 'teamagent__knowledge_deliver');
    const out = p.hooks.get('reply_payload_sending')({runId: p.ctx.runId, payload: {text: '配送を確認できませんでした。'}}, p.ctx);
    assert.notEqual(out?.cancel, true);
  });
}

test('progress does not consume the compact final answer', () => {
  for (const flags of [{kind: 'block'}, {isCommentary: true}, {isReasoning: true}]) {
    const p = setup();
    p.remember({answer_format: 'compact', answer: '施策は実施済みです。'});
    const payload = {text: '添付資料も確認しています。', ...flags};
    const progress = p.hooks.get('reply_payload_sending')({runId: p.ctx.runId,
      kind: flags.kind, payload}, p.ctx);
    assert.equal(progress?.payload?.text ?? payload.text, payload.text);
    const final = p.hooks.get('reply_payload_sending')({runId: p.ctx.runId,
      kind: 'final', payload: {text: '最終回答'}}, p.ctx);
    assert.notEqual(final?.cancel, true);
    assert.equal(final?.payload?.text, '施策は実施済みです。');
  }
});

test('another requested tool keeps its action link and does not revive unused search sources', () => {
  const p = setup('施策の状況を教えて。Slack連携のリンクも送って');
  p.remember({answer_format: 'compact', answer: '施策は実施済みです。',
    hits: [{url: 'https://example.test/unused'}]});
  const url = 'https://connect.newstv.co.jp/slack/oauth/start/issued_token-09==';
  p.remember({slack_url: url}, 'teamagent__oauth_connect');
  assert.ok(p.send(`連携はこちらです。${url}`).includes(url));
  assert.doesNotMatch(p.send('施策実績と連携を確認しました。'), /unused|📎/u);
});

test('a requested action is preserved when its result arrives before search or after direct search delivery', () => {
  const url = 'https://connect.newstv.co.jp/slack/oauth/start/issued_token-09==';
  for (const directPosted of [false, true]) {
    const p = setup('施策の状況を教えて。Slack連携のリンクも送って');
    if (directPosted) p.remember({status: 'posted', delivered: true,
      delivery: {kind: 'search_direct_answer', answer_kind: 'not_found'}});
    p.remember({slack_url: url}, 'teamagent__oauth_connect');
    if (!directPosted) p.remember({answer_format: 'compact', answer: '施策は実施済みです。'});
    const text = `施策実績と連携リンクを確認しました。${url}`;
    const out = p.hooks.get('reply_payload_sending')({runId: p.ctx.runId, payload: {text}}, p.ctx);
    assert.notEqual(out?.cancel, true);
    assert.ok((out?.payload?.text ?? text).includes(url));
  }
});

test('a failed additional tool keeps its recovery reply instead of a stale search answer', () => {
  const p = setup('施策の状況を教えて。提案PDFも送って');
  p.remember({answer_format: 'compact', answer: '施策は実施済みです。',
    hits: [{url: 'https://example.test/unused'}]});
  p.remember({}, 'teamagent__knowledge_deliver', 'delivery failed');
  const text = '提案PDFの配送を確認できませんでした。';
  assert.equal(p.send(text), text);
});

test('a PDF receipt does not acknowledge the undelivered status answer requested alongside it', () => {
  for (const mode of [{answer_format: 'compact'}, {displayed_refs: []}]) {
    const p = setup('施策の状況を教えて。提案PDFも送って');
    p.remember({...mode, answer: '施策は実施済みです。'});
    p.remember({delivered_count: 2,
      delivery: {kind: 'knowledge_deliver', status: 'posted', delivered_count: 2}}, 'teamagent__knowledge_deliver');
    const text = '施策は実施済みです。';
    const out = p.hooks.get('reply_payload_sending')({runId: p.ctx.runId, payload: {text}}, p.ctx);
    assert.notEqual(out?.cancel, true);
    assert.equal(out?.payload?.text ?? text, text);
  }
});

test('a completed PDF batch suppresses its own extra receipt after the compact answer was selected', () => {
  const p = setup('施策の状況を教えて。提案PDFも送って');
  p.remember({answer_format: 'compact', answer: '施策は実施済みです。'});
  assert.equal(p.send('施策状況の回答です。'), '施策は実施済みです。');
  p.remember({delivered_count: 2,
    delivery: {kind: 'knowledge_deliver', status: 'posted', delivered_count: 2}}, 'teamagent__knowledge_deliver');
  assert.equal(p.hooks.get('reply_payload_sending')({runId: p.ctx.runId,
    payload: {text: 'PDFを送りました。'}}, p.ctx)?.cancel, true);
});

test('only extra final text chunks are suppressed; requested media is retained', () => {
  for (const attachment of [{mediaUrl: 'https://example.test/proposal.pdf'},
    {mediaUrls: ['https://example.test/proposal.pdf']}, {blocks: [{type: 'divider'}]},
    {attachments: [{text: '提案PDF'}]}]) {
    const p = setup('施策の状況を教えて。提案PDFも送って');
    p.remember({answer_format: 'compact', answer: '施策は実施済みです。'});
    p.send('施策状況の長い回答です。');
    const duplicate = p.hooks.get('reply_payload_sending')({runId: p.ctx.runId,
      payload: {text: '余分な資料一覧です。'}}, p.ctx);
    assert.equal(duplicate?.cancel, true);
    const payload = {text: '資料PDF', ...attachment};
    const out = p.hooks.get('reply_payload_sending')({runId: p.ctx.runId, payload}, p.ctx);
    assert.notEqual(out?.cancel, true);
    assert.deepEqual(out?.payload ?? payload, payload);
  }
});


test('case list stays five backend lines without OC recommendations', () => {
  const p = setup('ベビー用品の事例出して');
  const answer = Array.from({length: 5}, (_, i) => `案件${i}（2026-10-20）— ベビー用品の施策 [資料](https://example.test/case${i})`).join('\n');
  p.remember({answer_format: 'list', answer, found: true});
  const out = p.send('事例は1件です。\n\n💡 推奨アクション\n営業に紹介します。\n\n共感が効くパターンです。');
  assert.equal(out, answer);
});

for (const direct of [false, true]) {
  for (const count of [0, 1, 5]) {
    test(`Drive case list excludes model feedback and extra source links: direct=${direct}, count=${count}`, () => {
      const p = setup('食品メーカーのショート動画の事例出して', undefined, direct);
      const answer = count ? Array.from({length: count}, (_, i) =>
        `*食品会社${i}*（2026年9月）\n• ショート動画を配信。\n• 結果を確認。\n• <https://drive.google.com/file/d/FILE${i}/view|資料を見る>`
      ).join('\n\n') : 'Drive に資料のある事例は見つかりませんでした。';
      const slack = 'https://vector-workspcae.slack.com/archives/CFOOD/p1234567890123456';
      p.remember({answer_format: 'list', answer, found: count > 0,
        hits: [{url: 'https://example.test/app'}], slack_hits: [{permalink: slack}]});
      const out = p.send(`${answer}\n\n営業フィードバック\n温度感B <${slack}|詳細>\n\nAico Vault https://example.test/app`);
      assert.equal(out, answer);
      assert.doesNotMatch(out, /slack\.com|営業フィードバック|Vault/u);
    });
  }
}

test('fact reply removes advice added by OC', () => {
  const p = setup('撮影日はいつ？');
  p.remember({answer: '撮影日は10月20日です。', found: true});
  assert.equal(p.send('撮影日は10月20日です。\n\n💡 推奨アクション\n確認しましょう。\n\n共感が効くパターンです。'), '撮影日は10月20日です。');
});

for (const query of ['JAL の過去提案で何が効いた？', 'JAL の過去提案で効いたのは？']) {
  test('what worked keeps analysis without unsolicited advice: ' + query, () => {
    const p = setup(query);
    const answer = '提案は採用されています。\n\n💡 推奨アクション\n同じ条件で試す。';
    p.remember({answer, found: true});
    assert.equal(p.send(answer), '提案は採用されています。');
  });
}


test('receipt thread from trusted Slack context is signed for omiyage', () => {
  const p = setup('JTBとHISのお土産資料を作って', '1792461784.695389');
  const toolName = 'teamagent__omiyage_report_submit', toolCallId = 'receipt-thread-call';
  const result = p.hooks.get('before_tool_call')({toolName, toolCallId, runId: p.ctx.runId, params: {}}, {...p.ctx, toolName, toolCallId});
  assert.equal(result?.params?._user_context?.thread_ts, '1792461784.695389');
  const claim = JSON.parse(Buffer.from(result.params._user_context.caller_claim.split('.')[0], 'base64url'));
  assert.equal(claim.thread, '1792461784.695389');
});

for (const direct of [false, true]) {
  test(`Drive-first compact preserves backend body and used links with direct=${direct}`, () => {
    const p = setup('提案の進め方は？', undefined, direct);
    const url = 'https://drive.google.com/file/d/USED/view';
    const answer = `進行表を先に共有します。\n\n<${url}|資料を見る>`;
    p.remember({answer_format: 'compact', answer, found: true,
      displayed_refs: [{kind: 'drive', drive_file_id: 'USED', url}],
      hits: [{url: 'https://drive.google.com/file/d/UNUSED/view'}],
      slack_hits: [{permalink: 'https://example.slack.com/archives/C1/p1234567890123456'}]});
    const out = p.send('進行表を先に共有します。 https://example.slack.com/archives/C1/p1234567890123456');
    assert.equal(out, answer);
    assert.equal(out.split(url).length - 1, 1);
  });
}

for (const direct of [false, true]) {
  for (const repeat of [1, 100]) {
    test(`Drive normal fallback survives without unused inventory: direct=${direct}, repeat=${repeat}`, () => {
      const p = setup('食品業界の施策の進め方は？', undefined, direct);
      const answer = '食品の施策では配信面と出演者を組み合わせています。'.repeat(repeat);
      p.remember({answer, found: true, displayed_refs: [],
        hits: [{url: 'https://drive.google.com/file/d/UNUSED/view'}],
        slack_hits: [{permalink: 'https://example.slack.com/archives/C1/p1234567890123456'}]});
      assert.equal(p.send('未使用資料の一覧です。 https://drive.google.com/file/d/UNUSED/view'), answer);
    });
  }
}

for (const direct of [false, true]) {
  for (const hasDrive of [false, true]) {
    test(`Drive case cards suppress Slack additions: direct=${direct}, hasDrive=${hasDrive}`, () => {
      const p = setup('食品メーカーのショート動画の事例出して', undefined, direct);
      const url = 'https://drive.google.com/file/d/USED/view';
      const answer = hasDrive ? `*食品会社A*（2026年9月）\n• 紹介動画を配信。\n• <${url}|資料を見る>`
        : 'Drive に資料のある事例は見つかりませんでした。';
      p.remember({answer, answer_format: 'list', found: hasDrive, hits: [], slack_hits: [],
        displayed_refs: hasDrive ? [{kind: 'drive', drive_file_id: 'USED', url}] : []});
      assert.equal(p.send(`${answer}\nSlack からの事例共有: 別会社の商品`), answer);
    });
  }
  test(`Drive normal fallback preserves used link: direct=${direct}`, () => {
    const p = setup('TikTokの保存率を上げるコツは？', undefined, direct);
    const url = 'https://drive.google.com/file/d/USED/view';
    const answer = `食品会社Aでは商品紹介を工夫しています。\n\n<${url}|資料を見る>`;
    p.remember({answer, found: true, answer_format: null,
      displayed_refs: [{kind: 'drive', drive_file_id: 'USED', url}]});
    assert.equal(p.send('保存を促すポイントを説明します。'), answer);
  });
}

for (const syntax of ['slack', 'markdown']) {
  test(`drive-first never labels a Slack thread as a material: ${syntax}`, () => {
    const p = setup('食品系商品の星空堂って会社に行く。事例ある？');
    const url = 'https://example.slack.com/archives/CPUBLIC/p1792727460000000';
    const link = syntax === 'slack' ? `<${url}|0824_提案資料>` : `[0824_提案資料](${url})`;
    p.remember({answer: `投稿に資料があります。資料: ${link}`, found: true,
      displayed_refs: [], answer_format: null});
    const out = p.send(`投稿に資料があります。資料: ${link}`);
    assert.doesNotMatch(out, /\|0824_提案資料>|\[0824_提案資料\]\(/u);
    assert.match(out, /0824_提案資料/u);
  });
}

for (const tail of ['正式社名を確認してみてください。', '商材名を確認すると見つかるかもしれません。']) {
  test('Drive fallback never closes by asking the user to verify: ' + tail, () => {
    const p = setup('星空堂って会社に行く。事例ある？');
    const body = '星空堂との過去案件は見つかりませんでした。';
    const link = '<https://drive.google.com/file/d/SAVED/view|資料を見る>';
    p.remember({answer: `${body}\n${link}\n${tail}`, found: true,
      displayed_refs: [], answer_format: null});
    const out = p.send('モデルが書き直しました。');
    assert.ok(out.includes(body) && out.includes(link));
    assert.ok(!out.includes(tail));
  });
}

test('legacy search leaves material-shaped Slack links alone', () => {
  const p = setup();
  const text = '<https://example.slack.com/archives/CPUBLIC/p1792727460000000|0824_提案資料>';
  p.remember({answer: text, found: true});
  assert.equal(p.send(text), text);
});

for (const link of ['', '<https://drive.google.com/file/d/USED/view|資料を見る>', '<https://example.slack.com/archives/C1/p1234567890123456|会話の記録>', 'https://example.test/app']) {
  for (const tail of ['詳しい資料が必要でしたらお知らせください。', '詳しい資料が見たいときは、お知らせください。', '提案資料や詳細が必要でしたら、声をかけてください。']) {
    test(`PR3 invitation removed with original, conversation or Vault: ${link}/${tail}`, () => {
      const p = setup('社内でいう動画商品とは？');
      p.remember({answer: '配信サービスです。' + tail + (link ? '\n' + link : ''), found: true, displayed_refs: []});
      const out = p.send('書き直した説明です。');
      assert.doesNotMatch(out, /お知らせ|声をかけ/u);
      assert.equal(out.includes('資料は見つかりませんでした。'), !link);
      if (link) assert.ok(out.includes(link));
    });
  }
}

for (const question of ['どの期間の数字か、お知らせください。', '決まったら声をかけてください。']) {
  test(`PR3 invitation guard keeps clarifying question: ${question}`, () => {
    const p = setup('社内でいう動画商品とは？');
    p.remember({answer: '配信サービスです。' + question, found: true, displayed_refs: []});
    const out = p.send('配信サービスです。' + question);
    assert.ok(out.includes(question));
    assert.ok(!out.includes('資料は見つかりませんでした。'));
  });
}

test('PR3 comparison analysis keeps explanation and strips unsolicited next action', () => {
  const p = setup('比較して何が効いた？');
  const body = '冒頭で完成形を示した点が効いたと考えられます。';
  p.remember({answer: body, found: true});
  assert.equal(p.send(body + '\n\n:alarm_clock: ネクストアクション\n同条件での配信が推奨です。'), body);
});

for (const link of ['<https://drive.google.com/file/d/USED/view|資料を見る>', '[資料を見る](https://drive.google.com/file/d/USED/view)', 'https://drive.google.com/file/d/USED/view']) {
  test(`PR3 invitation removal preserves the source in the same sentence: ${link}`, () => {
    const p = setup('社内でいう動画商品とは？');
    p.remember({answer: '配信サービスです。詳しい資料が必要でしたらお知らせください ' + link + '。', found: true, displayed_refs: []});
    const out = p.send('説明です。');
    assert.ok(out.includes('https://drive.google.com/file/d/USED/view'));
    assert.doesNotMatch(out, /お知らせ|資料は見つかりませんでした/u);
  });
}

for (const [id, query, body, tail] of [
  ['list_cases', '青空社の事例を一覧で出して', '青空社の動画施策です。', '詳しい資料が見たいときは、お知らせください。'],
  ['internal_term', '社内でいう動画商品とは？', '動画商品は配信サービスです。', '詳しい資料が必要でしたらお知らせください。'],
  ['unjoined_channel_topic', '展示企画って何？うちも関わってる？', '展示企画の広報を担当しました。', '提案資料や詳細が必要でしたら、お知らせください。'],
  ['delivery_kanji', '宅配サービスの事例は？', '宅配サービスの提案です。', '詳しい資料が必要でしたら声をかけてください。'],
  ['completed_status', '青空社の施策は今どうなっている？', '青空社の施策は完了しました。', '提案資料や詳細が必要でしたら、お知らせください。'],
]) {
  test(`PR3 batch-11 anonymous ${id} sends its original without an invitation`, () => {
    const p = setup(query);
    const url = 'https://drive.google.com/file/d/USED/view';
    const link = `<${url}|資料を見る>`;
    p.remember({answer: body + tail + '\n' + link, found: true,
      ...(id === 'list_cases' ? {answer_format: 'list'} : {}),
      displayed_refs: [{kind: 'drive', drive_file_id: 'USED', url}]});
    assert.equal(p.send('モデルが追加した説明です。'), body + '\n' + link);
  });
}

for (const tail of ['詳しい内容をご覧になりたい場合は、お知らせください。', '何か他に知りたいことはありますか？']) {
  test(`eval12 final guard with MCP drive receipt and no OC drive env: ${tail}`, () => {
    const p = setup('社内でいうビデオリリースって何？');
    p.remember({answer: '配信サービスです。' + tail, found: true, displayed_refs: []});
    const out = p.send('モデルの下書き。');
    assert.match(out, /配信サービスです/u);
    assert.doesNotMatch(out, /お知らせ|ありますか/u);
  });
}
