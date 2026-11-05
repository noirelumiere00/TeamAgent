#!/usr/bin/env bash
# 一度きり・冪等: 動画ライブラリのサムネ（raw_files の video-library/thumbs/）への S3 権限を付与する（2026-10-30）。
#   ingest タスク: PutObject・GetObject（同期の後段でサムネを保存）
#   connect-web タスク: PutObject・GetObject（/library/thumb で配信・11/04 から URL 取り込みの worker がサムネを保存）
# bootstrap_vseo_s3_iam.sh と同じ流儀: 別名の inline policy（terraform apply で剥がれない）、put-role-policy は同名上書きで冪等。
# tf 側の真実源は connect_web.tf / ingest_schedule.tf の VideoLibraryThumbnails statement（取込時に重複しても無害）。
# role 名はハードコードせず live の taskRoleArn から解決する。
# 戻し方: aws iam delete-role-policy --role-name <role> --policy-name VideoLibraryThumbnailsBootstrap
set -euo pipefail
export AWS_PROFILE=${AWS_PROFILE:-aiiadev-mfa} AWS_REGION=ap-northeast-1 AWS_PAGER=""
BUCKET=teamagent-dev-raw-files
PREFIX=video-library/thumbs
NAME=VideoLibraryThumbnailsBootstrap

role_of() { aws ecs describe-task-definition --task-definition "$1" --query 'taskDefinition.taskRoleArn' --output text | sed 's#.*/##'; }

put() {  # <role> <actions-json>
  local role="$1" actions="$2" doc
  doc=$(printf '{"Version":"2012-10-17","Statement":[{"Sid":"VideoLibraryThumbnails","Effect":"Allow","Action":%s,"Resource":"arn:aws:s3:::%s/%s/*"}]}' "$actions" "$BUCKET" "$PREFIX")
  aws iam put-role-policy --role-name "$role" --policy-name "$NAME" --policy-document "$doc"
  echo "== $role: $(aws iam get-role-policy --role-name "$role" --policy-name "$NAME" --query 'PolicyDocument.Statement[0].[Action,Resource]' --output json | tr -d ' \n')"
}

put "$(role_of teamagent-dev-ingest)" '["s3:PutObject","s3:GetObject"]'
put "$(role_of teamagent-dev-connect-web)" '["s3:PutObject","s3:GetObject"]'
