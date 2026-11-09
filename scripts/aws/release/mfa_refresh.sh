#!/opt/homebrew/bin/bash
# aiiadev-mfa プロファイルの MFA セッションを更新する（12 時間）。
# ~/bin/teamagent-build-mfa.sh と同じ IAM ユーザー・同じ MFA デバイスを使い、結果を
# `aws configure set ... --profile aiiadev-mfa` に書く（発射・TD 差し替え・S3 配置の各スクリプトが読む）。
# ワンタイムコードはこの場で読み取り、環境変数にもファイルにも残さない。
set -euo pipefail
RELEASE_PROFILE="${RELEASE_PROFILE:-aiiadev-mfa}"
export AWS_CA_BUNDLE="${AWS_CA_BUNDLE:-$(python3 -c 'import certifi;print(certifi.where())')}"
export AWS_REGION=ap-northeast-1 AWS_PAGER=""
# PROFILE / MFA_SERIAL は既存スクリプトの定義を流用（値をここに複製しない）
if [ -n "${MFA_BASE_PROFILE:-}" ] && [ -n "${MFA_SERIAL:-}" ]; then
  PROFILE="$MFA_BASE_PROFILE"
else
  eval "$(grep -E '^(PROFILE|MFA_SERIAL)=' ~/bin/teamagent-build-mfa.sh)"
fi
read -r -p "MFA コード(6桁): " CODE
CODE="$(printf '%s' "$CODE" | tr -cd '0-9')"
[ "${#CODE}" = 6 ] || { echo "6 桁ではありません"; exit 1; }
# 2026-09-17 裁定: セッションは 36 時間（GetSessionToken の上限 129600 秒）。アカウント側で拒否されたら 43200 に戻す
J="$(aws sts get-session-token --profile "$PROFILE" --serial-number "$MFA_SERIAL" --token-code "$CODE" --duration-seconds 129600 --output json)"
python3 - "$J" "$RELEASE_PROFILE" <<'PY'
import json, subprocess, sys
c = json.loads(sys.argv[1])["Credentials"]
for k, v in (("aws_access_key_id", c["AccessKeyId"]), ("aws_secret_access_key", c["SecretAccessKey"]), ("aws_session_token", c["SessionToken"])):
    subprocess.run(["aws", "configure", "set", k, v, "--profile", sys.argv[2]], check=True)
print(sys.argv[2], "を更新しました（失効:", c["Expiration"], "）")
PY
aws sts get-caller-identity --profile "$RELEASE_PROFILE" --query Arn --output text
