"""学習ジョブ 1 件を、使い捨ての HERMES_HOME と子プロセスで実行する。

- ジョブごとに 0700 の一時ディレクトリを作り、HOME・TMPDIR も同じ場所に向ける
  （本家が HOME の外へ書いても一緒に消える）
- 子プロセスに渡す環境変数は許可リストだけ。ingress bearer など本サーバの秘密は渡さない
- 子プロセスの stdout/stderr は発話を含みうるので、記録も返却もしない
  （終了コードと理由コードだけ）
- 子が終わったらプロセスグループごと必ず止め（孫を残さない）、成否にかかわらず作業場を消す
- 返す本人メモは入力と同じ規則（§ なし・200 字以内・空でない）に通し、外れた項目は返さない
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .config import build_config, lint_config, render_config
from .schema import ENTRY_DELIMITER, LearnRequest, valid_entry

RESULT_FILE: Final = "result.json"
INPUT_FILE: Final = "input.json"
WORKDIR_PREFIX: Final = "hermes-job-"
_CODE_RE: Final = re.compile(r"[a-z][a-z0-9_]{0,39}")

# 子プロセスに引き継いでよい環境変数（AWS の資格情報取得と Python の実行に要るものだけ）
ENV_ALLOWLIST: Final = frozenset(
    {
        "PATH",
        "PYTHONPATH",
        "LANG",
        "LC_ALL",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN",
        "AWS_CA_BUNDLE",
        "SSL_CERT_FILE",
        # 以下はローカル実測用（ECS ではタスクロールを使うので設定しない）
        "AWS_PROFILE",
        "AWS_CONFIG_FILE",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    }
)


class JobError(RuntimeError):
    """ジョブの失敗。code は応答・ログに載せてよい固定の識別子。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class LearnResult:
    job_id: str
    user_entries: tuple[str, ...]
    memory_entries: tuple[str, ...]
    elapsed_s: float
    dropped: int = 0


def _write_entries(path: Path, entries: Sequence[str]) -> None:
    path.write_text(ENTRY_DELIMITER.join(entries), encoding="utf-8")


def _read_entries(path: Path) -> tuple[tuple[str, ...], int]:
    """(返してよい項目, 規則に合わず返さなかった数)。"""
    if not path.exists():
        return (), 0
    raw = path.read_text(encoding="utf-8")
    parts = [e.strip() for e in raw.split(ENTRY_DELIMITER) if e.strip()]
    kept = tuple(e for e in parts if valid_entry(e))
    return kept, len(parts) - len(kept)


def child_env(base: Mapping[str, str], *, home: Path, model: str, region: str) -> dict[str, str]:
    env = {k: v for k, v in base.items() if k in ENV_ALLOWLIST}
    env.update(
        {
            "HERMES_HOME": str(home),
            "HOME": str(home),
            "TMPDIR": str(home / "tmp"),
            "PYTHONDONTWRITEBYTECODE": "1",
            # cwd（= HERMES_HOME）を import 経路に入れない
            "PYTHONSAFEPATH": "1",
            "AICO_HERMES_MODEL": model,
            "AWS_REGION": region,
        }
    )
    return env


def _remove_tree(path: Path) -> bool:
    for _ in range(3):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return True
        time.sleep(0.2)
    return not path.exists()


def sweep_stale_workdirs(root: str | None = None) -> int:
    """前回の異常終了などで残った作業場（発話を含みうる）を消す。サーバ起動時に呼ぶ。"""
    base = Path(root or tempfile.gettempdir())
    removed = 0
    for path in base.glob(f"{WORKDIR_PREFIX}*"):
        if path.is_dir() and _remove_tree(path):
            removed += 1
    return removed


class JobRunner:
    def __init__(
        self,
        *,
        child_cmd: Sequence[str],
        model: str,
        region: str,
        timeout_s: float = 110.0,
        base_env: Mapping[str, str] | None = None,
        workdir_root: str | None = None,
    ) -> None:
        if not child_cmd:
            raise ValueError("child_cmd は必須")
        self._child_cmd = list(child_cmd)
        self._model = model
        self._region = region
        self._timeout_s = timeout_s
        self._base_env = dict(os.environ if base_env is None else base_env)
        self._workdir_root = workdir_root
        # 起動時に一度だけ config を検査する（壊れた設定で 1 件も動かさない）
        problems = lint_config(build_config(model=model, region=region))
        if problems:
            raise ValueError(f"config lint failed: {problems}")

    def run(self, request: LearnRequest) -> LearnResult:
        started = time.monotonic()
        home = Path(tempfile.mkdtemp(prefix=WORKDIR_PREFIX, dir=self._workdir_root))
        try:
            os.chmod(home, 0o700)
            (home / "tmp").mkdir(mode=0o700)
            memories = home / "memories"
            memories.mkdir(mode=0o700)
            config = build_config(model=self._model, region=self._region)
            (home / "config.yaml").write_text(render_config(config), encoding="utf-8")
            if lint_config(json.loads((home / "config.yaml").read_text(encoding="utf-8"))):
                raise JobError("config_lint")
            _write_entries(memories / "USER.md", request.user_entries)
            _write_entries(memories / "MEMORY.md", request.memory_entries)
            (home / INPUT_FILE).write_text(
                json.dumps({"utterances": list(request.utterances)}, ensure_ascii=False),
                encoding="utf-8",
            )
            self._run_child(home)
            result = self._read_result(home)
            if not result.get("ok"):
                code = result.get("code")
                # 理由コードは ASCII の固定語だけ通す
                # （日本語も isidentifier() は真になるため正規表現で絞る）
                if isinstance(code, str) and _CODE_RE.fullmatch(code) is not None:
                    raise JobError(code)
                raise JobError("child_failed")
            user_entries, dropped_user = _read_entries(memories / "USER.md")
            memory_entries, dropped_memory = _read_entries(memories / "MEMORY.md")
            return LearnResult(
                job_id=request.job_id,
                user_entries=user_entries,
                memory_entries=memory_entries,
                elapsed_s=round(time.monotonic() - started, 3),
                dropped=dropped_user + dropped_memory,
            )
        finally:
            if not _remove_tree(home):  # 消せなかった作業場を残したまま成功扱いにしない
                raise JobError("workdir_not_removed")

    def _run_child(self, home: Path) -> None:
        env = child_env(self._base_env, home=home, model=self._model, region=self._region)
        proc = subprocess.Popen(
            self._child_cmd,
            cwd=home,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        timed_out = False
        try:
            returncode = proc.wait(timeout=self._timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            returncode = -1
        finally:
            # 子が終わっていても、同じプロセスグループの孫が作業場に書き続けないよう必ず止める
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            proc.wait()
        if timed_out:
            raise JobError("timeout")
        if returncode != 0:
            raise JobError(f"child_exit_{returncode}" if 0 < returncode < 256 else "child_exit")

    @staticmethod
    def _read_result(home: Path) -> dict[str, object]:
        path = home / RESULT_FILE
        if not path.exists():
            raise JobError("no_result")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise JobError("bad_result") from None
        if not isinstance(data, dict):
            raise JobError("bad_result")
        return data
