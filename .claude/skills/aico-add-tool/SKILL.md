---
name: aico-add-tool
description: Aico（TeamAgent）に新しいツール/機能（Skill）を足す・出し入れするときの手順。4段ゲート（skill → factory USE_<X> → openclaw toolFilter.include → 本番 env ON）、description の棲み分け、ルーティング検証、出荷前チェックリスト。「ツールを追加したい」「skill を OC から呼べるようにしたい」「実装したのに本番で呼べない」で使う。
---

# Aico にツール/機能を足す手順

新しい能力は「skill を書いたら使える」ではない。**OC(Aico) に届くには段が4つ**あり、どこかで止まると「実装したのに本番で呼べない」になる（実例: `video_approval` は skill 完成済みなのに OC 未露出で長く埋もれていた）。
仕組みの詳細は `openwiki/architecture/tool-registry-and-feature-flags.md`、規則は `CLAUDE.md` §3・§4・§7。

## E1 ツールが本番で Aico に届くまでの4段（全部通って初めて稼働）

1. **skill 実装**：`src/teamagent/skills/<name>/{schema,skill}.py` ＋ `@register`（CLAUDE.md §3 の Do/Don't に従う）。← ここまでで「実装済」。
2. **factory 登録（env-gate）**：`src/teamagent/orchestrator/factory.py` の `build_production_tools()` に `if _envflag("USE_<X>"): specs.append(ToolSpec(...))`。新規は**必ず既定 OFF**（後方互換）。
3. **OC 露出（第2ゲート）**：`infra/openclaw/openclaw.config.json5` の `mcp.servers.teamagent.toolFilter.include` に **tool 名を追加**。factory に在っても include に無ければ **OpenClaw からは見えない**（一番の見落としポイント）。
4. **本番 ON（デプロイ＝人間ゲート）**：mcp の ECS task env に `USE_<X>=1`（tfvars/CodeBuild）→ apply → run-task 検証（CLAUDE.md §4）。AWS 書込み（apply/build/push）は人間が実行する。

> **「実装済」と「稼働」は別。** どの段で止まっているかを常に言う。`factory` だけ＝dark（コードは在るが見えない）／`include` だけ＝MCP 側の `USE_<X>` が無いと呼んでも失敗。

## E2 description は「AI の振り分け判断材料」

- OC は **name + description だけ**でどの tool を呼ぶか決める。被る tool（特に動画/TikTok 系: `tiktok_search` / `tiktok_acquire` / `video_algorithm` / `video_analysis` / `video_approval` / `proposal_campaign`）は、**トリガー語で棲み分け**＋**「これは対象外（→他tool）」の相互排他注記**を description に書く。
  - 例：`video_approval`=「自社編集者の**納品**動画チェック（誤植/尺/NG）」 vs `video_analysis`=「**外部URLの競合**動画分析・納品物は対象外（→video_approval）」。
- **新規/変更時はルーティング・シミュで検証**：現実的な依頼を多数流し、正しい tool に行くか・混同ペアが無いかを出荷前に確認する（手順は `openwiki/testing/routing-and-eval.md`）。過去実績は 32 発話で正答率 97%、最大リスクは動画チェック↔競合分析の取り違え（納品事故）。

## E3 ツール追加チェックリスト

- [ ] `@register` で skill 登録
- [ ] factory に `USE_<X>` gate（既定 OFF）
- [ ] `openclaw.config.json5` の `toolFilter.include` に追加
- [ ] description にトリガー語＋相互排他注記（被る tool があるなら）
- [ ] 単体テスト（登録＋description 固定。`tests/skills/`）
- [ ] 本番 env ON（CLAUDE.md §4）は人間ゲート
- [ ] 「今どの段か」を PR と `infra/deploy_log.md` に明記

## E4 ブランチ衛生

- 機能が未マージ PR / 未 push ローカルコミットに散在しやすい（本番イメージが dev より進む／dev にコードが無いのに本番 env に flag がある等の不整合が起きる）。
- 新規作業は必ず `origin/dev` 基点で branch を切る。「何が本番か」は `infra/deploy_log.md` と本番 ECS task の env（`aws ecs describe-task-definition --task-definition teamagent-dev-mcp`）を実機で確認してから判断する。
