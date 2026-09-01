# PR2-A0: content-addressed buildspec の hash-keyed append-only 世代モデル。
#
# 【背景】evidence バケット上の buildspec は content-addressed key
# （codebuild-buildspecs/<project>/<body の sha256>.yml）で置かれ、Object Lock
# GOVERNANCE(2099-12-31) と bucket policy の Delete 無条件 Deny で不変化されている。
# さらに runtime_evidence.tf の DenyReleaseEvidenceObjectMutation が、apply を実行する
# 唯一の principal（terraform-runtime-automation）に対して当バケットへの s3:PutObject を
# 明示 Deny している。つまり **Terraform はこれらのオブジェクトを create できない**。
#
# 【従来モデルの破綻】単一リソースの key を sha256(body) から導出していたため、body の
# 入力（契約 JSON・helper スクリプト）が変わるたびに key が変わり、aws_s3_object.key は
# ForceNew なので replacement 判定になる。しかし prevent_destroy が plan 段階で停止する。
# 実際 2026-08-17 の契約更新以降、dev HEAD は 4 本の buildspec で apply 不能になっていた。
# 一方で AWS 実体は正しく、admin が新世代を publish 済み・CodeBuild も新 key を参照済みで、
# 取り残されていたのは tfstate だけだった。
#
# 【本モデル】世代を key（= body の sha256）で持つ append-only 台帳にする。
#   新世代の取り込み手順: admin が S3 へ publish → 下の台帳へ 1 エントリ追記 → adopt(import)
#   既存エントリは削除しない（削除しようとすると prevent_destroy が停止させる）。
#
# 【content を持たせない理由】adopt 対象は既に Object Lock 下にある不変オブジェクトで、
# Terraform 側に content を持たせると import 直後に PutObject を伴う update が planned され、
# 実体（VersionId を含む）を書き換えてしまう。よって Terraform は「不変 artifact の存在と
# その content-addressed key」だけを管理し、body の整合性は
#   (a) 下の check ブロック（Terraform が保持する body の sha256 が台帳に登録されていること）
#   (b) infra/deploy/supply_chain_adopt_integrity.py の独立した S3 body SHA256 検査
# の二重で担保する。ignore_changes は使わない（実測で不要と確認済み）。
#
# 【属性値の出所】各世代の content_type / object_lock_retain_until_date は **実体の値**を
# 世代ごとに明示する。共通定数でまとめない — 実体は世代ごとに publish イベントが異なり、
# retain-until も content_type も揃っているとは限らない（live head-object 実測 2026-08-28:
# Wave3 の 3 世代は content_type=text/yaml かつ retain 00:00:00Z、approval-publisher の
# 08-13 世代だけ content_type=binary/octet-stream かつ retain 23:59:59Z）。共通定数への
# 一般化が PR2-A0 初版で実体との不一致を生み、adopt が Object Lock を短縮する import に
# なりかけた。同じ病気が content_type でも起きていた: 初版は 4 世代とも
# binary/octet-stream を宣言していたが、実体は 3 件が text/yaml で、
# supply_chain_adopt_validate.py の IMPORT_DIFF_IGNORED_ATTRIBUTES が空である以上、
# その宣言のままでは import が必ず「実体を変える差分」として拒否された。
# 各値は adopt 前に supply_chain_adopt_integrity.py の crosscheck が live 実体と照合し、
# repo 内では tests/scripts/test_supply_chain_adopt.py が実測値との一致を固定する。
#
# 【この台帳は短命 manifest】activation 対象の dev HEAD（Generation Baseline）に束縛される。
# buildspec 入力（infra/deploy/buildspec_generation_inputs.json が列挙）が変わると
# content-addressed key も変わり、この台帳は陳腐化する。値の正は Terraform 自身の評価
# （local.*_buildspec_sha256）であり、live CodeBuild 参照への盲目的な追随は禁止。
# 詳細は docs/runbooks/supply_chain_adopt.md を参照。

locals {
  mcp_source_publisher_buildspec_generations = {
    # 2026-08-26 03:50 UTC publish 世代（Wave3）。content_type は live 実測の text/yaml。
    # 値の出所は repo tree からのオフライン導出（infra/deploy/derive_buildspec_generations.py）
    # で、live からの写経ではない。導出値 == live == 本台帳の 3 点一致を CI が固定する。
    "db8a6c2b97c36e68f201873660fc5333e27fffb5b34dddf88f498c5db5f6538b" = {
      content_type                  = "text/yaml"
      object_lock_retain_until_date = "2099-12-31T00:00:00Z"
    }
    # 2026-09-01 publish 世代（Wave4・便γ）。契約の node pin/lock 追随（PR #363）による
    # 再レンダリング。値の出所は repo tree からのオフライン導出（校正済みハーネスで
    # Wave3 4値の完全再現を確認済み）。publish 儀式は同日 admin CLI（Wave3 と同一経路）。
    "a20ce391f39d57bfce922f77008a55c1106bbe5d8cd2d1d4e55de7fd71e758cc" = {
      content_type                  = "text/yaml"
      object_lock_retain_until_date = "2099-12-31T00:00:00Z"
    }
  }

  image_attestor_buildspec_generations = {
    # 2026-08-26 03:50 UTC publish 世代（Wave3）。content_type は live 実測の text/yaml。
    # 値の出所は repo tree からのオフライン導出（infra/deploy/derive_buildspec_generations.py）
    # で、live からの写経ではない。導出値 == live == 本台帳の 3 点一致を CI が固定する。
    "1e1906ae37692b12e8ac7ca833d43c9c826263728e48cc5fedf625b2f99ee8b6" = {
      content_type                  = "text/yaml"
      object_lock_retain_until_date = "2099-12-31T00:00:00Z"
    }
    # 2026-09-01 publish 世代（Wave4・便γ）。契約の node pin/lock 追随（PR #363）による
    # 再レンダリング。値の出所は repo tree からのオフライン導出（校正済みハーネスで
    # Wave3 4値の完全再現を確認済み）。publish 儀式は同日 admin CLI（Wave3 と同一経路）。
    "cfe30ec490d3561dc50e995f81921069ed00fa822af2c0cf5f3aabaa6e88cb2a" = {
      content_type                  = "text/yaml"
      object_lock_retain_until_date = "2099-12-31T00:00:00Z"
    }
  }

  image_promoter_buildspec_generations = {
    # 2026-08-26 03:50 UTC publish 世代（Wave3）。content_type は live 実測の text/yaml。
    # 値の出所は repo tree からのオフライン導出（infra/deploy/derive_buildspec_generations.py）
    # で、live からの写経ではない。導出値 == live == 本台帳の 3 点一致を CI が固定する。
    "554ede59c17e336301ce4aed90cbc6c2171c26faf02df46173fd54c908b621b6" = {
      content_type                  = "text/yaml"
      object_lock_retain_until_date = "2099-12-31T00:00:00Z"
    }
    # 2026-09-01 publish 世代（Wave4・便γ）。契約の node pin/lock 追随（PR #363）による
    # 再レンダリング。値の出所は repo tree からのオフライン導出（校正済みハーネスで
    # Wave3 4値の完全再現を確認済み）。publish 儀式は同日 admin CLI（Wave3 と同一経路）。
    "82e30f81d8b766a80b974acf9a141ee7787a5b2bcf40928ee444a0ac12ec5151" = {
      content_type                  = "text/yaml"
      object_lock_retain_until_date = "2099-12-31T00:00:00Z"
    }
  }

  approval_publisher_resolved_source_buildspec_generations = {
    # 2026-08-13 09:59 UTC publish 世代。retain-until は実体実測の 23:59:59Z
    # （他世代の 00:00:00Z と異なる。ここを共通定数で潰さないこと）
    "33e2a64353969f75e941a9524fcd76919c2dfcc7d192aabb67ecc09c9921ddf4" = {
      content_type                  = "binary/octet-stream"
      object_lock_retain_until_date = "2099-12-31T23:59:59Z"
    }
  }
}

# ── 世代リソース（append-only・create ではなく adopt でのみ state に入る）──────────

resource "aws_s3_object" "mcp_source_publisher_buildspec_generation" {
  for_each = local.mcp_source_publisher_buildspec_generations

  bucket                        = aws_s3_bucket.image_release_evidence.id
  key                           = "codebuild-buildspecs/${local.mcp_source_publisher_project_name}/${each.key}.yml"
  content_type                  = each.value.content_type
  server_side_encryption        = "aws:kms"
  kms_key_id                    = aws_kms_key.image_release_evidence.arn
  bucket_key_enabled            = true
  object_lock_mode              = "GOVERNANCE"
  object_lock_retain_until_date = each.value.object_lock_retain_until_date

  depends_on = [
    aws_s3_bucket_object_lock_configuration.image_release_evidence,
    aws_s3_bucket_policy.image_release_evidence,
  ]

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_object" "image_attestor_buildspec_generation" {
  for_each = local.image_attestor_buildspec_generations

  bucket                        = aws_s3_bucket.image_release_evidence.id
  key                           = "codebuild-buildspecs/${local.image_attestor_project_name}/${each.key}.yml"
  content_type                  = each.value.content_type
  server_side_encryption        = "aws:kms"
  kms_key_id                    = aws_kms_key.image_release_evidence.arn
  bucket_key_enabled            = true
  object_lock_mode              = "GOVERNANCE"
  object_lock_retain_until_date = each.value.object_lock_retain_until_date

  depends_on = [
    aws_s3_bucket_object_lock_configuration.image_release_evidence,
    aws_s3_bucket_policy.image_release_evidence,
  ]

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_object" "image_promoter_buildspec_generation" {
  for_each = local.image_promoter_buildspec_generations

  bucket                        = aws_s3_bucket.image_release_evidence.id
  key                           = "codebuild-buildspecs/${local.image_promoter_project_name}/${each.key}.yml"
  content_type                  = each.value.content_type
  server_side_encryption        = "aws:kms"
  kms_key_id                    = aws_kms_key.image_release_evidence.arn
  bucket_key_enabled            = true
  object_lock_mode              = "GOVERNANCE"
  object_lock_retain_until_date = each.value.object_lock_retain_until_date

  depends_on = [
    aws_s3_bucket_object_lock_configuration.image_release_evidence,
    aws_s3_bucket_policy.image_release_evidence,
  ]

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_object" "approval_publisher_resolved_source_buildspec_generation" {
  for_each = local.approval_publisher_resolved_source_buildspec_generations

  bucket                        = aws_s3_bucket.image_release_evidence.id
  key                           = "codebuild-buildspecs/${local.approval_publisher_project_name}/${each.key}.yml"
  content_type                  = each.value.content_type
  server_side_encryption        = "aws:kms"
  kms_key_id                    = aws_kms_key.image_release_evidence.arn
  bucket_key_enabled            = true
  object_lock_mode              = "GOVERNANCE"
  object_lock_retain_until_date = each.value.object_lock_retain_until_date

  depends_on = [
    aws_s3_bucket_object_lock_configuration.image_release_evidence,
    aws_s3_bucket_policy.image_release_evidence,
  ]

  lifecycle {
    prevent_destroy = true
  }
}

# ── content-addressed 性の強制 ────────────────────────────────────────────────
# Terraform が保持している buildspec body の sha256 が、必ず台帳に登録済みの世代で
# あることを要求する。body だけ変えて世代を登録し忘れる（＝実体の無い key を CodeBuild へ
# 指してしまう）事故を止める。content を object に持たせない代償をここで埋めている。

check "mcp_source_publisher_buildspec_generation_is_registered" {
  assert {
    condition = contains(
      keys(local.mcp_source_publisher_buildspec_generations),
      local.mcp_source_publisher_buildspec_sha256,
    )
    error_message = "mcp-source-publisher buildspec の現行 body の sha256 が世代台帳に未登録です。実体を publish してから supply_chain_adopt.tf の台帳と infra/deploy/supply_chain_adoptions.json へ追記してください。"
  }
}

check "image_attestor_buildspec_generation_is_registered" {
  assert {
    condition = contains(
      keys(local.image_attestor_buildspec_generations),
      local.image_attestor_buildspec_sha256,
    )
    error_message = "image-attestor buildspec の現行 body の sha256 が世代台帳に未登録です。実体を publish してから supply_chain_adopt.tf の台帳と infra/deploy/supply_chain_adoptions.json へ追記してください。"
  }
}

check "image_promoter_buildspec_generation_is_registered" {
  assert {
    condition = contains(
      keys(local.image_promoter_buildspec_generations),
      local.image_promoter_buildspec_sha256,
    )
    error_message = "image-promoter buildspec の現行 body の sha256 が世代台帳に未登録です。実体を publish してから supply_chain_adopt.tf の台帳と infra/deploy/supply_chain_adoptions.json へ追記してください。"
  }
}

check "approval_publisher_resolved_source_buildspec_generation_is_registered" {
  assert {
    condition = contains(
      keys(local.approval_publisher_resolved_source_buildspec_generations),
      local.approval_publisher_buildspec_sha256,
    )
    error_message = "approval-publisher resolved-source buildspec の現行 body の sha256 が世代台帳に未登録です。実体を publish してから supply_chain_adopt.tf の台帳と infra/deploy/supply_chain_adoptions.json へ追記してください。"
  }

  # 旧 aws_s3_object.approval_publisher_resolved_source_buildspec が precondition で
  # 守っていた不変条件の移植: resolved-source 世代は bootstrap 世代の key と衝突しない。
  assert {
    condition = !contains(
      keys(local.approval_publisher_resolved_source_buildspec_generations),
      local.approval_publisher_bootstrap_buildspec_expected_sha256,
    )
    error_message = "resolved-source の世代台帳が bootstrap 世代の content-addressed key と衝突しています。"
  }
}

# ── 移行ブロック（PR2-A0 の activation 用・一度 adopt したら削除してよい）──────────
# removed は state から外すだけで実体には触れない（destroy = false）。S3 実体は Object Lock と
# bucket policy の Delete Deny により、そもそも Terraform からも admin からも削除できない。
# import は既に publish 済みの実体を新しい hash-keyed アドレスへ取り込む（create ではない）。

removed {
  from = aws_s3_object.mcp_source_publisher_buildspec

  lifecycle {
    destroy = false
  }
}

removed {
  from = aws_s3_object.image_attestor_buildspec

  lifecycle {
    destroy = false
  }
}

removed {
  from = aws_s3_object.image_promoter_buildspec

  lifecycle {
    destroy = false
  }
}

removed {
  from = aws_s3_object.approval_publisher_resolved_source_buildspec

  lifecycle {
    destroy = false
  }
}

import {
  to = aws_s3_object.mcp_source_publisher_buildspec_generation["a20ce391f39d57bfce922f77008a55c1106bbe5d8cd2d1d4e55de7fd71e758cc"]
  id = "teamagent-dev-image-release-evidence/codebuild-buildspecs/teamagent-dev-mcp-source-publisher/a20ce391f39d57bfce922f77008a55c1106bbe5d8cd2d1d4e55de7fd71e758cc.yml"
}

import {
  to = aws_s3_object.image_attestor_buildspec_generation["cfe30ec490d3561dc50e995f81921069ed00fa822af2c0cf5f3aabaa6e88cb2a"]
  id = "teamagent-dev-image-release-evidence/codebuild-buildspecs/teamagent-dev-image-attestor/cfe30ec490d3561dc50e995f81921069ed00fa822af2c0cf5f3aabaa6e88cb2a.yml"
}

import {
  to = aws_s3_object.image_promoter_buildspec_generation["82e30f81d8b766a80b974acf9a141ee7787a5b2bcf40928ee444a0ac12ec5151"]
  id = "teamagent-dev-image-release-evidence/codebuild-buildspecs/teamagent-dev-image-promoter/82e30f81d8b766a80b974acf9a141ee7787a5b2bcf40928ee444a0ac12ec5151.yml"
}

import {
  to = aws_s3_object.approval_publisher_resolved_source_buildspec_generation["33e2a64353969f75e941a9524fcd76919c2dfcc7d192aabb67ecc09c9921ddf4"]
  id = "teamagent-dev-image-release-evidence/codebuild-buildspecs/teamagent-dev-approval-publisher/33e2a64353969f75e941a9524fcd76919c2dfcc7d192aabb67ecc09c9921ddf4.yml"
}
