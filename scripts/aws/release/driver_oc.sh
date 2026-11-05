#!/usr/bin/env bash
# 承認用の型。変更時は drafts でレビューし、改名で再承認する。
#   ① 発射台（teamagent-build・branch dev）を origin/dev へ ff → ② build_openclaw_image.sh（publisher → attestor → promoter）
#   → ③ authorize（active・consumer manifest は 09-04 正本の openclaw digest を差し替え）→ ④ OC TD 差し替え（TRACE env は撤去）
# 各段 fail-closed。進捗は STATUS に 1 行ずつ。09-08 の driver_0908_oc6.sh と同型。
#
# 前提: #441 が dev にマージ済み（#405 は 09-17 の TD:48 で本番入り済み）。mcp の契約・世代とは独立（OC は専用 publisher ロール／KMS／ECR、attestor/promoter は
#       PIPELINE=openclaw で契約ピン条件外）。MFA セッション（aiiadev-mfa）が有効なこと。
set -euo pipefail

# 検査器もこの型の承認対象。便データを source/eval しない。
release_data() {
  python3 - "$@" <<'RELEASE_DATA_PY'
import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath


def require(ok, message):
    if not ok:
        raise ValueError(message)


def string(value):
    return isinstance(value, str) and bool(value.strip()) and not any(ord(c) < 32 for c in value)


def safe_path(value):
    require(string(value), 'path は空でない文字列が必要')
    require(not PurePosixPath(value).is_absolute() and not any(p in ('..', '.', '') for p in value.split('/'))
            and not any(c in value for c in '\\:'), '絶対パス・..・特殊なパスは禁止: ' + value)


def validate(data, pipeline):
    fields = {'name', 'description', 'checks'} | ({'OLD_CORE'} if pipeline == 'mcp' else set())
    require(isinstance(data, dict) and set(data) == fields, '便データの欄が不足/未知')
    require(string(data['name']) and re.fullmatch(r'r[0-9]+' if pipeline == 'mcp' else r'oc[0-9]+', data['name']), '便名が不正')
    require(string(data['description']), 'description が不正')
    if pipeline == 'mcp':
        require(isinstance(data['OLD_CORE'], str) and re.fullmatch(r'sha256:[0-9a-f]{64}', data['OLD_CORE']), 'OLD_CORE が不正')
    require(isinstance(data['checks'], list) and bool(data['checks']), 'checks は空でない配列が必要')
    for c in data['checks']:
        require(isinstance(c, dict), '確認はオブジェクトが必要')
        require(string(c.get('label')), 'label が不正')
        kind = c.get('kind')
        require(kind in ('grep_fixed', 'grep_regex', 'file_exists', 'lock_version'), '未知の kind')
        allowed = {'label', 'kind', 'path'}
        if pipeline == 'oc':
            allowed.add('phase')
            require(c.get('phase') in ('before_mcp', 'after_mcp'), 'phase が不正')
        paths = c.get('path')
        if isinstance(paths, list):
            require(kind.startswith('grep_') and bool(paths), 'path 配列は grep のみ')
            for path in paths:
                safe_path(path)
        else:
            safe_path(paths)
        if kind != 'file_exists':
            allowed.add('pattern')
            require(string(c.get('pattern')), 'pattern は空でない1行の文字列が必要')
        if kind.startswith('grep_'):
            allowed |= {'absent', 'whole_line', 'count'}
            if kind == 'grep_regex':
                allowed.add('dialect')
                require(c.get('dialect', 'basic') in ('basic', 'extended'), 'dialect が不正')
            if 'count' in c:
                require(type(c['count']) is int and c['count'] >= 0 and 'absent' not in c and isinstance(paths, str), 'count が不正')
            require(not c.get('whole_line') or isinstance(paths, str), '行全体の検査は単一ファイルが必要')
            # Compile with the same grep dialect; patterns are argv data, never shell code.
            result = subprocess.run(grep_args(c), input=b'', capture_output=True)
            require(result.returncode in (0, 1), 'grep pattern が不正: ' + c['label'])
        elif kind == 'file_exists':
            allowed.add('absent')
        else:
            allowed.add('format')
            fmt = c.get('format')
            require(fmt in ('uv_exact', 'uv_adjacent', 'apk_arg', 'json_value', 'json_array_length', 'json_no_package_prefix'), 'lock format が不正')
            if fmt in ('uv_exact', 'uv_adjacent', 'apk_arg'):
                allowed.add('package')
                require(string(c.get('package')), 'package が不正')
            if fmt == 'uv_adjacent':
                allowed.add('whole_line')
            if fmt == 'apk_arg':
                allowed.add('arg_path')
                safe_path(c.get('arg_path'))
                require(re.fullmatch(r'[A-Z][A-Z0-9_]*', c['pattern']), 'ARG 名が不正')
            if fmt in ('json_value', 'json_array_length'):
                allowed.add('field')
                require(isinstance(c.get('field'), list) and bool(c['field']) and all(string(f) for f in c['field']), 'field が不正')
            if fmt == 'json_array_length':
                require(re.fullmatch(r'0|[1-9][0-9]*', c['pattern']), '配列件数が不正')
        require(not set(c) - allowed, '未知の確認欄（コマンド欄は禁止）: ' + c['label'])
        for flag in ('absent', 'whole_line'):
            require(flag not in c or type(c[flag]) is bool, flag + ' は boolean が必要')
    if pipeline == 'oc':
        phases = [c['phase'] for c in data['checks']]
        require(phases == sorted(phases, key=lambda p: p == 'after_mcp'), 'phase の順序が不正')
    return data


def grep_args(c):
    args = ['grep', '-F' if c['kind'] == 'grep_fixed' else ('-E' if c.get('dialect') == 'extended' else '-G')]
    if c.get('whole_line'):
        args.append('-x')
    args += ['-c' if 'count' in c else '-q', '-e', c['pattern']]
    return args


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], capture_output=True)


def local_path(root, path):
    target = root / path
    require(target.resolve().is_relative_to(root.resolve()), '作業ツリー外への symlink は禁止: ' + path)
    return target


def read_text(root, path, pipeline):
    if pipeline == 'mcp':
        result = git(root, 'show', 'HEAD:' + path)
        require(result.returncode == 0, 'HEAD を読めない: ' + path)
        return result.stdout.decode()
    return local_path(root, path).read_text()


def check_one(c, root, pipeline):
    path = c['path']
    if c['kind'] == 'file_exists':
        if pipeline == 'mcp':
            if c.get('absent'):
                result = git(root, 'ls-tree', '--name-only', 'HEAD', '--', ':(literal)' + path)
                require(result.returncode == 0, 'HEAD を読めない: ' + path)
                return not result.stdout
            result = git(root, 'cat-file', '-e', 'HEAD:' + path)
            exists = result.returncode == 0
        else:
            try:
                local_path(root, path).stat()
                exists = True
            except FileNotFoundError:
                exists = False
        return not exists if c.get('absent') else exists
    if c['kind'].startswith('grep_'):
        paths = path if isinstance(path, list) else [path]
        args = grep_args(c)
        if pipeline == 'mcp' and (c.get('whole_line') or 'count' in c):
            # git grep has no -x; the reference uses git show | grep here.
            require(len(paths) == 1, '行一致/件数確認は単一ファイルが必要')
            text = read_text(root, paths[0], pipeline)
            result = subprocess.run(args, input=text.encode(), capture_output=True)
        elif pipeline == 'mcp':
            if c.get('absent'):
                for target in paths:
                    require(git(root, 'cat-file', '-e', 'HEAD:' + target).returncode == 0, 'HEAD の検査対象が無い: ' + target)
            result = git(root, 'grep', *args[1:], 'HEAD', '--', *paths)
        else:
            result = subprocess.run([*args, '--', *(str(local_path(root, p)) for p in paths)], capture_output=True)
        require(result.returncode in (0, 1), 'grep 読取/構文エラー: ' + c['label'])
        if 'count' in c:
            return int(result.stdout) == c['count']
        found = result.returncode == 0
        return not found if c.get('absent') else found
    text = read_text(root, path, pipeline)
    fmt, pattern = c['format'], c['pattern']
    if fmt == 'uv_exact':
        versions = re.findall(r'\[\[package\]\]\nname = "' + re.escape(c['package']) + r'"\nversion = "([^"]+)"', text)
        return versions == [pattern]
    if fmt == 'uv_adjacent':
        lines = text.splitlines()
        following = [line for i, line in enumerate(lines) if i > 0 and lines[i-1] == f'name = "{c["package"]}"']
        wanted = f'version = "{pattern}"'
        args = ['grep', '-G', '-q'] + (['-x'] if c.get('whole_line') else []) + ['-e', wanted]
        result = subprocess.run(args, input='\n'.join(following).encode(), capture_output=True)
        require(result.returncode in (0, 1), '版の正規表現が不正')
        return result.returncode == 0
    if fmt == 'apk_arg':
        args = read_text(root, c['arg_path'], pipeline)
        values = re.findall(r'^ARG ' + re.escape(pattern) + r'=(.*)$', args, re.M)
        version = '\n'.join(values)
        if not version:
            return False
        result = subprocess.run(['grep', '-G', '-q', '-x', '-e', f'{c["package"]}-{version}'], input=text.encode(), capture_output=True)
        require(result.returncode in (0, 1), 'apk の版の正規表現が不正')
        return result.returncode == 0
    value = json.loads(text)
    if fmt == 'json_no_package_prefix':
        return not any(e['package'].startswith(pattern) for e in value['exceptions'])
    for field in c['field']:
        value = value[field]
    if fmt == 'json_array_length':
        require(isinstance(value, list), 'JSON の検査対象が配列ではない')
        return len(value) == int(pattern)
    return value == pattern


def run_checks(data, root, pipeline, phase=''):
    for c in data['checks']:
        if phase and c['phase'] != phase:
            continue
        require(check_one(c, Path(root), pipeline), c['label'])
        print('CHECK OK: ' + c['label'], flush=True)


def main():
    action, pipeline, payload, *args = sys.argv[1:]
    if action == 'validate':
        def unique(pairs):
            result = {}
            for key, value in pairs:
                require(key not in result, 'JSON の重複キー: ' + key)
                result[key] = value
            return result
        data = json.loads(Path(payload).read_text(), object_pairs_hook=unique)
        print(json.dumps(validate(data, pipeline), ensure_ascii=False))
    elif action == 'field':
        print(json.loads(payload)[args[0]])
    elif action == 'checks':
        run_checks(json.loads(payload), args[0], pipeline, args[1] if len(args) > 1 else '')
    else:
        raise ValueError('未知の操作')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError, IndexError) as error:
        print('FATAL: ' + str(error), file=sys.stderr)
        sys.exit(1)
RELEASE_DATA_PY
}

if [ "$#" -ne 1 ]; then
  echo "usage: $0 releases/ocNN.json" >&2
  exit 2
fi
RELEASE_DATA=$(release_data validate oc "$1")
readonly RELEASE_DATA
RELEASE_NAME=$(release_data field oc "$RELEASE_DATA" name)
printf '== %s: %s\n' "$RELEASE_NAME" "$(release_data field oc "$RELEASE_DATA" description)"

export SSL_CERT_FILE="/opt/homebrew/lib/python3.14/site-packages/certifi/cacert.pem"
export AWS_CA_BUNDLE="$SSL_CERT_FILE"
export AWS_PAGER=""
PAD=/Users/s-komata/dev/worktrees/teamagent-build
R=ap-northeast-1
B=teamagent-dev-image-release-evidence
S=/Users/s-komata/Documents/Claude/Scheduled/aico-release-tools
STATUS=/tmp/oc_driver_1016b.status
OLD_OC=sha256:5530dbe44ed4e63146d60a8ab7df5b47b8d02c3a50191bbcdcdfff2958404744   # 雛形 manifest 0904e の openclaw digest（置換の目印。本番の現行 TD:48 は 81ce5e8c…）
mark(){ echo "$(date '+%H:%M:%S') $*" | tee -a "$STATUS"; }
die(){ mark "FATAL: $*"; exit 1; }
: > "$STATUS"

# ⓪ セッション
aws sts get-caller-identity --profile aiiadev-mfa --region $R --query Arn --output text >/dev/null || die "aiiadev-mfa セッション無効（mfa_refresh.sh）"
mark "session ok"

# ① 発射台を dev 先頭へ
cd "$PAD"
git fetch -q origin dev || die "fetch failed"
[ "$(git symbolic-ref --short HEAD)" = "dev" ] || die "発射台が dev ブランチではない"
git merge --ff-only -q origin/dev || die "ff-only merge failed（発射台に独自コミット?）"
[ -z "$(git status --porcelain=v1 --untracked-files=all)" ] || die "発射台が dirty（build_openclaw_image.sh が拒否する）"
TIP=$(git rev-parse HEAD)
mark "発射台 dev @ ${TIP:0:7}"
release_data checks oc "$RELEASE_DATA" "$PAD" before_mcp
# #548 のツールは mcp r48 で増える。mcp 側に無いうちに OC を差し替えない（ツール一覧の整合）。
MCP_IMG=$(aws ecs describe-task-definition --profile aiiadev-mfa --region $R --task-definition teamagent-dev-mcp --query 'taskDefinition.containerDefinitions[0].image' --output text)
echo "$MCP_IMG" | grep -qE '48311f61|bfa95437' && die "mcp がまだ r47/r49（$MCP_IMG）。先に r50（#562 許可リスト・#565 の k を受ける側）を本番に入れる。逆順だと誰の依頼でも 83 枚のジョブが走り、評価ボタンの押下が AFB_TOKEN_INVALID になる"
release_data checks oc "$RELEASE_DATA" "$PAD" after_mcp
mark "積み荷 $RELEASE_NAME 確認 OK"
# ①' 撃つ前チェック（09-17 の 4 つ目の壁）: CodeBuild に焼き込まれた OC 手順書の契約 sha が dev の契約と一致すること
DEV_CONTRACT_SHA=$(shasum -a 256 infra/codebuild/openclaw_bundle_contract.json | awk '{print $1}')
LIVE_PIN_HITS=$(aws codebuild batch-get-projects --profile aiiadev-mfa --region $R --names teamagent-dev-openclaw-provenance-builder --query 'projects[0].source.buildspec' --output text | grep -c "$DEV_CONTRACT_SHA" || true)
[ "$LIVE_PIN_HITS" = "2" ] || die "OC 手順書（inline buildspec）の契約 sha が dev（${DEV_CONTRACT_SHA:0:8}…）と不一致（出現 $LIVE_PIN_HITS/2）。apply_oc_contract_pin_*.sh 型で差し替えてから撃つ"
mark "契約ピン一致 ${DEV_CONTRACT_SHA:0:8}…"

# ② OpenClaw イメージ build（publisher role を assume）
export AWS_PROFILE=aiiadev-mfa
bash infra/deploy/build_openclaw_image.sh --poll-seconds 20 --timeout-seconds 5400 2>&1 | tee /tmp/oc_build_1016b.log | grep -E "SUCCEEDED|FAILED|FATAL|verified|release_receipt" || true
grep -q "OpenClaw core build verified" /tmp/oc_build_1016b.log || die "OC build が完走していない（/tmp/oc_build_1016b.log）"
RECEIPT_KEY=$(grep -oE "release_receipt_key=\S+" /tmp/oc_build_1016b.log | tail -1 | cut -d= -f2)
RV=$(grep -oE "release_receipt_version_id=\S+" /tmp/oc_build_1016b.log | tail -1 | cut -d= -f2)
SV=$(grep -oE "release_signature_version_id=\S+" /tmp/oc_build_1016b.log | tail -1 | cut -d= -f2)
[ -n "$RECEIPT_KEY" ] && [ -n "$RV" ] && [ -n "$SV" ] || die "receipt 情報の抽出失敗"
mark "OC build verified receipt=$RECEIPT_KEY"

aws s3api get-object --profile aiiadev-mfa --bucket "$B" --key "$RECEIPT_KEY" --version-id "$RV" "$S/oc-release/receipt-1016b.json" --region $R --query ContentLength --output text >/dev/null
NEW_OC=$(python3 - "$S" <<'PY'
import json,sys
r=json.load(open(f"{sys.argv[1]}/oc-release/receipt-1016b.json"))
subj={s["name"]:s["digest"] for s in r["subjects"]}
d=subj.get("core") or next(iter(subj.values()))
assert d.startswith("sha256:"), d
print(d)
PY
) || die "receipt から digest 抽出失敗"
mark "candidate openclaw digest=$NEW_OC"

# ③ authorize（active）: 09-04 正本 manifest の openclaw digest（=TD:47 の現行）を差し替え
python3 - "$S" "$OLD_OC" "$NEW_OC" <<'PY' || die "manifest 生成失敗"
import json,sys
S,old,new=sys.argv[1:4]
base=open(f"{S}/oc-release/release-consumer-manifest-0904e.json",encoding="utf-8").read()
n=base.count(old); assert n==2, f"旧 openclaw digest 出現数 {n}（期待2）"
out=base.replace(old,new); m=json.loads(out); assert m["mode"]=="receipt-required"
open(f"{S}/oc-release/release-consumer-manifest-1016b.json","w",encoding="utf-8").write(out); print("manifest ok")
PY
export AWS_ACCESS_KEY_ID="$(aws configure get aws_access_key_id --profile aiiadev-mfa)"
export AWS_SECRET_ACCESS_KEY="$(aws configure get aws_secret_access_key --profile aiiadev-mfa)"
export AWS_SESSION_TOKEN="$(aws configure get aws_session_token --profile aiiadev-mfa)"
unset AWS_PROFILE
rm -f "$S/oc-release/gate-vars-1016b.tfvars.json"
bash infra/deploy/authorize_image_release.sh --pipeline openclaw --channel active \
  --receipt-key "$RECEIPT_KEY" --receipt-version-id "$RV" --receipt-signature-version-id "$SV" \
  --consumer-manifest "$S/oc-release/release-consumer-manifest-1016b.json" \
  --terraform-gate-vars-out "$S/oc-release/gate-vars-1016b.tfvars.json" \
  2>&1 | tee /tmp/oc_authorize_1016b.log | tail -12
[ -s "$S/oc-release/gate-vars-1016b.tfvars.json" ] || die "authorize が gate-vars を出していない（/tmp/oc_authorize_1016b.log）"
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
mark "authorize ok"
aws ecr describe-images --profile aiiadev-mfa --repository-name teamagent-openclaw --region $R --image-ids imageDigest="$NEW_OC" --query 'imageDetails[0].imageTags' --output json | tee -a "$STATUS"

# ④ OC TD 差し替え（Aico は数分再起動）。env は現行 TD:48 を継承（image だけ）
# 10-07 小俣さん「皆さんに公開してテストしてもらいましょう」→ 評価ボタン（#540 plugin 側）を全員 ON。
bash "$S/oc-release/register_oc_td_image_env.sh" "$NEW_OC" TEAMAGENT_ANSWER_FEEDBACK=1 || die "OC TD swap failed"
mark "OC TD swap done digest=$NEW_OC"
mark "🏁 driver complete — 実機 QA（小俣さん DM）: ① 食品メーカーのショート動画の事例を頼み、📊 動画ごとの数字（5 本・保存数・リンク）がそのまま出ること ② 通常の検索・お土産の受付が動くこと ③ 評価ボタンが出ること"
