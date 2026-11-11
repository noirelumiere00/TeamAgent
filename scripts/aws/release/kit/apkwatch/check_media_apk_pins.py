"""media イメージの apk 固定が「いま上流で解決でき、lock どおりに入るか」を数秒で確かめる（発射前チェック・毎朝の見張り）。

なぜ要るか（2026-09-24）: media の Dockerfile は apk を版まで明示 pin し、実インベントリを media-apk.lock（240 行）で
固定している（ビルドの最後に cmp で照合）。Alpine 上流（v3.24 / edge）は古い版を索引から消し、新しい版を足す。
pin した版が消えると "unable to select packages"、pin していない依存に新しい版が出ると apk がそちらを選んで cmp 不一致になり、
いずれも段 3 の media ビルドで落ちて承認レコード 1 本と 10〜20 分を無駄にする（08-26 ncurses・09-02 font-noto・
09-18 ca-certificates・09-24 libexpat の 4 回）。

判定（09-24 独立レビューの指摘を反映）:
  1. 明示 pin（"pkg=$ARG"）の版が「上流索引」にも「ベース同梱一覧」にも無い → DRIFT（unable to select）
  2. lock の行のうち pin でもベース同梱でもないもの（apk が上流から選んで入れる依存）について:
     a. その版が上流索引に無い → DRIFT（消えた）
     b. 上流索引にその名前のより新しい版がある → WARN（apk は依存の条件を満たす範囲で選ぶので、新しい版があるだけでは
        食い違わない。09-24 実測: simdjson 4.6.11 / python3-pyc r1 が上流にあっても、pin した nodejs / python3 の依存条件で
        lock の版が選ばれ r29・r30 の段 3 は通った）。確定は apk 段の実ビルド（shadow_build_apk.sh）で行う
  3. ベース同梱一覧（CHROMIUM_BASE_ARM64_DIGEST ごとの実測）が無い → 2 と判定できないので UNKNOWN（exit 2）。
     一覧は docker run <base> apk list --installed で作る（apkwatch/base-installed-<digest>.lock）
  依存グラフそのものの変化（新しい依存の追加・脱落）は索引だけでは完全には検出できない。確実な確認は apk 段の実ビルド
  （shadow_build_apk.sh）で行う。本チェックの OK は「pin と lock の版が上流（またはベース同梱）で解決できる」の意味。

使い方:
  python3 check_media_apk_pins.py --repo <repo root> [--ref <git ref>]   # ref 指定時は git show で読む（作業ツリー不要）
  python3 check_media_apk_pins.py --repo <repo> --lock-file <path>       # 任意の lock（apply_apklock.sh の新 lock の自己検査用）
終了コード: 0=OK / 1=DRIFT（撃つと段 3 で落ちる）/ 2=確認できない（索引取得失敗・ベース一覧なし・想定外の例外）
"""

from __future__ import annotations

import argparse
import collections
import io
import pathlib
import re
import subprocess
import sys
import tarfile
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
DOCKERFILE = "infra/docker/Dockerfile.teamagent-media-worker"
LOCK = "infra/docker/media-apk.lock"

# apk の版の大小（apk-tools の規則の要約）: 数字の列 → 英字 1 文字 → 接尾辞（_alpha<_beta<_pre<_rc<なし<_cvs<_svn<_git<_hg<_p）→ -rN
_SUFFIX_ORDER = {
    "alpha": -4,
    "beta": -3,
    "pre": -2,
    "rc": -1,
    "cvs": 1,
    "svn": 2,
    "git": 3,
    "hg": 4,
    "p": 5,
}
_VERSION_RE = re.compile(
    r"^([0-9]+(?:\.[0-9]+)*)([a-z]?)((?:_[a-z]+[0-9]*)*)(~[0-9a-f]+)?(?:-r([0-9]+))?$"
)


def version_key(ver: str) -> tuple:
    m = _VERSION_RE.match(ver)
    if not m:
        raise ValueError(f"apk の版を解釈できない: {ver}")
    nums = [int(x) for x in m.group(1).split(".")]
    suffixes = [
        (_SUFFIX_ORDER.get(name, 0), int(num or 0))
        for name, num in re.findall(r"_([a-z]+)([0-9]*)", m.group(3))
    ]
    return (nums, m.group(2), [*suffixes, (0, 0)], int(m.group(5) or 0))


def read(repo: pathlib.Path, ref: str | None, rel: str) -> str:
    if ref:
        return subprocess.run(
            ["git", "-C", str(repo), "show", f"{ref}:{rel}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    return (repo / rel).read_text(encoding="utf-8")


def fetch_index(base: str) -> dict[str, set[str]]:
    idx: dict[str, set[str]] = collections.defaultdict(set)
    for sub in ("main", "community"):
        data = urllib.request.urlopen(f"{base}/{sub}/aarch64/APKINDEX.tar.gz", timeout=60).read()
        member = tarfile.open(fileobj=io.BytesIO(data)).extractfile("APKINDEX")
        if member is None:
            raise RuntimeError(f"{base}/{sub} の APKINDEX を展開できない")
        for blk in member.read().decode().split("\n\n"):
            p = re.search(r"^P:(.+)$", blk, re.M)
            v = re.search(r"^V:(.+)$", blk, re.M)
            if p and v:
                idx[p.group(1)].add(v.group(1))
    return idx


def split_entry(line: str) -> tuple[str, str]:
    m = re.match(r"^(.+)-([^-]+-r\d+)$", line)
    if not m:
        raise ValueError(f"lock 行を名前と版に分けられない: {line}")
    return m.group(1), m.group(2)


def check(repo: pathlib.Path, ref: str | None, lock_file: str | None) -> int:
    df = read(repo, ref, DOCKERFILE)
    lock = (
        pathlib.Path(lock_file).read_text(encoding="utf-8") if lock_file else read(repo, ref, LOCK)
    ).split()
    args = dict(re.findall(r"^ARG ([A-Z0-9_]+)=(\S+)$", df, re.M))
    pins = re.findall(r'"([a-z0-9][a-z0-9+._-]*)=\$([A-Z0-9_]+)"', df)
    base_digest = args.get("CHROMIUM_BASE_ARM64_DIGEST", "")
    base_file = HERE / f"base-installed-{base_digest.removeprefix('sha256:')}.lock"
    if not base_file.exists():
        print(
            f"UNKNOWN: ベース同梱一覧 {base_file.name} が無い（ベース digest が変わった？）。lock 全行の照合ができない"
        )
        print(
            f"  作り方: docker run --rm --platform linux/arm64 --entrypoint sh <ECR>/teamagent-mirror/chromium-headless@{base_digest}"
            " -c \"apk list --installed | awk '{print \\$1}' | LC_ALL=C sort\" > " + str(base_file)
        )
        return 2
    base = set(base_file.read_text(encoding="utf-8").split())
    try:
        idx: dict[str, set[str]] = collections.defaultdict(set)
        for repo_url in (args["ALPINE_EDGE_REPOSITORY"], args["ALPINE_STABLE_REPOSITORY"]):
            for name, vers in fetch_index(repo_url).items():
                idx[name] |= vers
    except Exception as e:  # 索引が取れないときは判定不能
        print(f"UNKNOWN: 上流の APKINDEX を取得できない（{type(e).__name__}: {e}）")
        return 2

    drift: list[str] = []
    warns: list[str] = []
    pinned_names = set()
    for name, arg in pins:
        pinned_names.add(name)
        ver = args.get(arg)
        if ver is None:
            drift.append(f"pin {name}: ARG {arg} が Dockerfile に無い")
        elif ver not in idx.get(name, set()) and f"{name}-{ver}" not in base:
            drift.append(
                f"pin {name}={ver} が上流にもベース同梱にも無い（上流: {', '.join(sorted(idx.get(name, set()), key=version_key)) or 'なし'}）"
            )

    for line in lock:
        name, ver = split_entry(line)
        if line in base or name in pinned_names:
            continue
        upstream = idx.get(name, set())
        if ver not in upstream:
            drift.append(
                f"lock {line} が上流に無い（上流: {', '.join(sorted(upstream, key=version_key)) or 'なし'}）"
            )
            continue
        newest = max(upstream, key=version_key)
        if version_key(newest) > version_key(ver):
            warns.append(
                f"lock {line} より新しい {name}-{newest} が上流にある（依存条件しだい。実ビルドで確認）"
            )

    print(f"pins {len(pins)} / lock {len(lock)} 行 / base {base_digest[:19]}…（同梱 {len(base)}）")
    for w in warns:
        print(f"  WARN {w}")
    if drift:
        print(
            "DRIFT: 撃つと段 3 の media ビルドで落ちる。apkwatch/apply_apklock.sh で追随させてから撃つこと"
        )
        for d in drift:
            print(f"  - {d}")
        return 1
    print(
        "OK: pin と lock の版はすべて上流（またはベース同梱）で解決できる"
        + (f"（新しい版の出現 {len(warns)} 件は WARN・実ビルドで確定）" if warns else "")
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--ref", default=None)
    ap.add_argument("--lock-file", default=None)
    a = ap.parse_args()
    try:
        return check(pathlib.Path(a.repo), a.ref, a.lock_file)
    except Exception as e:  # 想定外の失敗は DRIFT と区別して「確認できない」にする
        print(f"UNKNOWN: 想定外の失敗（{type(e).__name__}: {e}）")
        return 2


if __name__ == "__main__":
    sys.exit(main())
