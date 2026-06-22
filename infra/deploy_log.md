# 本番デプロイ履歴（image↔commit 追跡）

柱4（2026-06-22 事故対策）。`terraform.tfvars` は `.gitignore` 対象で image digest が repo に
残らない＝「本番で動いている image がどの commit から焼かれたか」が追えずドリフトの温床だった。
**デプロイの度にここへ1行追記**して、digest ↔ source commit ↔ 変更内容 を repo に残す。

運用ルール:
- image を焼いて apply したら、その digest・ビルド元 commit/branch・IMAGE_TAG・変更概要を追記。
- ビルドは `GIT_COMMIT=$(git rev-parse HEAD)` を `--build-arg`/`--environment-variables-override` で
  渡す（Dockerfile が ENV `TEAMAGENT_BUILD_COMMIT`／LABEL `org.opencontainers.image.revision` に刻む）。
  → 起動ログ `[entrypoint] build commit=…`（openclaw）や image LABEL からも遡れる。

| 日付(JST) | component | image digest (短) | build commit | branch | 変更概要 |
|---|---|---|---|---|---|
| 2026-06-22 | mcp | `fa9fd8a8` | d1d34f7 | feat/knowledge-base-phase1 | 検索round2（Cohere再ランク＋業界ルーティング＋確信配信＋bedrock:Rerank IAM）|
| 2026-06-22 | openclaw | `320754894c`(→rollback) | (SOUL硬化版) | feat/knowledge-base-phase1 | SOUL硬化（内部メカニズム秘匿・依頼ごとにツール実行）|
| 2026-06-22 | openclaw | `3fbef2ee` | 543d148 | feat/knowledge-base-phase1 | dmPolicy allowlist→open（社内DM自己連携）|
| 2026-06-22 | openclaw(env) | `3fbef2ee`(同) | e343de6 | feat/knowledge-base-phase1 | SLACK_DM_ALLOWLIST="*"（allowFrom=["*"]注入）＝非管理者DMの無音drop解消。**真因=open でも allowFrom に "*" 無いと allowlist gating** |

> 注: 上の openclaw `3fbef2ee` は dmPolicy:open のイメージ。最終的な「全社内DM開放」は
> env `SLACK_DM_ALLOWLIST="*"`（tfvars・gitignore）との組で成立（柱1の不変条件チェックで今後は保証）。
