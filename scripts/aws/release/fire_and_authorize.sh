#!/usr/bin/env bash
# 承認用の型。変更時は drafts でレビューし、改名で再承認する。
# なぜ連続でなければならないか（2026-09-11 に実際に踏んだ）:
#   段1 が作る承認レコードには **1 時間の有効期限**がある（approval-records の expires_at_utc）。
#   便そのものは 15 分ほどで 5 段完走するが、そこで手を止めて後から authorize しようとすると
#   'approval payload validation failed: approval is expired' で弾かれる。
#   17:21 に完走した便を 18:43 に authorize しようとして失敗した。
#
# もうひとつの注意（同日に踏んだ）:
#   昇格タグ verified-<commit>-<name> はコミットだけから決まるのに、ビルドは再現可能ではなく
#   撃つたびに digest が変わる。よって **同じコミットで撃ち直すと段5 で必ず弾かれる**。
#   撃ち直すには新しいコミットが要る。本スクリプトは発射前にその衝突を検査して止める。
#
set -euo pipefail
RELEASE_PROFILE="${RELEASE_PROFILE:-aiiadev-mfa}"

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

if [ "$#" -ne 2 ]; then
  echo "usage: $0 releases/rNN.json --dry-only | $0 releases/rNN.json \"APPROVED: <理由>\"" >&2
  exit 2
fi
DRY_ONLY=0
APPROVAL="$2"
if [ "$APPROVAL" = "--dry-only" ]; then
  DRY_ONLY=1
  APPROVAL=""
elif [[ ! "$APPROVAL" =~ ^APPROVED:[[:space:]]+[^[:space:]] ]]; then
  echo 'FATAL: --dry-only または APPROVED: <理由> が必要' >&2
  exit 2
fi
RELEASE_DATA=$(release_data validate mcp "$1")
readonly RELEASE_DATA
RELEASE_NAME=$(release_data field mcp "$RELEASE_DATA" name)
OLD_CORE=$(release_data field mcp "$RELEASE_DATA" OLD_CORE)
printf '== %s: %s\n' "$RELEASE_NAME" "$(release_data field mcp "$RELEASE_DATA" description)"


S="${RELEASE_TOOLS_DIR:-/Users/s-komata/Documents/Claude/Scheduled/aico-release-tools}"   # 雛形（oc-release/release-consumer-manifest-0904e.json）の正本。09-14 r23 で session 一時領域を指していて STEP 6 が落ちた
B=teamagent-dev-image-release-evidence
R=ap-northeast-1
PAD="${RELEASE_PAD:-/Users/s-komata/dev/worktrees/teamagent-build}"
OUT=/private/tmp/r67-fire

export SSL_CERT_FILE="/opt/homebrew/lib/python3.14/site-packages/certifi/cacert.pem"
export AWS_CA_BUNDLE="$SSL_CERT_FILE"
export AWS_PROFILE="${RELEASE_PROFILE}"
export AWS_REGION="$R"
export AWS_PAGER=""
export AWS_RETRY_MODE=adaptive AWS_MAX_ATTEMPTS=10  # 09-24: 一時的な接続失敗（r26 で段 3 待ち中に落ちた）で鎖ごと落ちないように


mkdir -p "$OUT"

echo "== 実行主体"
aws sts get-caller-identity --output json

echo
echo "== STEP 1: 発射台を local dev ブランチに立てる"
# authorize は symbolic-ref で dev ブランチを要求する（detach では弾かれる）。
git -C "$PAD" fetch -q origin
if git -C "$PAD" show-ref --verify --quiet refs/heads/dev; then
  git -C "$PAD" checkout -q dev
  git -C "$PAD" merge --ff-only -q origin/dev
else
  git -C "$PAD" checkout -q -b dev origin/dev
fi
BR="$(git -C "$PAD" symbolic-ref --quiet --short HEAD || true)"
[ "$BR" = "dev" ] || { echo "FATAL: dev ブランチに立てませんでした（現在: ${BR:-detached}）"; exit 1; }
[ -z "$(git -C "$PAD" status --porcelain=v1 --untracked-files=all --ignore-submodules=none)" ] \
  || { echo "FATAL: 作業ツリーが汚れています"; git -C "$PAD" status --short; exit 1; }
COMMIT="$(git -C "$PAD" rev-parse HEAD)"
echo "  ブランチ: $BR / HEAD: $(git -C "$PAD" rev-parse --short HEAD)"
echo "  積み荷:"
git -C "$PAD" log --oneline origin/main..HEAD | sed 's/^/    /'

echo
echo "== STEP 1a: media の apk 固定が上流で解決できるか（改善 1・撃ってから段 3 で落ちるのを防ぐ）"
bash "$(dirname "$0")/apkwatch/prefire_apk_check.sh" "$PAD" \
  || { echo "FATAL: apk 在庫確認で止めた（上の理由を参照）。apkwatch/apply_apklock.sh で追随させてから撃つこと"; exit 1; }

echo
echo "== STEP 1b: 積み荷の中身を確かめる"
# 09-24 レビュー R28-01: 前提がコメントだけだと、#436 だけマージした段階で先頭が動き STEP 2 を素通りして
# sysfix の無いイメージが焼かれる。squash で SHA は変わるので中身で判定する。
release_data checks mcp "$RELEASE_DATA" "$PAD"

echo
echo "== STEP 2: 昇格タグの衝突を先に検査する"
# 同じコミットで既に昇格タグが確定していると、段5 で必ず弾かれる。撃つ前に止める。
CLASH=0
for PAIR in "teamagent-mcp-verified-candidates:core" "teamagent-media-worker-verified-candidates:media"; do
  REPO="${PAIR%%:*}"; NAME="${PAIR##*:}"
  TAG="verified-$COMMIT-$NAME"
  D="$(aws ecr describe-images --repository-name "$REPO" --image-ids imageTag="$TAG" \
        --query 'imageDetails[0].imageDigest' --output text 2>/dev/null || true)"
  if [ -n "$D" ] && [ "$D" != "None" ]; then
    echo "  衝突: $REPO の $TAG は既に $D に束縛されています。"
    CLASH=1
  else
    echo "  OK: $REPO に $TAG はまだありません。"
  fi
done
if [ "$CLASH" = "1" ]; then
  echo
  echo "FATAL: このコミットは既に昇格タグを確定させています。撃っても段5 で必ず弾かれます。"
  echo "       dev に新しいコミットを積んでから撃ってください。"
  echo "       （同じコミットでもビルドのたびに digest が変わるのに、タグはコミットから決まるため）"
  exit 1
fi

echo
echo "== STEP 3: 隔離側の候補タグを片付ける"
bash "$(dirname "$0")/purge_candidate_tags.sh" "$COMMIT" | tail -3

echo
echo "== STEP 4: 発射前チェック"
bash "$PAD/scripts/aws/release_mcp.sh" --dry-run

if [ "$DRY_ONLY" = "1" ]; then
  echo
  echo "--dry-only のためここで止まります。"
  exit 0
fi

echo
echo "== STEP 5: 発射（既存の MFA セッションを再利用）"
PATCHED=/tmp/release_mcp_r45_session.sh
python3 - "$PAD/scripts/aws/release_mcp.sh" "$PATCHED" "$PAD" "$RELEASE_PROFILE" <<'PY'
import shlex
import sys

src, dst, launch, profile = sys.argv[1:5]
s = open(src, encoding="utf-8").read()
start = s.index('read -r -s -p "MFAコード')
end_marker = 'BASE_TK="$(echo "$BASE" | cut -f3)"\n'
end = s.index(end_marker) + len(end_marker)
repl = (
  f'RELEASE_PROFILE={shlex.quote(profile)}\n'
  'info "MFA 付きセッション（${RELEASE_PROFILE} プロファイル）を再利用"\n'
  'BASE_AK="$(aws configure get aws_access_key_id --profile "${RELEASE_PROFILE}")"\n'
  'BASE_SK="$(aws configure get aws_secret_access_key --profile "${RELEASE_PROFILE}")"\n'
  'BASE_TK="$(aws configure get aws_session_token --profile "${RELEASE_PROFILE}")"\n'
  '[ -n "$BASE_AK" ] && [ -n "$BASE_SK" ] && [ -n "$BASE_TK" ] || die "${RELEASE_PROFILE} の認証情報が読めない"\n'
)
s = s[:start] + repl + s[end:]
s = s.replace('REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"', f'REPO_ROOT="{launch}"')
tail = s.split('info "MFA 付きセッション')[1].split('assume() {')[0]
assert 'get-session-token' not in tail, "GetSessionToken が残っている"
open(dst, "w", encoding="utf-8").write(s)
PY
chmod 700 "$PATCHED"
# 2026-09-16: 免除 gate JSON は「前回ビルドから写す」と旧期限（9/22）を引き継ぎ、段 2 の
# 'forced rollback evidence mismatch' になる。#419 で期限を 10/22 へ延長したので、
# 新 release_evidence.py が期待する gate をそのまま渡す（frj.json は _initial_release_exemption_gate の出力）。
FRJ_FILE="${RELEASE_FRJ_FILE:-/Users/s-komata/Documents/Claude/Artifacts/aico-inputs-20260911/frj_exemption_gate_20261222.json}"
[ -s "$FRJ_FILE" ] || { echo "FATAL: $FRJ_FILE が無い（derive → gate 生成を先に）"; exit 1; }
python3 - "$PATCHED" "$FRJ_FILE" <<'PY2'
import pathlib, sys
p = pathlib.Path(sys.argv[1]); s = p.read_text(encoding="utf-8")
old = '[ -n "$PREV_FRJ" ] && [ "$PREV_FRJ" != "None" ] && FORCED_ROLLBACK_EVIDENCE_JSON="$PREV_FRJ"'
assert old in s, "session script の PREV_FRJ 行が見つからない"
s = s.replace(old, f'FORCED_ROLLBACK_EVIDENCE_JSON="$(cat {sys.argv[2]})"  # 2026-09-16: 新期限の gate を固定', 1)
p.write_text(s, encoding="utf-8"); print("  gate JSON を新期限版に固定:", sys.argv[2])
PY2

FIRE_LOG="/tmp/release_mcp_r45_$(date +%Y%m%d-%H%M%S).log"
echo "  ログ: $FIRE_LOG"
cd "$PAD"
RELEASE_MCP_PROFILE="${RELEASE_PROFILE}" bash "$PATCHED" "$APPROVAL" 2>&1 | tee "$FIRE_LOG" \
  | grep -E "SUCCEEDED|FAILED|FATAL|完走|レシート|承認レコード|段[0-9]" || true

grep -q "5 段完走" "$FIRE_LOG" || { echo "FATAL: 完走していません（${FIRE_LOG}）"; exit 1; }

RECEIPT_KEY=$(grep -oE "release-receipts/mcp/$COMMIT/[0-9a-f]{64}\.json" "$FIRE_LOG" | tail -1)
[ -n "$RECEIPT_KEY" ] || { echo "FATAL: レシート key の抽出に失敗"; exit 1; }
echo
echo "  receipt: $RECEIPT_KEY"

echo
echo "== STEP 6: authorize（ここを空けると承認が 1 時間で失効する。即実行する）"
# 09-24 レビュー R28-05: authorize は「ローカル dev の HEAD == その場で fetch した origin/dev」を要求する。
# 便の最中に dev が動いていたら、COMMIT が origin/dev の祖先であることを確かめてローカル dev を早送りする
# （authorize が見る SOURCE_COMMIT は履歴に残るので通る）。祖先でなければ止める。
git -C "$PAD" fetch -q origin
NOW_DEV="$(git -C "$PAD" rev-parse origin/dev)"
if [ "$NOW_DEV" != "$COMMIT" ]; then
  echo "  ⚠️ 便の最中に origin/dev が動いた: ${COMMIT:0:8} → ${NOW_DEV:0:8}"
  git -C "$PAD" merge-base --is-ancestor "$COMMIT" "$NOW_DEV" \
    || { echo "FATAL: 発射コミットが origin/dev の祖先でない（force push？）。authorize しない"; exit 1; }
  git -C "$PAD" merge --ff-only -q origin/dev
  echo "  ローカル dev を ${NOW_DEV:0:8} へ早送りしてから authorize する"
fi
RV=$(aws s3api list-object-versions --bucket "$B" --prefix "$RECEIPT_KEY" \
  --query "Versions[?IsLatest && Key=='$RECEIPT_KEY'].VersionId | [0]" --output text)
SV=$(aws s3api list-object-versions --bucket "$B" --prefix "$RECEIPT_KEY.sig" \
  --query "Versions[?IsLatest && Key=='$RECEIPT_KEY.sig'].VersionId | [0]" --output text)
[ "$RV" != "None" ] && [ "$SV" != "None" ] || { echo "FATAL: レシート版数の取得に失敗"; exit 1; }
aws s3api get-object --bucket "$B" --key "$RECEIPT_KEY" --version-id "$RV" \
  "$OUT/receipt.json" --query ContentLength --output text >/dev/null

python3 - "$OUT" "$OLD_CORE" "$S" <<'PY'
import copy
import json
import sys

out, old_core, scratch = sys.argv[1:4]
r = json.load(open(f"{out}/receipt.json", encoding="utf-8"))
ae = r["approval_evidence"]
subj = {s["name"]: s["digest"] for s in r["subjects"]}
core, media = subj["core"], subj["media"]
assert core.startswith("sha256:") and core != old_core, "core digest が現行と同一/不正"
print(f"  core:  {core}")
print(f"  media: {media}")

with open(f"{out}/args.env", "w", encoding="utf-8") as f:
    f.write(f"AP_KEY={ae['payload']['key']}\nAP_VER={ae['payload']['version_id']}\nAP_SHA={ae['payload']['sha256']}\n")
    f.write(f"SIG_KEY={ae['signature']['key']}\nSIG_VER={ae['signature']['version_id']}\nSIG_SHA={ae['signature']['sha256']}\n")
    f.write(f"CORE_DIGEST={core}\nMEDIA_DIGEST={media}\n")

base = json.load(open(f"{scratch}/oc-release/release-consumer-manifest-0904e.json", encoding="utf-8"))
m = copy.deepcopy(base)
assert m["mode"] == "receipt-required" and len(m["consumers"]) == 8
for c in m["consumers"]:
    if c["consumer_id"] == "openclaw":
        c["after"] = copy.deepcopy(c["before"])
    elif c["consumer_id"] == "mcp":
        assert c["receipt"] == {"pipeline": "mcp", "subject": "core"}, c["receipt"]
        repo_img = c["after"]["image"].split("@")[0]
        assert repo_img.endswith("/teamagent-mcp"), repo_img
        c["after"]["image"] = f"{repo_img}@{core}"
        for cd in c["after"]["task_definition"]["container_definitions"]:
            if cd.get("image", "").startswith(repo_img + "@"):
                cd["image"] = f"{repo_img}@{core}"
text = json.dumps(m, ensure_ascii=False, indent=2) + "\n"
assert text.count(core) == 2, f"新 core の出現数 {text.count(core)}（期待 2）"
open(f"{out}/consumer-manifest.json", "w", encoding="utf-8").write(text)
print("  manifest 生成 OK")
PY

source "$OUT/args.env"

export AWS_ACCESS_KEY_ID="$(aws configure get aws_access_key_id --profile "${RELEASE_PROFILE}")"
export AWS_SECRET_ACCESS_KEY="$(aws configure get aws_secret_access_key --profile "${RELEASE_PROFILE}")"
export AWS_SESSION_TOKEN="$(aws configure get aws_session_token --profile "${RELEASE_PROFILE}")"
rm -f "$OUT/gate-vars.tfvars.json"
AUTH_LOG="/tmp/mcp_authorize_r45_$(date +%Y%m%d-%H%M%S).log"
echo "  ログ: $AUTH_LOG"
bash infra/deploy/authorize_image_release.sh \
  --pipeline mcp --channel active \
  --receipt-key "$RECEIPT_KEY" --receipt-version-id "$RV" --receipt-signature-version-id "$SV" \
  --consumer-manifest "$OUT/consumer-manifest.json" \
  --terraform-gate-vars-out "$OUT/gate-vars.tfvars.json" \
  --approval-payload-bucket "$B" --approval-payload-key "$AP_KEY" \
  --approval-payload-version-id "$AP_VER" --approval-payload-sha256 "$AP_SHA" \
  --approval-signature-bucket "$B" --approval-signature-key "$SIG_KEY" \
  --approval-signature-version-id "$SIG_VER" --approval-signature-sha256 "$SIG_SHA" \
  2>&1 | tee "$AUTH_LOG" | tail -20

echo
echo "== STEP 7: 本番リポジトリへ入ったか"
aws ecr describe-images --repository-name teamagent-mcp --image-ids imageDigest="$CORE_DIGEST" \
  --query 'imageDetails[0].{tags:imageTags,pushed:imagePushedAt}' --output json \
  || echo "  まだ入っていません"

echo
echo "完了。次はタスク定義の差し替えです（mcp＋consumer 4＋ingest env＋media を 1 本で）:"
echo "  bash ${RELEASE_INPUTS_DIR:-~/Documents/Claude/Artifacts/aico-inputs-20260911}/apply_mcp_tds.sh $CORE_DIGEST $MEDIA_DIGEST"
