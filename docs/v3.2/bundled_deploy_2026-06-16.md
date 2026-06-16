# まとめてデプロイ runbook（2026-06-16）

> dev に溜まった差分（go-live〜Wave1-3〜可観測性・main より 40+ commit 先行）を**一発**で本番反映する turnkey 手順。
> 個別デプロイは打たず、これ1枚で完結させる方針（ユーザー決定 2026-06-16）。
> 実行は人間（operator）が「go」と判断したとき。コマンドはコピペ可。**secret 値は貼らない**。

リポジトリ: `~/Documents/teamagent-orchestrator-poc` / branch `dev` / region `ap-northeast-1` / account `718959508629`。

---

## 0. 結論（30秒）
- **正味の効果＝MCP の JSON ログ化**（→ CloudWatch アラーム McpCostUSD/McpToolError/McpIdentitySpoofRejected が初めて発火する）。
- **やるのは MCP のみの再ビルド＋ECS ローリング**（無停止）。OpenClaw は据置・worker EC2 も据置。
- proposal_deck は **gated OFF のまま**（env を付けない）。

---

## 1. Inventory（dev 待機中の差分とデプロイ要否）

| 変更 | commit | 反映状態 | この一発で要る？ |
|---|---|---|---|
| Wave1-② operation_log 配線 | `76327b1` | **live mcp:5 に反映済**（USE_OPERATION_LOG_TOOLS=1） | 維持するだけ |
| Wave1-③ ingest systemd + #ops | `9133660` | worker EC2 に配信済（systemd 稼働） | 不要（worker 側） |
| Wave2-④ office 抽出 | `5477341` | worker EC2 に SSM 配信済（ingest 専用・MCP無関係） | 不要 |
| Wave2-⑤ migration 0012 | `8eb60ef` | **本番 RDS 適用済** | 不要 |
| Wave3-⑧ proposal_deck（gated OFF） | `9613587`/`49a5a04` | dev のみ・**OFF なので無害** | 同梱されるが露出しない |
| Wave3-⑨ proposal_deck publish | `0fe3633` | dev のみ・gated | 同梱（無害） |
| pilot_health.py | `e89aa28` | スクリプト（デプロイ不要） | 不要 |
| **JSON ログ化** | `40afded` | **dev のみ・要 MCP 再ビルド** | ★これが本命 |

→ **MCP イメージを dev HEAD で再ビルドし ECS を更新するだけ**で、上記の「要る」＝JSON 化が反映される。

---

## 2. ⚠️ 重要な前提（ハマり防止）
- **live の taskdef `teamagent-dev-mcp:5` は手動 register（Wave1）で、terraform 由来ではない。** よって `fargate.tf` に足した `STRUCTLOG_FORMAT=json` は **terraform apply しない限り live に効かない**。本 runbook は手動 register を踏襲するので、**手順 3-② で env に `STRUCTLOG_FORMAT=json` を明示的に入れる**（忘れると JSON 化されずアラームが直らない）。
- ECR は **immutable tag**。新タグを採番すること（既存 `p2v-wave1` 等は再利用不可）。

---

## 3. デプロイ手順（MCP のみ・無停止）

### ① ソース zip → S3 → CodeBuild（ECR へ push）
```bash
cd ~/Documents/teamagent-orchestrator-poc
git checkout dev && git pull            # dev HEAD（40afded 以降）であることを確認
git archive --format=zip -o /tmp/teamagent-source.zip HEAD
aws s3 cp /tmp/teamagent-source.zip s3://teamagent-dev-raw-files/codebuild/source.zip --region ap-northeast-1

aws codebuild start-build \
  --project-name teamagent-dev-image-builder \
  --environment-variables-override \
      'name=WITH_SCRAPE_TOOLS,value=true,type=PLAINTEXT' \
      'name=IMAGE_TAG,value=p2x-bundle,type=PLAINTEXT' \
  --region ap-northeast-1 --query 'build.id' --output text
# → SUCCEEDED まで待つ（aws codebuild batch-get-builds --ids <id> --query 'builds[0].buildStatus'）
```
完了後、digest を取得：
```bash
aws ecr describe-images --repository-name teamagent-mcp --image-ids imageTag=p2x-bundle \
  --region ap-northeast-1 --query 'imageDetails[0].imageDigest' --output text
```

### ② 新 taskdef を register（現行 mcp:5 を land にして image + env を更新）
```bash
aws ecs describe-task-definition --task-definition teamagent-dev-mcp --region ap-northeast-1 \
  --query 'taskDefinition' --output json > /tmp/mcp-td.json

# 必要キーだけ抽出 → image を新 digest に → env に STRUCTLOG_FORMAT=json を必ず追加（無ければ）
jq '{family,taskRoleArn,executionRoleArn,networkMode,containerDefinitions,volumes,
     placementConstraints,requiresCompatibilities,cpu,memory,runtimePlatform,ephemeralStorage}
    | with_entries(select(.value != null))
    | .containerDefinitions[0].image = "718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/teamagent-mcp@<NEW_DIGEST>"
    | .containerDefinitions[0].environment |= (
        (map(select(.name!="STRUCTLOG_FORMAT"))) + [{"name":"STRUCTLOG_FORMAT","value":"json"}]
      )' /tmp/mcp-td.json > /tmp/mcp-td-new.json

# env 確認（必須: STRUCTLOG_FORMAT=json / USE_OPERATION_LOG_TOOLS=1 / USE_VIDEO_TOOLS=1 / USE_TIKTOK_TOOLS=1）
#         （禁止: USE_PROPOSAL_DECK_TOOLS は付けない＝proposal_deck は露出させない）
jq '.containerDefinitions[0].environment' /tmp/mcp-td-new.json

aws ecs register-task-definition --cli-input-json file:///tmp/mcp-td-new.json --region ap-northeast-1 \
  --query 'taskDefinition.{family:family,revision:revision}'
```

### ③ ECS ローリング（無停止）
```bash
aws ecs update-service --cluster teamagent-dev --service teamagent-dev-mcp \
  --task-definition teamagent-dev-mcp:<NEW_REV> --region ap-northeast-1 \
  --query 'service.deployments[].{status:status,td:taskDefinition,rollout:rolloutState}'
# rolloutState COMPLETED / runningCount==desiredCount / health HEALTHY を確認
```

---

## 4. Post-deploy 検証（この順で）
1. **JSON 形式 flip**: 再起動後、MCP 起動ログが JSON になったことを確認。
   ```bash
   aws logs filter-log-events --log-group-name /teamagent/dev/teamagent-mcp \
     --filter-pattern '{ $.event = "mcp_http_started" }' --region ap-northeast-1 \
     --max-items 3 --query 'events[].message'
   # → JSON 行（{"event":"mcp_http_started",...}）が返れば JSON 化成功
   #   （旧 console 形式だと $.event パターンにマッチせず 0 件）
   ```
2. **アラーム発火土台**: Slack（OpenClaw 経由）で検索を1回流す → `McpCostUSD` メトリクスに datapoint。
   ```bash
   aws cloudwatch get-metric-statistics --namespace TeamAgent/dev --metric-name McpCostUSD \
     --start-time <now-1h> --end-time <now> --period 300 --statistics Sum --region ap-northeast-1
   # → Datapoints が空でない＝metric filter がバインド。MCP アラームが INSUFFICIENT_DATA を抜ける。
   ```
3. **readout**: `python scripts/pilot_health.py --hours 1` が JSON ログから p95/cost/error を引いて GO。
4. **smoke / RLS**: operator が VPC 内で `scripts/smoke_mcp.py --full`（#15）/ `scripts/attack_mcp.py`（#16）を再走。RLS は DB 層 28 tests で不変（`pilot_gate_status_2026-06-15.md`）。

---

## 5. Rollback（~1分・無停止）
```bash
aws ecs update-service --cluster teamagent-dev --service teamagent-dev-mcp \
  --task-definition teamagent-dev-mcp:5 --region ap-northeast-1
```
旧イメージ（Wave1・console ログ）へ即戻る。OpenClaw/worker は触っていないので影響なし。

---

## 6. 既知ギャップ（この一発の対象外・別タスク）
- **app 経路の CloudWatch ログ送出**: `/teamagent/dev`（worker EC2 旧 slack_bot）は **0 bytes＝ログ未到達**（EC2 に CloudWatch agent 無し）。app 系アラーム（BedrockCostUSD/SkillLatencyMs/ErrorCount）は「形式」以前に「未到達」で死。EC2 への log 送出（CloudWatch agent or awslogs）を入れる時に、worker の `ec2.overrides.env` へ `STRUCTLOG_FORMAT=json` も同時に足す。※本番経路は OpenClaw→MCP で、そちらは本デプロイで観測可能になる。
- **`OPS_SLACK_WEBHOOK` 実値投入**（Slack admin・未投入なら ingest 失敗は journalctl 手検知）。
- **`dev→main` マージ**（40+ commit）: デプロイは dev からなので必須ではないが、いずれ PR で同期。
- **proposal_deck 本番化**（FMT テンプレ provision＋`USE_PROPOSAL_DECK_TOOLS=1`）＝P2。

---

## 7. パイロット人手ゲート（デプロイ後・`pilot_gate_status_2026-06-15.md` と併用）
ゲート①署名 / #17 2人混線 / #18 1週間観測。機械検証（RLS・検索 latency・可観測性）は GO 済。

---

## 8. 実施記録（2026-06-16・無停止で実行済）
- ソース: dev `c3cf803` → CodeBuild `teamagent-dev-image-builder`（tag `p2x-bundle`・WITH_SCRAPE_TOOLS=true・~4分で SUCCEEDED）→ ECR digest `sha256:4d8904a6…`。
- taskdef: `teamagent-dev-mcp:6` を register（image=新digest／**STRUCTLOG_FORMAT=json 注入**／USE_OPERATION_LOG_TOOLS=1 維持／USE_PROPOSAL_DECK_TOOLS は付けず＝proposal_deck 非露出）。
- ECS: `update-service` → rolloutState COMPLETED・新タスク RUNNING/HEALTHY・**無停止**。
- 検証: ✅ JSON flip 確認（`{ $.event="mcp_http_started" }` で JSON 行マッチ＝structlog JSON 化が本番稼働）／✅ `pilot_health.py --hours 1` が JSON ログに対し正常動作（検索0件＝判定不可 GO）。
- **未（要 operator/パイロット）**: `McpCostUSD` への初 datapoint＋MCP アラームの INSUFFICIENT_DATA 脱出は、OpenClaw 経由の**初回実検索**で確認。
- rollback 可: `aws ecs update-service ... --task-definition teamagent-dev-mcp:5`。
