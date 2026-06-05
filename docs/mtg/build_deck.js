/* TeamAgent — AI紹介MTG デッキ生成（pptxgenjs / LAYOUT_WIDE 13.33x7.5）
   フォント: Hiragino Sans（macOS）。アイコンは react-icons → PNG。
*/
const pptxgen = require("pptxgenjs");
const React = require("react");
const RDS = require("react-dom/server");
const sharp = require("sharp");
const FA = require("react-icons/fa6");
const FA5 = require("react-icons/fa");

// ---------- palette ----------
const INK = "0E1430";      // dark navy bg
const INK2 = "1A2350";     // panel navy
const CYAN = "22D3EE";     // primary accent (bright cyan)
const CYAN_D = "0E7490";   // deep cyan (text on light)
const VIOLET = "8B7CFF";   // secondary
const VIOLET_D = "5B53C6";
const CORAL = "FB7185";    // sharp accent (video)
const CORAL_D = "E11D48";
const AMBER = "F59E0B";    // in-dev / highlight
const GREEN = "10B981";    // security/quality
const PAPER = "FFFFFF";
const TXT = "1B2350";      // dark text on light
const MUTE = "667089";     // muted gray
const LINE = "E5E9F2";     // light divider
const CARDBG = "F7F9FC";   // very light card
const FONT = "Hiragino Sans";

const W = 13.33, H = 7.5, MX = 0.7;

// ---------- icon cache ----------
const _cache = {};
async function ico(Comp, color, size = 256) {
  if (!Comp) return null;
  const key = (Comp.name || "x") + color + size;
  if (_cache[key]) return _cache[key];
  const svg = RDS.renderToStaticMarkup(React.createElement(Comp, { color: "#" + color, size: String(size) }));
  const png = await sharp(Buffer.from(svg)).png().toBuffer();
  const d = "image/png;base64," + png.toString("base64");
  _cache[key] = d;
  return d;
}
const shadow = () => ({ type: "outer", color: "1B2350", blur: 9, offset: 3, angle: 90, opacity: 0.12 });

// icon component picks (guarded)
const I = {
  brain: FA.FaBrain, search: FA.FaMagnifyingGlass, chat: FA.FaComments, bolt: FA.FaBolt,
  file: FA.FaFileLines, pen: FA.FaPenToSquare, card: FA.FaIdCard, log: FA.FaClipboardList,
  video: FA.FaVideo, film: FA.FaFilm, chart: FA.FaChartLine, check: FA.FaCircleCheck,
  shield: FA.FaShieldHalved, lock: FA.FaLock, users: FA.FaUsers, arrow: FA.FaArrowRight,
  rocket: FA.FaRocket, wand: FA.FaWandMagicSparkles, server: FA.FaServer, db: FA.FaDatabase,
  slack: FA5.FaSlack, tiktok: FA.FaTiktok, route: FA.FaCodeBranch, eye: FA.FaEye,
  layers: FA.FaLayerGroup, clock: FA.FaClock, link: FA.FaLink, gauge: FA.FaGaugeHigh,
  quote: FA.FaQuoteLeft, star: FA.FaStar, hand: FA.FaHandshake, list: FA.FaListCheck,
};

(async () => {
  const p = new pptxgen();
  p.defineLayout({ name: "WIDE", width: W, height: H });
  p.layout = "WIDE";
  p.author = "TeamAgent";
  p.title = "TeamAgent AI紹介MTG";

  // ---------- helpers ----------
  const T = (s, t, o) => s.addText(t, { fontFace: FONT, ...o });
  const RECT = p.shapes.RECTANGLE, ROUND = p.shapes.ROUNDED_RECTANGLE, OVAL = p.shapes.OVAL, LN = p.shapes.LINE;

  function footer(s, n) {
    T(s, "TeamAgent ／ ベクトル社・社内AIエージェント", { x: MX, y: 7.05, w: 8, h: 0.3, fontSize: 9, color: MUTE });
    T(s, String(n), { x: W - 1.1, y: 7.05, w: 0.5, h: 0.3, fontSize: 9, color: MUTE, align: "right" });
  }
  function kicker(s, txt, color) {
    T(s, txt, { x: MX, y: 0.44, w: 11, h: 0.3, fontSize: 12, bold: true, color: color || CYAN_D, charSpacing: 2 });
  }
  function title(s, txt) {
    T(s, txt, { x: MX, y: 0.66, w: W - 2 * MX, h: 0.9, fontSize: 30, bold: true, color: TXT, fontFace: FONT });
  }
  async function iconCircle(s, x, y, d, comp, circleColor, iconColor) {
    s.addShape(OVAL, { x, y, w: d, h: d, fill: { color: circleColor }, line: { type: "none" } });
    const data = await ico(comp, iconColor || "FFFFFF");
    if (data) s.addImage({ data, x: x + d * 0.24, y: y + d * 0.24, w: d * 0.52, h: d * 0.52 });
  }
  function card(s, x, y, w, h, fill) {
    s.addShape(ROUND, { x, y, w, h, rectRadius: 0.09, fill: { color: fill || PAPER }, line: { color: LINE, width: 1 }, shadow: shadow() });
  }

  // =========================================================
  // 1. TITLE (dark)
  // =========================================================
  let s = p.addSlide(); s.background = { color: INK };
  // faint accent shapes
  s.addShape(OVAL, { x: 10.2, y: -1.6, w: 5.2, h: 5.2, fill: { color: INK2 }, line: { type: "none" } });
  s.addShape(OVAL, { x: 11.6, y: 3.9, w: 3.6, h: 3.6, fill: { color: "16204A" }, line: { type: "none" } });
  await iconCircle(s, MX, 1.5, 0.92, I.brain, CYAN, INK);
  T(s, "TeamAgent", { x: MX, y: 2.65, w: 11, h: 1.3, fontSize: 60, bold: true, color: PAPER });
  T(s, [
    { text: "営業の", options: { color: "C7D2FE" } },
    { text: "“会社の脳”", options: { color: CYAN, bold: true } },
    { text: " になる Slack AI エージェント", options: { color: "C7D2FE" } },
  ], { x: MX, y: 3.95, w: 11.5, h: 0.6, fontSize: 23, fontFace: FONT });
  // chips
  const chips = ["横断検索の脳", "提案まで作る", "動画を読む", "雑談もOK"];
  let cx = MX;
  for (const c of chips) {
    const cw = 0.42 + c.length * 0.26;
    s.addShape(ROUND, { x: cx, y: 4.85, w: cw, h: 0.5, rectRadius: 0.25, fill: { color: INK2 }, line: { color: "33407A", width: 1 } });
    T(s, c, { x: cx, y: 4.85, w: cw, h: 0.5, fontSize: 12.5, color: "DBE3FF", align: "center", valign: "middle" });
    cx += cw + 0.25;
  }
  T(s, "2026.06  AI紹介MTG ／ ベクトル株式会社", { x: MX, y: 6.4, w: 9, h: 0.4, fontSize: 14, color: "8893C4" });
  T(s, "営業16名規模 ・ 同時利用4名／最大20接続を想定", { x: MX, y: 6.78, w: 9, h: 0.35, fontSize: 11, color: "5C679B" });

  // =========================================================
  // 2. これは何？ (light)
  // =========================================================
  s = p.addSlide(); s.background = { color: PAPER };
  kicker(s, "WHAT IS IT");
  title(s, "一言でいうと");
  card(s, MX, 1.75, 7.1, 1.5, INK);
  T(s, [
    { text: "Slackで ", options: { color: "C7D2FE" } },
    { text: "@メンションするだけ。", options: { color: CYAN, bold: true } },
    { text: "営業メンバーのナレッジを", options: { color: "FFFFFF" }, breakLine: true },
    { text: "一つの脳に束ね、", options: { color: "FFFFFF" } },
    { text: "案件相談から雑談まで", options: { color: CYAN, bold: true } },
    { text: "答える相棒。", options: { color: "FFFFFF" } },
  ], { x: MX + 0.35, y: 1.95, w: 6.5, h: 1.1, fontSize: 18, fontFace: FONT, lineSpacingMultiple: 1.15, valign: "middle" });

  const rows2 = [
    [I.brain, CYAN_D, "全員の知見を、全員に", "14名分の提案書・議事録・Slackを横断して回答"],
    [I.wand, VIOLET_D, "検索だけじゃない", "提案のたたき台づくり・レビュー・動画分析まで"],
    [I.chat, CORAL_D, "コマンド不要", "自然文でOK。雑談は会話、依頼は実行に振り分け"],
  ];
  let yy = 3.55;
  for (const [comp, col, head, body] of rows2) {
    await iconCircle(s, MX, yy, 0.7, comp, col);
    T(s, head, { x: MX + 0.95, y: yy - 0.04, w: 6.2, h: 0.4, fontSize: 16, bold: true, color: TXT });
    T(s, body, { x: MX + 0.95, y: yy + 0.36, w: 6.4, h: 0.4, fontSize: 12.5, color: MUTE });
    yy += 1.05;
  }
  // right visual: stacked "knowledge" tiles into brain
  card(s, 8.5, 1.75, 4.15, 4.9, CARDBG);
  await iconCircle(s, 10.05, 2.15, 1.0, I.brain, CYAN, INK);
  T(s, "会社の脳", { x: 8.5, y: 3.25, w: 4.15, h: 0.4, fontSize: 16, bold: true, color: TXT, align: "center" });
  const feeds = ["提案書 / 企画書", "Slackの商談スレッド", "Google Drive 資料", "議事録・メール"];
  let fy = 3.9;
  for (const f of feeds) {
    s.addShape(ROUND, { x: 8.95, y: fy, w: 3.25, h: 0.52, rectRadius: 0.08, fill: { color: PAPER }, line: { color: LINE, width: 1 } });
    T(s, f, { x: 9.15, y: fy, w: 2.9, h: 0.52, fontSize: 12.5, color: TXT, valign: "middle" });
    fy += 0.66;
  }
  footer(s, 2);

  // =========================================================
  // 3. 課題 → 価値 (Before/After, light)
  // =========================================================
  s = p.addSlide(); s.background = { color: PAPER };
  kicker(s, "WHY NOW", CORAL_D);
  title(s, "なぜ必要か — 散らばった知見を“会社の脳”へ");
  // before card
  card(s, MX, 1.85, 5.75, 4.8, CARDBG);
  T(s, "いま起きていること", { x: MX + 0.35, y: 2.05, w: 5, h: 0.45, fontSize: 16, bold: true, color: MUTE });
  const before = [
    "提案ナレッジが個人に分散し、再利用できない",
    "過去の“勝ち筋”が埋もれて探せない",
    "競合のショート動画分析が毎回手作業",
    "新人・異動者の立ち上がりに時間がかかる",
  ];
  let by = 2.7;
  for (const b of before) {
    s.addShape(OVAL, { x: MX + 0.35, y: by + 0.07, w: 0.16, h: 0.16, fill: { color: MUTE }, line: { type: "none" } });
    T(s, b, { x: MX + 0.65, y: by - 0.05, w: 4.9, h: 0.55, fontSize: 13.5, color: TXT, valign: "middle" });
    by += 0.92;
  }
  // arrow
  await iconCircle(s, 6.62, 3.9, 0.7, I.arrow, CYAN_D, "FFFFFF");
  // after card (dark)
  card(s, 7.55, 1.85, 5.1, 4.8, INK);
  T(s, "TeamAgent があると", { x: 7.9, y: 2.05, w: 4.6, h: 0.45, fontSize: 16, bold: true, color: CYAN });
  const after = [
    ["全員の知見を全員が即活用", I.brain],
    ["根拠（出典リンク）付きで回答", I.quote],
    ["KWを入れるだけで動画を分析", I.chart],
    ["自然文だから誰でも使える", I.chat],
  ];
  let ay = 2.7;
  for (const [t, comp] of after) {
    await iconCircle(s, 7.9, ay, 0.56, comp, INK2, CYAN);
    T(s, t, { x: 8.62, y: ay - 0.05, w: 3.9, h: 0.66, fontSize: 13.5, color: "FFFFFF", valign: "middle" });
    ay += 0.92;
  }
  footer(s, 3);

  // =========================================================
  // 4. 全体像 (light)
  // =========================================================
  s = p.addSlide(); s.background = { color: PAPER };
  kicker(s, "HOW IT WORKS");
  title(s, "全体像 — メンションから回答＋提案まで");
  // flow row
  const flow = [
    [I.slack, "Slackで@メンション", "自然文 / コマンド不要", CYAN_D],
    [I.route, "AIルーターが意図判定", "雑談 / 検索 / 各Skillへ", VIOLET_D],
    [I.layers, "9つのSkillが実行", "横断検索・提案・動画ほか", CORAL_D],
    [I.check, "回答＋その他の提案", "根拠リンク付きで返す", GREEN],
  ];
  let fx = MX;
  const fw = 2.72, gap = 0.32;
  for (let i = 0; i < flow.length; i++) {
    const [comp, head, body, col] = flow[i];
    card(s, fx, 1.95, fw, 2.15, PAPER);
    await iconCircle(s, fx + fw / 2 - 0.42, 2.2, 0.84, comp, col);
    T(s, head, { x: fx + 0.1, y: 3.15, w: fw - 0.2, h: 0.55, fontSize: 14, bold: true, color: TXT, align: "center" });
    T(s, body, { x: fx + 0.1, y: 3.66, w: fw - 0.2, h: 0.4, fontSize: 11, color: MUTE, align: "center" });
    if (i < flow.length - 1) {
      const ad = await ico(I.arrow, "B7C0D8");
      if (ad) s.addImage({ data: ad, x: fx + fw + gap / 2 - 0.16, y: 2.85, w: 0.32, h: 0.32 });
    }
    fx += fw + gap;
  }
  // skills strip
  T(s, "9つのSkill", { x: MX, y: 4.55, w: 4, h: 0.4, fontSize: 14, bold: true, color: TXT });
  const skillChips = [
    ["横断検索", CYAN_D], ["取引先カルテ", CYAN_D], ["提案ドラフト", VIOLET_D], ["提案レビュー", VIOLET_D],
    ["営業ログ化", VIOLET_D], ["VSEO動画分析", CORAL_D], ["動画分析", CORAL_D], ["動画チェック（制作中）", AMBER], ["TikTok検索", CORAL_D],
  ];
  let scx = MX, scy = 5.05;
  for (const [t, col] of skillChips) {
    const cw = 0.5 + t.length * 0.235;
    if (scx + cw > W - MX) { scx = MX; scy += 0.66; }
    s.addShape(ROUND, { x: scx, y: scy, w: cw, h: 0.5, rectRadius: 0.25, fill: { color: CARDBG }, line: { color: col, width: 1 } });
    await iconCircle(s, scx + 0.1, scy + 0.1, 0.3, I.bolt, col);
    T(s, t, { x: scx + 0.46, y: scy, w: cw - 0.5, h: 0.5, fontSize: 11.5, color: TXT, valign: "middle" });
    scx += cw + 0.22;
  }
  footer(s, 4);

  // =========================================================
  // 5. SECTION DIVIDER ① (dark)
  // =========================================================
  function divider(num, big, sub, accent) {
    const d = p.addSlide(); d.background = { color: INK };
    d.addShape(RECT, { x: 0, y: 0, w: 0.18, h: H, fill: { color: accent }, line: { type: "none" } });
    T(d, num, { x: MX, y: 2.05, w: 3, h: 1.4, fontSize: 90, bold: true, color: accent });
    T(d, big, { x: MX, y: 3.5, w: 11.5, h: 1.0, fontSize: 40, bold: true, color: PAPER });
    T(d, sub, { x: MX + 0.03, y: 4.55, w: 11, h: 0.6, fontSize: 17, color: "9AA6D6" });
    return d;
  }
  divider("01", "会社の脳 — 横断検索エージェント", "営業メンバーの知見を束ね、根拠付きで“教える”", CYAN);

  // =========================================================
  // 6. 会社の脳① 横断検索 (light)
  // =========================================================
  s = p.addSlide(); s.background = { color: PAPER };
  kicker(s, "CORE ①  COMPANY BRAIN");
  title(s, "横断検索 — 「響いた訴求は？」に根拠付きで答える");
  // example bubble
  card(s, MX, 1.85, 6.0, 1.15, INK2);
  await iconCircle(s, MX + 0.28, 2.12, 0.6, I.chat, INK, CYAN);
  T(s, "@TeamAgent  A社の提案で響いた訴求は？", { x: MX + 1.05, y: 1.85, w: 4.8, h: 1.15, fontSize: 14.5, color: "FFFFFF", valign: "middle", fontFace: FONT });
  // how it works rows
  const brainRows = [
    [I.search, "ベクトル検索（pgvector）", "意味で近い資料を全社から top-30 抽出"],
    [I.gauge, "Cohere Rerank で精度UP", "本当に関連する5件へ絞り込み（東京リージョン）"],
    [I.quote, "引用付き・反ハルシ", "根拠が弱ければ“記載なし”。出典リンクで確認可"],
    [I.shield, "RLSで権限分離", "本人が見てよい資料だけを対象（情報漏洩を防ぐ）"],
  ];
  let bry = 3.25;
  for (const [comp, head, body] of brainRows) {
    await iconCircle(s, MX, bry, 0.62, comp, CYAN_D);
    T(s, head, { x: MX + 0.85, y: bry - 0.06, w: 5.3, h: 0.4, fontSize: 14.5, bold: true, color: TXT });
    T(s, body, { x: MX + 0.85, y: bry + 0.32, w: 5.4, h: 0.45, fontSize: 12, color: MUTE });
    bry += 0.85;
  }
  // right: stat callouts
  card(s, 7.35, 1.85, 5.28, 4.8, CARDBG);
  T(s, "リランク導入の効果（社内goldセット）", { x: 7.6, y: 2.05, w: 4.9, h: 0.4, fontSize: 13, bold: true, color: MUTE });
  // big stat
  T(s, [
    { text: "20% ", options: { color: MUTE, fontSize: 26, bold: true } },
    { text: "→ ", options: { color: MUTE, fontSize: 26 } },
    { text: "64%", options: { color: CYAN_D, fontSize: 54, bold: true } },
  ], { x: 7.6, y: 2.5, w: 4.9, h: 1.0, fontFace: FONT, valign: "middle" });
  T(s, "一発正答率（top-1）", { x: 7.6, y: 3.55, w: 4.9, h: 0.35, fontSize: 12, color: MUTE });
  // second stat
  s.addShape(LN, { x: 7.6, y: 4.15, w: 4.78, h: 0, line: { color: LINE, width: 1 } });
  T(s, [
    { text: "88", options: { color: VIOLET_D, fontSize: 46, bold: true } },
    { text: "%", options: { color: VIOLET_D, fontSize: 28, bold: true } },
  ], { x: 7.6, y: 4.35, w: 2.3, h: 0.9, fontFace: FONT, valign: "middle" });
  T(s, "実データ9,400件での\ntop-5 ヒット率", { x: 9.95, y: 4.4, w: 2.6, h: 0.9, fontSize: 12.5, color: TXT, valign: "middle" });
  T(s, "※ 根拠なき断定をしない設計（grounded）。ハルシネーション0を実データで確認。", { x: 7.6, y: 5.5, w: 4.9, h: 0.9, fontSize: 11, color: MUTE, italic: true, valign: "top" });
  footer(s, 6);

  // =========================================================
  // 7. 会社の脳② 会話の出し分け (light)  ← 今回のアップデート
  // =========================================================
  s = p.addSlide(); s.background = { color: PAPER };
  kicker(s, "CORE ①  CONVERSATION");
  title(s, "“検索する道具”から“提案する相棒”へ（今回更新）");
  const convo = [
    ["雑談・挨拶", CORAL_D, "こんにちは！", "こんにちは。案件のことなど、何でも話しかけてください。", "→ 検索せず、会話で即答"],
    ["依頼（検索）", CYAN_D, "飲食店のPR事例を教えて", "🔎 受け付けました。『飲食店のPR事例』について検索します（資料を探索中…）", "→ 何を調べるか“話題を復唱”"],
    ["回答のあと", VIOLET_D, "（検索結果）", "💡 その他の提案：カルテ作成／提案のたたき台／競合動画の分析…", "→ 次の一手まで提案する"],
  ];
  let cvy = 1.85;
  for (const [tag, col, user, bot, note] of convo) {
    // tag
    s.addShape(ROUND, { x: MX, y: cvy + 0.15, w: 1.7, h: 0.55, rectRadius: 0.1, fill: { color: col }, line: { type: "none" } });
    T(s, tag, { x: MX, y: cvy + 0.15, w: 1.7, h: 0.55, fontSize: 13, bold: true, color: "FFFFFF", align: "center", valign: "middle" });
    // user bubble
    s.addShape(ROUND, { x: 2.55, y: cvy, w: 3.6, h: 0.85, rectRadius: 0.12, fill: { color: CARDBG }, line: { color: LINE, width: 1 } });
    T(s, user, { x: 2.73, y: cvy, w: 3.25, h: 0.85, fontSize: 12.5, color: TXT, valign: "middle", fontFace: FONT });
    // bot bubble
    s.addShape(ROUND, { x: 6.35, y: cvy, w: 4.5, h: 0.85, rectRadius: 0.12, fill: { color: INK2 }, line: { type: "none" } });
    T(s, bot, { x: 6.53, y: cvy, w: 4.15, h: 0.85, fontSize: 11.5, color: "FFFFFF", valign: "middle", fontFace: FONT });
    // note
    T(s, note, { x: 10.98, y: cvy, w: 1.65, h: 0.85, fontSize: 10.5, color: col, bold: true, valign: "middle" });
    cvy += 1.25;
  }
  card(s, MX, 5.75, W - 2 * MX, 0.95, CARDBG);
  await iconCircle(s, MX + 0.3, 5.95, 0.55, I.bolt, AMBER);
  T(s, [
    { text: "ポイント：", options: { bold: true, color: TXT } },
    { text: "「Hello」と打つと検索受付が返る不自然さを解消。雑談は会話で、依頼は“話題を復唱＋提案”で返すよう改善しました。", options: { color: TXT } },
  ], { x: MX + 1.05, y: 5.75, w: 10.6, h: 0.95, fontSize: 13, valign: "middle", fontFace: FONT });
  footer(s, 7);

  // =========================================================
  // 8. 資料化・提案支援 (2x2, light)
  // =========================================================
  s = p.addSlide(); s.background = { color: PAPER };
  kicker(s, "CORE ①  PRODUCTIVITY", VIOLET_D);
  title(s, "資料化・提案支援 — 営業の手を止めない");
  const grid = [
    [I.file, VIOLET_D, "提案ドラフト自動生成", "過去提案を参照し、たたき台を自動で作成。ゼロから書かない。"],
    [I.pen, VIOLET_D, "提案レビュー・改善", "既存提案を診断し、刺さる訴求・抜けを指摘して磨き込む。"],
    [I.card, CYAN_D, "取引先カルテ", "履歴・温度感・次アクションを時系列で1枚に束ねる。"],
    [I.log, GREEN, "営業ログ化（CRM）", "Slackの商談会話を、フェーズ/次ステップ/BANTに構造化。"],
  ];
  const gw = 5.80, gh = 2.05, gx0 = MX, gy0 = 1.95, gxg = 0.33, gyg = 0.3;
  for (let i = 0; i < grid.length; i++) {
    const [comp, col, head, body] = grid[i];
    const gx = gx0 + (i % 2) * (gw + gxg);
    const gyc = gy0 + Math.floor(i / 2) * (gh + gyg);
    card(s, gx, gyc, gw, gh, PAPER);
    await iconCircle(s, gx + 0.35, gyc + 0.38, 0.92, comp, col);
    T(s, head, { x: gx + 1.5, y: gyc + 0.35, w: gw - 1.7, h: 0.5, fontSize: 17, bold: true, color: TXT });
    T(s, body, { x: gx + 1.5, y: gyc + 0.95, w: gw - 1.7, h: 0.9, fontSize: 12.5, color: MUTE, valign: "top" });
  }
  footer(s, 8);

  // =========================================================
  // 9. SECTION DIVIDER ② (dark)
  // =========================================================
  divider("02", "動画を読むAI — VSEO / 動画分析", "ショート動画の“なぜ伸びるか”を構造で可視化", CORAL);

  // =========================================================
  // 10. VSEO 看板機能 (light)
  // =========================================================
  s = p.addSlide(); s.background = { color: PAPER };
  kicker(s, "FLAGSHIP  VSEO", CORAL_D);
  title(s, "VSEO動画分析 — KWひとつで“勝ち筋”を構造化");
  card(s, MX, 1.8, 5.0, 0.95, INK2);
  await iconCircle(s, MX + 0.25, 2.0, 0.55, I.chart, INK, CORAL);
  T(s, "@TeamAgent  VSEO分析 新宿 ランチ", { x: MX + 0.95, y: 1.8, w: 3.9, h: 0.95, fontSize: 14, color: "FFFFFF", valign: "middle", fontFace: FONT });
  // 4-step process (vertical)
  const steps = [
    ["TikTok上位動画を収集", "検索KWの入賞動画を自動取得（目標+4本を確保）"],
    ["1本ずつ時刻付きで分析", "Geminiが構成・テロップ・フックを秒単位で読む"],
    ["横断で勝ち筋を抽出", "共通パターンを統計＋戦略シンセシス"],
    ["HTMLレポートで納品", "Premiere風タイムライン／実動画再生つき"],
  ];
  let sy = 3.05;
  for (let i = 0; i < steps.length; i++) {
    const [head, body] = steps[i];
    s.addShape(OVAL, { x: MX, y: sy, w: 0.6, h: 0.6, fill: { color: CORAL }, line: { type: "none" } });
    T(s, String(i + 1), { x: MX, y: sy, w: 0.6, h: 0.6, fontSize: 18, bold: true, color: "FFFFFF", align: "center", valign: "middle" });
    if (i < steps.length - 1) s.addShape(LN, { x: MX + 0.3, y: sy + 0.6, w: 0, h: 0.33, line: { color: "F1B6C0", width: 2 } });
    T(s, head, { x: MX + 0.85, y: sy - 0.04, w: 5.1, h: 0.4, fontSize: 14.5, bold: true, color: TXT });
    T(s, body, { x: MX + 0.85, y: sy + 0.33, w: 5.2, h: 0.4, fontSize: 11.5, color: MUTE });
    sy += 0.93;
  }
  // right value panel
  card(s, 6.95, 1.8, 5.68, 4.85, CARDBG);
  await iconCircle(s, 7.3, 2.15, 0.8, I.star, CORAL_D);
  T(s, "提案の“説得力”が変わる", { x: 8.3, y: 2.2, w: 4.1, h: 0.7, fontSize: 18, bold: true, color: TXT, valign: "middle" });
  const vpts = [
    "感覚でなく“なぜ伸びるか”を構造で説明できる",
    "競合・市場の最新の勝ち筋をその場で提示",
    "クライアント提案・社内勉強会にそのまま使える",
  ];
  let vy = 3.25;
  for (const v of vpts) {
    const cd = await ico(I.check, CORAL_D); if (cd) s.addImage({ data: cd, x: 7.35, y: vy + 0.03, w: 0.32, h: 0.32 });
    T(s, v, { x: 7.8, y: vy - 0.05, w: 4.65, h: 0.5, fontSize: 13, color: TXT, valign: "middle" });
    vy += 0.66;
  }
  // reliability badge
  s.addShape(ROUND, { x: 7.35, y: 5.35, w: 5.0, h: 1.05, rectRadius: 0.1, fill: { color: "FFF1F3" }, line: { color: CORAL, width: 1 } });
  await iconCircle(s, 7.6, 5.6, 0.55, I.shield, CORAL_D);
  T(s, [
    { text: "失敗ゼロ化 ", options: { bold: true, color: CORAL_D } },
    { text: "— DL/解析の失敗を自動リカバリ。10本を確実に揃える堅牢設計（負荷テスト済）。", options: { color: TXT } },
  ], { x: 8.3, y: 5.35, w: 3.9, h: 1.05, fontSize: 11.5, valign: "middle", fontFace: FONT });
  footer(s, 10);

  // =========================================================
  // 11. 動画分析 & TikTok検索 (light)
  // =========================================================
  s = p.addSlide(); s.background = { color: PAPER };
  kicker(s, "VIDEO TOOLS", CORAL_D);
  title(s, "単体の動画分析 ＆ TikTok検索");
  // left
  card(s, MX, 1.95, 5.85, 4.4, PAPER);
  await iconCircle(s, MX + 0.4, 2.3, 0.95, I.video, CORAL_D);
  T(s, "動画分析（単体）", { x: MX + 1.55, y: 2.4, w: 4.1, h: 0.55, fontSize: 19, bold: true, color: TXT });
  T(s, "URLを貼るだけ", { x: MX + 1.55, y: 2.95, w: 4.1, h: 0.4, fontSize: 12.5, color: MUTE });
  const lpts = ["YouTube / Shorts / TikTok / Instagram に対応", "構成・フック・テロップ・訴求を要約", "「この動画の良い点は？」にその場で回答"];
  let ly = 3.7;
  for (const t of lpts) {
    const cd = await ico(I.check, CORAL_D); if (cd) s.addImage({ data: cd, x: MX + 0.45, y: ly + 0.03, w: 0.3, h: 0.3 });
    T(s, t, { x: MX + 0.88, y: ly - 0.05, w: 4.8, h: 0.5, fontSize: 12.5, color: TXT, valign: "middle" });
    ly += 0.62;
  }
  // right
  card(s, 6.95, 1.95, 5.68, 4.4, PAPER);
  await iconCircle(s, 7.35, 2.3, 0.95, I.tiktok, "111111");
  T(s, "TikTok検索", { x: 8.5, y: 2.4, w: 4.0, h: 0.55, fontSize: 19, bold: true, color: TXT });
  T(s, "「TikTokで◯◯検索」「#タグ で調べて」", { x: 8.5, y: 2.95, w: 4.0, h: 0.4, fontSize: 12, color: MUTE });
  const rpts = ["上位動画のデータ（再生・保存・構成）を収集", "Geminiが横断で傾向を分析", "市場リサーチ・ネタ探しを数十秒で"];
  let ry = 3.7;
  for (const t of rpts) {
    const cd = await ico(I.check, CORAL_D); if (cd) s.addImage({ data: cd, x: 7.4, y: ry + 0.03, w: 0.3, h: 0.3 });
    T(s, t, { x: 7.83, y: ry - 0.05, w: 4.6, h: 0.5, fontSize: 12.5, color: TXT, valign: "middle" });
    ry += 0.62;
  }
  footer(s, 11);

  // =========================================================
  // 12. 制作中：動画チェックAI (light, "制作中" badge)
  // =========================================================
  s = p.addSlide(); s.background = { color: PAPER };
  kicker(s, "IN DEVELOPMENT", AMBER);
  title(s, "制作中：動画チェックAI — 一次FBを自動化");
  // in-dev badge
  s.addShape(ROUND, { x: 9.7, y: 0.66, w: 2.9, h: 0.6, rectRadius: 0.3, fill: { color: "FEF3C7" }, line: { color: AMBER, width: 1 } });
  await iconCircle(s, 9.85, 0.76, 0.4, I.clock, AMBER);
  T(s, "開発中・近日リリース", { x: 10.3, y: 0.66, w: 2.2, h: 0.6, fontSize: 12, bold: true, color: "92660A", valign: "middle" });

  card(s, MX, 1.9, 6.0, 1.2, INK2);
  await iconCircle(s, MX + 0.3, 2.18, 0.62, I.list, INK, AMBER);
  T(s, "@TeamAgent  動画チェック E01-01", { x: MX + 1.1, y: 1.9, w: 4.7, h: 1.2, fontSize: 14.5, color: "FFFFFF", valign: "middle", fontFace: FONT });
  T(s, "編集者の納品動画を、オリエン（指示書）と自動照合して一次フィードバック。", { x: MX, y: 3.32, w: 6.1, h: 0.62, fontSize: 13.5, color: TXT });
  const checks = [
    ["必須要素の有無", "指定テロップ・ロゴ・訴求が入っているか"],
    ["NGワード検出", "言ってはいけない表現が無いか"],
    ["テロップ／尺の確認", "可読性・指定の長さに収まっているか"],
    ["Phase2：自動監視", "シート更新を検知して自動でチェック"],
  ];
  let chy = 4.0;
  for (const [h2, b2] of checks) {
    await iconCircle(s, MX, chy, 0.56, I.check, AMBER);
    T(s, h2, { x: MX + 0.78, y: chy - 0.04, w: 5.3, h: 0.38, fontSize: 13.5, bold: true, color: TXT });
    T(s, b2, { x: MX + 0.78, y: chy + 0.31, w: 5.4, h: 0.38, fontSize: 11.5, color: MUTE });
    chy += 0.66;
  }
  // right value
  card(s, 7.35, 1.9, 5.28, 4.75, CARDBG);
  await iconCircle(s, 7.7, 2.25, 0.85, I.bolt, AMBER);
  T(s, "なにが嬉しい？", { x: 8.75, y: 2.35, w: 3.6, h: 0.6, fontSize: 18, bold: true, color: TXT, valign: "middle" });
  const dv = [
    ["チェック工数を圧縮", "目視の一次確認をAIが下ごしらえ"],
    ["ヒューマンエラー削減", "見落とし・確認漏れを機械的に防ぐ"],
    ["品質を均一に", "担当者によらず同じ基準で確認"],
  ];
  let dvy = 3.35;
  for (const [h3, b3] of dv) {
    T(s, h3, { x: 7.7, y: dvy, w: 4.7, h: 0.4, fontSize: 14.5, bold: true, color: "92660A" });
    T(s, b3, { x: 7.7, y: dvy + 0.38, w: 4.7, h: 0.4, fontSize: 12, color: TXT });
    dvy += 1.02;
  }
  footer(s, 12);

  // =========================================================
  // 13. 使い方 (light)
  // =========================================================
  s = p.addSlide(); s.background = { color: PAPER };
  kicker(s, "HOW TO USE");
  title(s, "使い方はかんたん — @メンション＋自然文");
  const ex = [
    [I.search, CYAN_D, "ナレッジ検索", "@TeamAgent コスメの予算50万、似た実績ある？"],
    [I.card, CYAN_D, "取引先カルテ", "@TeamAgent ◯◯社のカルテ"],
    [I.file, VIOLET_D, "提案づくり", "@TeamAgent ◯◯向けの提案のたたき台を作って"],
    [I.chart, CORAL_D, "VSEO動画分析", "@TeamAgent VSEO分析 渋谷 カフェ"],
    [I.video, CORAL_D, "動画を分析", "@TeamAgent （動画URLを貼る）"],
    [I.chat, GREEN, "雑談・相談", "@TeamAgent 最近の案件、行き詰まってて…"],
  ];
  const ew = 5.80, eh = 1.18, ex0 = MX, ey0 = 1.95, exg = 0.33, eyg = 0.26;
  for (let i = 0; i < ex.length; i++) {
    const [comp, col, head, body] = ex[i];
    const exx = ex0 + (i % 2) * (ew + exg);
    const eyy = ey0 + Math.floor(i / 2) * (eh + eyg);
    card(s, exx, eyy, ew, eh, PAPER);
    await iconCircle(s, exx + 0.3, eyy + 0.3, 0.6, comp, col);
    T(s, head, { x: exx + 1.05, y: eyy + 0.16, w: ew - 1.2, h: 0.38, fontSize: 14, bold: true, color: TXT });
    T(s, body, { x: exx + 1.05, y: eyy + 0.56, w: ew - 1.25, h: 0.5, fontSize: 11.5, color: MUTE, valign: "top" });
  }
  T(s, "迷ったら、まず話しかけてOK。曖昧な質問も検索で拾います。", { x: MX, y: 6.55, w: 11, h: 0.4, fontSize: 12.5, italic: true, color: MUTE });
  footer(s, 13);

  // =========================================================
  // 14. セキュリティ＆品質 (light)
  // =========================================================
  s = p.addSlide(); s.background = { color: PAPER };
  kicker(s, "SECURITY & QUALITY", GREEN);
  title(s, "裏側の安心 — 守りと品質");
  const sec = [
    [I.lock, "秘密情報はコードに出さない", "鍵・トークンは Secrets Manager のみで管理"],
    [I.shield, "権限分離（RLS）", "本人が見てよい資料だけを検索対象に"],
    [I.card, "本人データは本人だけ", "個人OAuth。他人のGoogleは参照しない"],
    [I.check, "削除しない設計", "シートは単一セル更新・追記のみ（事故防止）"],
    [I.eye, "反ハルシネーション", "根拠なき断定をしない。出典リンクで確認可"],
    [I.gauge, "自動テスト & 総量規制", "831件の自動テスト／同時4名で安定運用"],
  ];
  const sw = 5.80, sh = 1.18, sx0 = MX, syy0 = 1.95, sxg = 0.33, syg = 0.26;
  for (let i = 0; i < sec.length; i++) {
    const [comp, head, body] = sec[i];
    const sxx = sx0 + (i % 2) * (sw + sxg);
    const syy = syy0 + Math.floor(i / 2) * (sh + syg);
    card(s, sxx, syy, sw, sh, PAPER);
    await iconCircle(s, sxx + 0.3, syy + 0.3, 0.6, comp, GREEN);
    T(s, head, { x: sxx + 1.05, y: syy + 0.16, w: sw - 1.2, h: 0.4, fontSize: 14, bold: true, color: TXT });
    T(s, body, { x: sxx + 1.05, y: syy + 0.56, w: sw - 1.25, h: 0.5, fontSize: 11.5, color: MUTE, valign: "top" });
  }
  footer(s, 14);

  // =========================================================
  // 15. 技術スタック（概要・1枚, light）
  // =========================================================
  s = p.addSlide(); s.background = { color: PAPER };
  kicker(s, "UNDER THE HOOD");
  title(s, "技術スタック（ざっくり1枚）");
  const tech = [
    [I.wand, CYAN_D, "テキストAI", "Claude（AWS Bedrock）\n検索・要約・提案・会話"],
    [I.video, CORAL_D, "動画AI", "Gemini（GCP Vertex AI）\nマルチモーダル動画分析"],
    [I.db, VIOLET_D, "検索基盤", "pgvector ＋ Cohere Rerank\n意味検索＋精度リランク"],
    [I.slack, "111111", "入口", "Slack（Socket Mode）\n@メンションで起動"],
  ];
  const tw = 2.77, th = 2.7, tx0 = MX, ty0 = 2.0, txg = 0.30;
  for (let i = 0; i < tech.length; i++) {
    const [comp, col, head, body] = tech[i];
    const txx = tx0 + i * (tw + txg);
    card(s, txx, ty0, tw, th, PAPER);
    await iconCircle(s, txx + tw / 2 - 0.45, ty0 + 0.32, 0.9, comp, col === "111111" ? "111111" : col);
    T(s, head, { x: txx + 0.15, y: ty0 + 1.32, w: tw - 0.3, h: 0.45, fontSize: 15.5, bold: true, color: TXT, align: "center" });
    T(s, body, { x: txx + 0.2, y: ty0 + 1.82, w: tw - 0.4, h: 0.8, fontSize: 11, color: MUTE, align: "center", valign: "top" });
  }
  s.addShape(ROUND, { x: MX, y: 5.05, w: W - 2 * MX, h: 1.05, rectRadius: 0.1, fill: { color: INK }, line: { type: "none" } });
  await iconCircle(s, MX + 0.32, 5.28, 0.6, I.layers, INK2, CYAN);
  T(s, [
    { text: "設計思想： ", options: { bold: true, color: CYAN } },
    { text: "3層分離（差し替え可能）／プロンプトはファイル管理／全I/Oを型で検証。テスト容易・拡張容易な“育てられる”基盤。", options: { color: "DBE3FF" } },
  ], { x: MX + 1.1, y: 5.05, w: 10.6, h: 1.05, fontSize: 12.5, valign: "middle", fontFace: FONT });
  footer(s, 15);

  // =========================================================
  // 16. ロードマップ (light)
  // =========================================================
  s = p.addSlide(); s.background = { color: PAPER };
  kicker(s, "ROADMAP", VIOLET_D);
  title(s, "これから — “相棒”を全社の脳へ育てる");
  const road = [
    [I.list, "動画チェックAIの本番化", "編集の一次FBを自動化（近日）"],
    [I.link, "自分のGoogleを繋ぐ", "本人のカレンダー・連絡先まで活用（per-user連携）"],
    [I.brain, "全社ナレッジ投入の拡大", "14名分→全部門へ。脳を厚くする"],
    [I.gauge, "利用の可視化", "ダッシュボードで効果と利用状況を見える化"],
  ];
  let rdy = 2.1;
  for (let i = 0; i < road.length; i++) {
    const [comp, head, body] = road[i];
    s.addShape(OVAL, { x: MX + 0.02, y: rdy, w: 0.7, h: 0.7, fill: { color: VIOLET_D }, line: { type: "none" } });
    const cd = await ico(comp, "FFFFFF"); if (cd) s.addImage({ data: cd, x: MX + 0.19, y: rdy + 0.17, w: 0.36, h: 0.36 });
    if (i < road.length - 1) s.addShape(LN, { x: MX + 0.37, y: rdy + 0.7, w: 0, h: 0.42, line: { color: "CBD0F0", width: 2 } });
    T(s, head, { x: MX + 1.05, y: rdy - 0.02, w: 10.5, h: 0.45, fontSize: 17, bold: true, color: TXT });
    T(s, body, { x: MX + 1.05, y: rdy + 0.44, w: 10.5, h: 0.4, fontSize: 12.5, color: MUTE });
    rdy += 1.12;
  }
  footer(s, 16);

  // =========================================================
  // 17. CLOSING (dark)
  // =========================================================
  s = p.addSlide(); s.background = { color: INK };
  s.addShape(OVAL, { x: -1.6, y: 4.2, w: 5, h: 5, fill: { color: INK2 }, line: { type: "none" } });
  s.addShape(OVAL, { x: 11.0, y: -1.4, w: 4.2, h: 4.2, fill: { color: "16204A" }, line: { type: "none" } });
  await iconCircle(s, MX, 1.55, 0.85, I.brain, CYAN, INK);
  T(s, [
    { text: "“検索する道具” から、", options: { color: "C7D2FE" }, breakLine: true },
    { text: "“提案する相棒” へ。", options: { color: CYAN, bold: true } },
  ], { x: MX, y: 2.75, w: 11.5, h: 1.9, fontSize: 44, bold: true, fontFace: FONT, lineSpacingMultiple: 1.1 });
  T(s, "明日から、Slackで @TeamAgent と話しかけてください。", { x: MX, y: 5.0, w: 11, h: 0.5, fontSize: 18, color: "DBE3FF" });
  T(s, "TeamAgent ／ ベクトル株式会社 ・ 2026.06", { x: MX, y: 6.6, w: 9, h: 0.4, fontSize: 12, color: "5C679B" });

  const out = "TeamAgent_AI紹介_2026-06.pptx";
  await p.writeFile({ fileName: out });
  console.log("WROTE", out);
})().catch((e) => { console.error("ERR", e); process.exit(1); });
