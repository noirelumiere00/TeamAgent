# 動画パイプラインの AWS 計算基盤移設評価 — EC2 vs ECS（2026-06-02）

## 0. なぜ移すのか（目的）
1. **会社プロキシSSLの根治**（最重要）。ローカル実行では yt-dlp が TikTok CDN から動画データを取る際、会社プロキシの自己署名CAで `SSL: CERTIFICATE_VERIFY_FAILED` が間欠発生し、上位5本中 1〜2本が落ちる（実測: 5本→分析成立3本）。AWS は会社プロキシの外なので、直接 CDN に繋がり**この失敗が消える**。
2. **常時稼働のSlack Bot**。Socket Mode のリスナーは常駐が必要（メンションを受けるため）。ローカルMacの常時起動に依存させたくない。
3. **レポートのURL配信は既にAWS(S3署名付き)で稼働**。計算基盤もAWSに寄せると一貫する。

> 注: SSLはAWSに行かずとも `yt-dlp nocheckcertificate` / `SSL_CERT_FILE`(社内CA) / truststore で**ローカル回避も可能**。本書は「常時稼働＋根治＋将来スケール」を見据えた基盤移設の比較。

## 1. ワークロード特性
- **バースト/オンデマンド**: 営業がSlackで「VSEO分析 <KW>」した時だけ重い処理。1回 ≈ 2〜5分。動画審査(video_approval)も同型(Drive DL+ffmpeg+Gemini)。
- **重い依存**: Node + ヘッドレスChrome(Puppeteer, tiktok_search) / yt-dlp / ffmpeg(proxy・frames・thumbnails) / Gemini(Vertex)。**メモリ4GBは欲しい**（Chrome+ffmpeg同時）。
- **送受信**: 動画DL=内向き(無料)。Gemini への inline 動画アップロード=外向き(数MB×本数、月1GB程度＝ほぼ無料)。
- **常駐部分**: Slack Socket Mode リスナーは小さく常時稼働。

## 2. 現行AWS実態（2026-06 実測）
| 区分 | 内容 |
|---|---|
| EC2 | **1台のみ** `teamagent-dev-bastion`（t4g.nano, running）＝踏み台。アプリ計算は無し |
| ECS / ECR | **無し**（クラスタ0・リポジトリ0）= ECSはグリーンフィールド |
| RDS | `teamagent-dev`（db.t4g.micro, postgres/pgvector, Tokyo） |
| 5月コスト | **≈ $0.00**（Free Tier / クレジット範囲）。新規計算を足すと純増になる |
| VPC | 既存（bastion が居る＝公開サブネット有り。SSM 接続経路もある） |

**含意**: 既に Graviton(arm64, t4g)系で揃っている。公開サブネットがあるので **NAT Gateway($45/mo級) を新設せず外向き通信できる**。

## 3. 選択肢の比較
| 観点 | **A. 単一EC2(t4g.medium)** | **B. ECS Fargate** | C. 現状ローカル+SSL回避 |
|---|---|---|---|
| SSL根治 | ◎ | ◎ | △(回避で実用) |
| 立ち上げ速度 | ◎ 既存VPC/SSMに1台足すだけ | △ 画像(Chrome+ffmpeg)+ECR+タスク定義+権限を新規構築 | ◎ 即 |
| 運用負荷 | ○ OS/パッチは自分持ち(SSMで自動化可) | ◎ サーバ管理不要 | ◎ |
| 常駐Bot | ◎ そのまま常駐 | ○ 常駐は **Fargate Service**(=常時課金) | △ Mac依存 |
| バースト隔離 | △ 同一ホストでBotと重処理が同居 | ◎ ジョブ毎にタスク分離 | △ |
| スケール | △ 縦(インスタンス大型化) | ◎ 横(タスク並列) | ✕ |
| Chrome(arm64) | ◎ Chrome for Testing arm64で可(ローカルと同様) | ○ 画像にChrome依存を焼く必要 | ◎ |
| 月額概算※ | **≈ $29** | **≈ $36〜(常駐) + 実行分** | $0(社内回線) |
| TikTok DCIP遮断リスク | あり(下記5) | あり(下記5) | 低(オフィスIP) |

※ ap-northeast-1 オンデマンド概算（2026目安）:
- **EC2 t4g.medium**(2vCPU/4GB) $0.0376/h ×730h ≈ **$27.4/mo** + EBS gp3 20GB ≈ $1.6 → **≈$29/mo**（停止できない=Bot常駐のため常時）。t4g.small(2GB)は$14/moだがChrome+ffmpeg同時で逼迫リスク。
- **Fargate(arm64)** 1vCPU/2GB 常駐 ≈ $0.049/h ×730 ≈ **$36/mo** + 重タスク実行分($0.05/h×実行時間, 月数十回なら数百円) + ECR保管(数十円)。**常駐BotをFargate Serviceにすると常時課金**なのでEC2より割高。
- いずれも**外向き通信は公開サブネット直行でNAT不要**（NATを挟むと+$45/mo級なので回避設計が重要）。

## 4. 推奨 — **まず単一EC2(t4g.medium)、ECSは"条件付き"のv2**
**結論**: この規模（営業16名・オンデマンド低頻度・現行ほぼ$0）では、**A. 単一EC2 が最短でSSLを根治し、常駐Botも兼ね、最も安く・最も速く立つ**。ECS Fargateは構成要素（コンテナ画像/ECR/タスク定義/実行ロール）が増える割に、この負荷では費用も運用妙味も上回らない。

- **第1段（今すぐ価値）**: 既存VPCの公開サブネットに **t4g.medium 1台**。Chrome for Testing(arm64)+ffmpeg+python+node を入れ、Bot常駐＋VSEO/動画審査ワーカーを同居。SSM接続(踏み台と同経路)。SSL根治＋常時稼働＋URL配信(S3)まで一気通貫。EBS暗号化・自動パッチ(SSM Patch Manager)・CloudWatchログ。
- **第2段（ECSへ行く"トリガー"が出たら）**: 次のいずれかが現実になったらFargateへ。
  - (a) 重処理がBotの応答性を食う（同居の限界）→ ジョブをFargateタスクに切り出す。
  - (b) 同時実行が増え横スケールが要る。
  - (c) "ゼロアイドル"課金にしたいほど実行が散発的＝常駐BotをEventBridge+Lambda起動の薄い常駐に変え、重処理だけFargate。
  - 画像は1つ（Chrome+ffmpeg+yt-dlp+app）をECRに置けば EC2/Fargate どちらでも再利用可能なので、**今からDockerfile化しておくと第2段の移行コストが下がる**（EC2上でもそのコンテナを動かす運用にすれば段差ゼロ）。

## 5. 最大のリスク = TikTokのデータセンターIP遮断（正直に）
- AWSのIPレンジは**住宅/オフィスIPより厳しくrate-limit/CAPTCHA/遮断**されやすい。tiktok_search(Puppeteer)・yt-dlp の両方が影響を受け得る。**SSLは直るが、代わりにこの壁が出る可能性**がある。
- **緩和**: (1) **住宅/モバイルプロキシ**を tiktok_search と yt-dlp に通す（例: 商用レジデンシャルプロキシ。月額が乗る）。(2) リクエスト間隔・同時数を抑える。(3) 失敗時は**オフィス回線のローカル実行にフォールバック**（現行の graceful 隔離が活きる）。(4) まず小規模(t4g.medium)で**実地に遮断率を測ってから**プロキシ投資を判断。
- → 移設は「SSL根治」効果が確実、「DCIP遮断」は**やってみないと程度が読めない**。**第1段で遮断率を実測**し、必要ならプロキシを足す、の順が安全。

## 6. 次アクション（ユーザー承認待ち）
1. 上記 **第1段(単一t4g.medium)** で進めてよいか（推奨）。
2. 進める場合: VPC/サブネット/SGの確認 → インスタンス作成(IaC: 既存tfstateがあるのでTerraform推奨) → 依存インストール → Bot常駐化(systemd) → 実地で TikTok 遮断率を1日観測 → 必要ならプロキシ。
3. 並行して app の **Dockerfile化**（将来のECS移行＆環境再現性のため。EC2上でもコンテナ運用にできる）。

> 本書は比較・推奨のみ。インフラ構築は未実施（ユーザーの方針決定後に着手）。
