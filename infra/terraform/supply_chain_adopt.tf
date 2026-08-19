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
# 【属性値の出所】各世代の content_type / object_lock_retain_until_date は **実体の値**。
# 従来の単一リソース定義は content_type="text/yaml" / retain_until=23:59:59 を宣言していたが、
# admin が publish した実体は binary/octet-stream / 00:00:00 で食い違っていた。実体に
# 合わせないと import 直後に差分が出る（PR2-A0 で read-only plan により実測）。
# 詳細は docs/runbooks/supply_chain_adopt.md を参照。

locals {
  # 実体が publish 済みの世代に共通する属性。
  adopted_buildspec_content_type      = "binary/octet-stream"
  adopted_buildspec_retain_until_date = "2099-12-31T00:00:00Z"

  mcp_source_publisher_buildspec_generations = {
    "c47473411fea400668ebec0628e81d521c9b28f971320a6d0336204a1c3e25ce" = {
      content_type                  = local.adopted_buildspec_content_type
      object_lock_retain_until_date = local.adopted_buildspec_retain_until_date
    }
  }

  image_attestor_buildspec_generations = {
    "6a3d489cd3c29b5bb90b85094f98765027a1580befeb9642763f185f830cfb8c" = {
      content_type                  = local.adopted_buildspec_content_type
      object_lock_retain_until_date = local.adopted_buildspec_retain_until_date
    }
  }

  image_promoter_buildspec_generations = {
    "bbc67883a4f03a40187588cf0bddc3a23e2cf6a331b8692945570a88e6fcb1c9" = {
      content_type                  = local.adopted_buildspec_content_type
      object_lock_retain_until_date = local.adopted_buildspec_retain_until_date
    }
  }

  approval_publisher_resolved_source_buildspec_generations = {
    "33e2a64353969f75e941a9524fcd76919c2dfcc7d192aabb67ecc09c9921ddf4" = {
      content_type                  = local.adopted_buildspec_content_type
      object_lock_retain_until_date = local.adopted_buildspec_retain_until_date
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
  to = aws_s3_object.mcp_source_publisher_buildspec_generation["c47473411fea400668ebec0628e81d521c9b28f971320a6d0336204a1c3e25ce"]
  id = "teamagent-dev-image-release-evidence/codebuild-buildspecs/teamagent-dev-mcp-source-publisher/c47473411fea400668ebec0628e81d521c9b28f971320a6d0336204a1c3e25ce.yml"
}

import {
  to = aws_s3_object.image_attestor_buildspec_generation["6a3d489cd3c29b5bb90b85094f98765027a1580befeb9642763f185f830cfb8c"]
  id = "teamagent-dev-image-release-evidence/codebuild-buildspecs/teamagent-dev-image-attestor/6a3d489cd3c29b5bb90b85094f98765027a1580befeb9642763f185f830cfb8c.yml"
}

import {
  to = aws_s3_object.image_promoter_buildspec_generation["bbc67883a4f03a40187588cf0bddc3a23e2cf6a331b8692945570a88e6fcb1c9"]
  id = "teamagent-dev-image-release-evidence/codebuild-buildspecs/teamagent-dev-image-promoter/bbc67883a4f03a40187588cf0bddc3a23e2cf6a331b8692945570a88e6fcb1c9.yml"
}

import {
  to = aws_s3_object.approval_publisher_resolved_source_buildspec_generation["33e2a64353969f75e941a9524fcd76919c2dfcc7d192aabb67ecc09c9921ddf4"]
  id = "teamagent-dev-image-release-evidence/codebuild-buildspecs/teamagent-dev-approval-publisher/33e2a64353969f75e941a9524fcd76919c2dfcc7d192aabb67ecc09c9921ddf4.yml"
}
