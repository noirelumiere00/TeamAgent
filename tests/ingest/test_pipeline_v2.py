"""入れ込み v2（2026-07-10）の ingest 基盤テスト（C3 担当分）。

実 AWS / 実 Drive API / 実 Postgres は一切呼ばない。検証する契約:

1. S3 yaml override（scripts/run_ingest_fargate._resolve_sources_yaml）:
   - env 未設定 → None（同梱 yaml の既定・baked ログ経路）
   - S3 取得成功 → ローカル化したパスを返し、中身が S3 の body と一致
   - S3 取得失敗 / sha256 不一致 / URI 形式不正 → **即 exit 1**（silent fallback 禁止）
2. フォルダ名除外（gdrive_client.walk_files_recursive + DEFAULT_EXCLUDE_FOLDER_NAME_RE）:
   - 既定 regex が 99_ / 一次倉庫 / 検索対象外 にマッチし通常フォルダに非マッチ
   - walk でマッチするサブフォルダは配下ごと skip・None なら除外なし・カスタム regex 上書き
   - loader がグローバルキー gdrive_exclude_folder_name_re をパース（未記載 None / 空文字 ""）
3. stale soft-delete（pipeline.IngestRunner._maybe_mark_stale_documents）:
   - 差集合（未観測 かつ 未 stale のみが候補）・観測済み doc の stale 解除
   - ブレーキ: 候補 >50% で exit 1・INGEST_STALE_ALLOW_MASS=true で明示続行・
     30%超50%以下は WARNING で続行
   - ゲート: INGEST_MARK_STALE 未設定 / dry_run / kinds に gdrive 無し → 何もしない
   - 観測完全性ガード（stale 堅牢化）: gdrive/shared_drives の列挙部分失敗
     （errors>0 / sources_skipped>0）または walk の max_files 打ち切りがあった run は
     mark 不発（WARNING skip）・観測分の clear だけ実行。正常 run は従来どおり
4. プレースホルダ skip（loader）: folder_id が REPLACE_ 始まりのエントリを WARNING skip
5. ルート検査（pipeline._check_rulebook_root）:
   - NN_ フォルダの yaml 不足 → exit 1 / 99_ 系の yaml 誤登録 → exit 1 /
   - 全カバー → 通過 / INGEST_ROOT_CHECK_WARN_ONLY=true で警告降格 / 列挙失敗も fail-loud
6. export の stale 除外（scripts/export_vault.documents_sql）:
   - 既定で metadata.stale='true' を除外し、include_stale=True で従来 SQL に戻る
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import re
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from teamagent.adapters.gdrive_client import (
    DEFAULT_EXCLUDE_FOLDER_NAME_RE,
    DriveFile,
    GDriveClient,
)
from teamagent.ingest.loader import (
    GDriveFolderSpec,
    IngestSources,
    load_ingest_sources,
)
from teamagent.ingest.pipeline import (
    IngestResult,
    IngestRunner,
    IngestStats,
    _check_rulebook_root,
)

_ROOT = Path(__file__).resolve().parents[2]


def _load_script(module_key: str, filename: str) -> Any:
    """scripts/ 配下（パッケージ外）のモジュールを importlib でロードする。"""
    spec = importlib.util.spec_from_file_location(module_key, _ROOT / "scripts" / filename)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_key] = mod
    spec.loader.exec_module(mod)
    return mod


_fargate = _load_script("run_ingest_fargate_v2test", "run_ingest_fargate.py")
_export_vault = _load_script("export_vault_v2test", "export_vault.py")


# -----------------------------------------------------------
# 1. S3 yaml override（run_ingest_fargate._resolve_sources_yaml）
# -----------------------------------------------------------
def _install_fake_boto3(
    monkeypatch: pytest.MonkeyPatch,
    *,
    body: bytes | None = None,
    error: Exception | None = None,
) -> None:
    """`import boto3` を sys.modules 経由で fake に差し替える（実 AWS を呼ばない）。"""

    class _FakeS3:
        def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803 (API 名)
            if error is not None:
                raise error
            return {"Body": io.BytesIO(body or b"")}

    mod = types.ModuleType("boto3")
    mod.client = lambda name: _FakeS3()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", mod)


@pytest.fixture()
def _clean_s3_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """S3 override 系 env を毎テストでまっさらにする。"""
    monkeypatch.delenv("INGEST_SOURCES_S3_URI", raising=False)
    monkeypatch.delenv("INGEST_SOURCES_SHA256", raising=False)
    return monkeypatch


def test_resolve_yaml_env_unset_returns_none(_clean_s3_env: pytest.MonkeyPatch) -> None:
    """env 未設定 → None（呼び出し側が同梱 yaml の既定に任せる・従来挙動）。"""
    assert _fargate._resolve_sources_yaml() is None


def test_resolve_yaml_s3_success_writes_local_copy(
    _clean_s3_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """S3 取得成功 → ローカル化したパスを返し、中身が S3 body と bit 一致する。"""
    body = b"version: 1\ngdrive_folders: []\n"
    local = tmp_path / "ingest_sources_s3.yaml"
    _clean_s3_env.setenv("INGEST_SOURCES_S3_URI", "s3://bkt/config/ingest_sources.yaml")
    _clean_s3_env.setattr(_fargate, "_S3_YAML_LOCAL", str(local))
    _install_fake_boto3(_clean_s3_env, body=body)

    result = _fargate._resolve_sources_yaml()

    assert result == str(local)
    assert local.read_bytes() == body


def test_resolve_yaml_s3_success_with_matching_sha256(
    _clean_s3_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """INGEST_SOURCES_SHA256 が一致すれば通過する（検証付き経路）。"""
    body = b"version: 1\n"
    _clean_s3_env.setenv("INGEST_SOURCES_S3_URI", "s3://bkt/config/ingest_sources.yaml")
    _clean_s3_env.setenv("INGEST_SOURCES_SHA256", hashlib.sha256(body).hexdigest())
    _clean_s3_env.setattr(_fargate, "_S3_YAML_LOCAL", str(tmp_path / "y.yaml"))
    _install_fake_boto3(_clean_s3_env, body=body)

    assert _fargate._resolve_sources_yaml() == str(tmp_path / "y.yaml")


def test_resolve_yaml_s3_fetch_failure_exits_1(_clean_s3_env: pytest.MonkeyPatch) -> None:
    """S3 取得失敗 → 同梱 yaml へ silent fallback せず即 exit 1。"""
    _clean_s3_env.setenv("INGEST_SOURCES_S3_URI", "s3://bkt/config/ingest_sources.yaml")
    _install_fake_boto3(_clean_s3_env, error=RuntimeError("AccessDenied"))

    with pytest.raises(SystemExit) as excinfo:
        _fargate._resolve_sources_yaml()
    assert excinfo.value.code == 1


def test_resolve_yaml_sha256_mismatch_exits_1(
    _clean_s3_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """sha256 不一致 → 即 exit 1（改竄/取り違え検知・fallback 禁止）。"""
    _clean_s3_env.setenv("INGEST_SOURCES_S3_URI", "s3://bkt/config/ingest_sources.yaml")
    _clean_s3_env.setenv("INGEST_SOURCES_SHA256", "0" * 64)
    _clean_s3_env.setattr(_fargate, "_S3_YAML_LOCAL", str(tmp_path / "y.yaml"))
    _install_fake_boto3(_clean_s3_env, body=b"version: 1\n")

    with pytest.raises(SystemExit) as excinfo:
        _fargate._resolve_sources_yaml()
    assert excinfo.value.code == 1
    assert not (tmp_path / "y.yaml").exists()  # 不一致 body をローカルに残さない


def test_resolve_yaml_malformed_uri_exits_1(_clean_s3_env: pytest.MonkeyPatch) -> None:
    """s3://bucket/key 形式でない URI → 即 exit 1。"""
    _clean_s3_env.setenv("INGEST_SOURCES_S3_URI", "https://example.com/x.yaml")

    with pytest.raises(SystemExit) as excinfo:
        _fargate._resolve_sources_yaml()
    assert excinfo.value.code == 1


# -----------------------------------------------------------
# 2. フォルダ名除外（regex / walk / loader パース）
# -----------------------------------------------------------
@pytest.mark.parametrize(
    "name",
    ["99_一次倉庫", "99＿旧データ", "  99_raw", "資料の一次倉庫", "検索対象外メモ"],
)
def test_default_exclude_regex_matches(name: str) -> None:
    """既定 regex は 99_ 接頭（全角＿含む）・一次倉庫・検索対象外にマッチする。"""
    assert re.search(DEFAULT_EXCLUDE_FOLDER_NAME_RE, name) is not None


@pytest.mark.parametrize("name", ["01_提案事例", "06_価格・契約", "1999年度", "営業資料"])
def test_default_exclude_regex_does_not_match(name: str) -> None:
    """通常フォルダ（NN_ ルールブックや 99 を含むだけの名前）は誤爆しない。"""
    assert re.search(DEFAULT_EXCLUDE_FOLDER_NAME_RE, name) is None


class _FakeWalkRequest:
    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response

    def execute(self) -> dict[str, Any]:
        return self._response


class _FakeWalkService:
    """walk_files_recursive 用: q の親フォルダ ID ごとに直下の files を返す fake。"""

    def __init__(self, tree: dict[str, list[dict[str, Any]]]) -> None:
        self._tree = tree

    def files(self) -> _FakeWalkService:
        return self

    def list(self, **kwargs: Any) -> _FakeWalkRequest:
        folder_id = str(kwargs["q"]).split("'")[1]  # "'FID' in parents and trashed = false"
        return _FakeWalkRequest({"files": self._tree.get(folder_id, []), "nextPageToken": None})


_FOLDER_MIME = "application/vnd.google-apps.folder"


def _walk_tree() -> dict[str, list[dict[str, Any]]]:
    return {
        "ROOT": [
            {"id": "F99", "name": "99_一次倉庫", "mimeType": _FOLDER_MIME},
            {"id": "F01", "name": "01_提案事例", "mimeType": _FOLDER_MIME},
            {"id": "A", "name": "a.pdf", "mimeType": "application/pdf"},
        ],
        "F99": [{"id": "B", "name": "b.pdf", "mimeType": "application/pdf"}],
        "F01": [{"id": "C", "name": "c.pdf", "mimeType": "application/pdf"}],
    }


def test_walk_skips_excluded_subfolder_recursively() -> None:
    """既定 regex 指定時、99_ 系サブフォルダは配下（b.pdf）ごと取り込まれない。"""
    client = GDriveClient(service=_FakeWalkService(_walk_tree()))
    files = client.walk_files_recursive(
        root_id="ROOT",
        request_id="req-t",
        exclude_folder_name_re=DEFAULT_EXCLUDE_FOLDER_NAME_RE,
    )
    assert {f.id for f in files} == {"A", "C"}


def test_walk_without_exclude_regex_keeps_all() -> None:
    """exclude regex 未指定（None）は後方互換＝全サブフォルダを取り込む。"""
    client = GDriveClient(service=_FakeWalkService(_walk_tree()))
    files = client.walk_files_recursive(root_id="ROOT", request_id="req-t")
    assert {f.id for f in files} == {"A", "B", "C"}


def test_walk_with_custom_regex_overrides_default() -> None:
    """カスタム regex（yaml 上書き相当）は既定と別の対象を除外できる。"""
    client = GDriveClient(service=_FakeWalkService(_walk_tree()))
    files = client.walk_files_recursive(
        root_id="ROOT", request_id="req-t", exclude_folder_name_re=r"^01_"
    )
    assert {f.id for f in files} == {"A", "B"}


def test_walk_truncation_at_max_files_warns_with_counts() -> None:
    """max_files 打ち切り時は WARNING（hit_max=True・root/件数/上限付き）で可視化される。"""
    from structlog.testing import capture_logs

    client = GDriveClient(service=_FakeWalkService(_walk_tree()))
    with capture_logs() as logs:
        files = client.walk_files_recursive(root_id="ROOT", request_id="req-t", max_files=1)
    assert len(files) == 1  # 上限で列挙が打ち切られる
    done = [e for e in logs if e["event"] == "gdrive_walk_files_recursive"]
    assert len(done) == 1
    assert done[0]["log_level"] == "warning"
    assert done[0]["hit_max"] is True
    assert done[0]["root_id"] == "ROOT"
    assert done[0]["files_collected"] == 1
    assert done[0]["max_files"] == 1


def test_walk_without_truncation_logs_info() -> None:
    """打ち切りが無い walk は従来どおり INFO（hit_max=False）のまま。"""
    from structlog.testing import capture_logs

    client = GDriveClient(service=_FakeWalkService(_walk_tree()))
    with capture_logs() as logs:
        client.walk_files_recursive(root_id="ROOT", request_id="req-t")
    done = [e for e in logs if e["event"] == "gdrive_walk_files_recursive"]
    assert len(done) == 1
    assert done[0]["log_level"] == "info"
    assert done[0]["hit_max"] is False


def _write_yaml(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "ingest_sources.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def test_loader_parses_exclude_regex_global_key(tmp_path: Path) -> None:
    """yaml グローバルキー gdrive_exclude_folder_name_re をそのまま保持する。"""
    yaml_path = _write_yaml(
        tmp_path,
        'version: 1\ngdrive_exclude_folder_name_re: "^stash_"\n',
    )
    sources = load_ingest_sources(yaml_path)
    assert sources.gdrive_exclude_folder_name_re == "^stash_"


def test_loader_exclude_regex_absent_is_none_and_empty_is_kept(tmp_path: Path) -> None:
    """キー未記載 → None（コード既定を使う）。空文字 "" → 「除外なし」の明示。"""
    absent = load_ingest_sources(_write_yaml(tmp_path, "version: 1\n"))
    assert absent.gdrive_exclude_folder_name_re is None
    empty = load_ingest_sources(
        _write_yaml(tmp_path, 'version: 1\ngdrive_exclude_folder_name_re: ""\n')
    )
    assert empty.gdrive_exclude_folder_name_re == ""


# -----------------------------------------------------------
# 3. stale soft-delete（差集合・ブレーキ・ゲート）
# -----------------------------------------------------------
class _FakeStaleRepo:
    """stale 3 メソッドだけを持つ fake repository（呼び出しを記録する）。"""

    def __init__(self, existing: list[tuple[str, bool]]) -> None:
        self._existing = existing
        self.listed = False
        self.marked: list[str] | None = None
        self.marked_at: str | None = None
        self.cleared: list[str] | None = None

    def list_gdrive_external_ids_with_stale(self) -> list[tuple[str, bool]]:
        self.listed = True
        return list(self._existing)

    def mark_documents_stale(self, external_ids: list[str], *, marked_at_iso: str) -> int:
        self.marked = list(external_ids)
        self.marked_at = marked_at_iso
        return len(external_ids)

    def clear_documents_stale(self, external_ids: list[str]) -> int:
        self.cleared = list(external_ids)
        return len(external_ids)


def _runner(repo: _FakeStaleRepo, *, dry_run: bool = False) -> IngestRunner:
    return IngestRunner(
        repo,  # type: ignore[arg-type]  # stale 3 メソッドのみ使用
        embedder=object(),  # type: ignore[arg-type]  # 本テストでは未使用
        owner_email="test@vectorinc.co.jp",
        dry_run=dry_run,
        alerter=object(),  # type: ignore[arg-type]  # 本テストでは未使用
    )


@pytest.fixture()
def _stale_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.delenv("INGEST_MARK_STALE", raising=False)
    monkeypatch.delenv("INGEST_STALE_ALLOW_MASS", raising=False)
    return monkeypatch


def test_mark_stale_gate_off_is_noop(_stale_env: pytest.MonkeyPatch) -> None:
    """INGEST_MARK_STALE 未設定（既定）→ DB を一切見ない＝現行と完全一致。"""
    repo = _FakeStaleRepo([("a", False)])
    _runner(repo)._maybe_mark_stale_documents(set(), kinds=["gdrive"], request_id="req-t")
    assert repo.listed is False


def test_mark_stale_skipped_on_dry_run(_stale_env: pytest.MonkeyPatch) -> None:
    """dry_run では書き込み系を呼ばない。"""
    _stale_env.setenv("INGEST_MARK_STALE", "true")
    repo = _FakeStaleRepo([("a", False)])
    _runner(repo, dry_run=True)._maybe_mark_stale_documents(
        set(), kinds=["gdrive"], request_id="req-t"
    )
    assert repo.listed is False


def test_mark_stale_skipped_without_gdrive_kind(_stale_env: pytest.MonkeyPatch) -> None:
    """kinds に gdrive が無い run では観測集合が空同然＝全 doc stale 誤爆を防いで skip。"""
    _stale_env.setenv("INGEST_MARK_STALE", "true")
    repo = _FakeStaleRepo([("a", False)])
    _runner(repo)._maybe_mark_stale_documents(set(), kinds=["slack"], request_id="req-t")
    assert repo.listed is False


def test_mark_stale_diff_marks_unobserved_and_clears_observed(
    _stale_env: pytest.MonkeyPatch,
) -> None:
    """未観測かつ未 stale の doc だけ候補にし、観測済み doc は stale 解除される。"""
    _stale_env.setenv("INGEST_MARK_STALE", "true")
    # e: 未観測・未 stale → 今回 mark。f: 未観測・既 stale → 再 mark しない（初回日時保持）。
    repo = _FakeStaleRepo(
        [("a", False), ("b", False), ("c", False), ("d", False), ("e", False), ("f", True)]
    )
    observed = {"a", "b", "c", "d"}
    _runner(repo)._maybe_mark_stale_documents(observed, kinds=["gdrive"], request_id="req-t")
    assert repo.marked == ["e"]
    assert repo.marked_at is not None  # run 日時 ISO が渡る
    assert repo.cleared == sorted(observed)


def test_mark_stale_brake_over_50_percent_exits_1(_stale_env: pytest.MonkeyPatch) -> None:
    """候補が総数の 50% 超 → 中止して exit 1（誤設定/走査失敗の疑い）。"""
    _stale_env.setenv("INGEST_MARK_STALE", "true")
    repo = _FakeStaleRepo([("a", False), ("b", False), ("c", False), ("d", False)])
    with pytest.raises(SystemExit) as excinfo:
        _runner(repo)._maybe_mark_stale_documents({"a"}, kinds=["gdrive"], request_id="req-t")
    assert excinfo.value.code == 1
    assert repo.marked is None  # 1 件も書き込まずに止まる
    assert repo.cleared is None


def test_mark_stale_allow_mass_continues_over_50_percent(
    _stale_env: pytest.MonkeyPatch,
) -> None:
    """INGEST_STALE_ALLOW_MASS=true で 50% 超でも明示続行できる（意図的な大量掃除）。"""
    _stale_env.setenv("INGEST_MARK_STALE", "true")
    _stale_env.setenv("INGEST_STALE_ALLOW_MASS", "true")
    repo = _FakeStaleRepo([("a", False), ("b", False), ("c", False), ("d", False)])
    _runner(repo)._maybe_mark_stale_documents({"a"}, kinds=["gdrive"], request_id="req-t")
    assert repo.marked == ["b", "c", "d"]
    assert repo.cleared == ["a"]


def test_mark_stale_between_30_and_50_percent_warns_but_runs(
    _stale_env: pytest.MonkeyPatch,
) -> None:
    """30% 超 50% 以下は WARNING を出しつつ実行する（中止しない）。"""
    _stale_env.setenv("INGEST_MARK_STALE", "true")
    # 候補 2/5 = 40%
    repo = _FakeStaleRepo([("a", False), ("b", False), ("c", False), ("d", False), ("e", False)])
    _runner(repo)._maybe_mark_stale_documents({"a", "b", "c"}, kinds=["gdrive"], request_id="req-t")
    assert repo.marked == ["d", "e"]


def test_mark_stale_no_gdrive_documents_is_noop(_stale_env: pytest.MonkeyPatch) -> None:
    """既存 gdrive documents が 0 件なら何もしない（0 除算もしない）。"""
    _stale_env.setenv("INGEST_MARK_STALE", "true")
    repo = _FakeStaleRepo([])
    _runner(repo)._maybe_mark_stale_documents(set(), kinds=["gdrive"], request_id="req-t")
    assert repo.marked is None
    assert repo.cleared is None


# --- 観測完全性ガード（stale 堅牢化 2026-07-10）---------------------------------
def _result_with_stats(
    kind: str, *, errors: list[str] | None = None, sources_skipped: int = 0
) -> IngestResult:
    """指定 kind の IngestStats だけ持つ IngestResult を作る（ガードテスト用）。"""
    result = IngestResult()
    stats = IngestStats(source_kind=kind)
    stats.errors.extend(errors or [])
    stats.sources_skipped = sources_skipped
    result.by_kind[kind] = stats
    return result


def test_mark_stale_skipped_on_gdrive_enumeration_errors_but_clears(
    _stale_env: pytest.MonkeyPatch,
) -> None:
    """gdrive 列挙に部分失敗（errors>0）→ mark 不発。観測分の clear だけは実行される。"""
    _stale_env.setenv("INGEST_MARK_STALE", "true")
    repo = _FakeStaleRepo([("a", False), ("b", False)])
    result = _result_with_stats("gdrive", errors=["RuntimeError: walk failed"], sources_skipped=1)
    _runner(repo)._maybe_mark_stale_documents(
        {"a"}, kinds=["gdrive"], request_id="req-t", result=result
    )
    assert repo.marked is None  # b は未観測だが誤 stale しない
    assert repo.cleared == ["a"]  # 観測できた doc の復活対応は安全なので続行


def test_mark_stale_skipped_on_shared_drives_sources_skipped(
    _stale_env: pytest.MonkeyPatch,
) -> None:
    """shared_drives の sources_skipped>0 でも skip（observed は folder/crawl 共有のため）。"""
    _stale_env.setenv("INGEST_MARK_STALE", "true")
    repo = _FakeStaleRepo([("a", False), ("b", False)])
    result = _result_with_stats("shared_drives", sources_skipped=1)
    result.by_kind["gdrive"] = IngestStats(source_kind="gdrive")  # gdrive 自体は成功
    _runner(repo)._maybe_mark_stale_documents(
        {"a"}, kinds=["gdrive", "shared_drives"], request_id="req-t", result=result
    )
    assert repo.marked is None
    assert repo.cleared == ["a"]


def test_mark_stale_skipped_on_truncated_walk_run(
    _stale_env: pytest.MonkeyPatch,
) -> None:
    """walk が max_files 打ち切りされた run → mark 不発（reason は打ち切り区別）・clear は実行。"""
    from structlog.testing import capture_logs

    _stale_env.setenv("INGEST_MARK_STALE", "true")
    repo = _FakeStaleRepo([("a", False), ("b", False)])
    with capture_logs() as logs:
        _runner(repo)._maybe_mark_stale_documents(
            {"a"},
            kinds=["gdrive"],
            request_id="req-t",
            result=_result_with_stats("gdrive"),  # 列挙エラーは無し＝打ち切りのみで skip
            truncated_walk_roots={"DRIVE1"},
        )
    assert repo.marked is None
    assert repo.cleared == ["a"]
    skipped = [e for e in logs if e["event"] == "ingest_mark_stale_skipped"]
    assert len(skipped) == 1
    assert "walk_truncated" in skipped[0]["reason"]  # source_failure と区別できる
    assert "source_failure" not in skipped[0]["reason"]
    assert skipped[0]["truncated_walk_roots"] == ["DRIVE1"]


def test_mark_stale_runs_normally_on_clean_result(
    _stale_env: pytest.MonkeyPatch,
) -> None:
    """errors=0・sources_skipped=0・打ち切り無しの正常 run は従来どおり mark + clear。"""
    _stale_env.setenv("INGEST_MARK_STALE", "true")
    repo = _FakeStaleRepo([("a", False), ("b", False), ("c", False), ("d", False), ("e", False)])
    observed = {"a", "b", "c", "d"}
    _runner(repo)._maybe_mark_stale_documents(
        observed,
        kinds=["gdrive"],
        request_id="req-t",
        result=_result_with_stats("gdrive"),
        truncated_walk_roots=set(),
    )
    assert repo.marked == ["e"]
    assert repo.cleared == sorted(observed)


class _FakeAlerter:
    """run() 配線テスト用の no-op alerter（_run_kind の失敗通知を受けるだけ）。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def send_ingest_failure(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


def _sources_one_gdrive_folder() -> IngestSources:
    return IngestSources(
        version=1,
        slack_channels=(),
        gdrive_folders=(
            GDriveFolderSpec(folder_id="F1", folder_name="01_提案事例", description="t"),
        ),
        gsheets=(),
    )


def _isolate_run_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    """run() 経由テスト用: stale 以外の env ゲートを確実に OFF にする（開発機の env 汚染対策）。"""
    for name in ("BOILERPLATE_DETECT", "DOC_DEDUP_DETECT", "USE_INCREMENTAL_SYNC"):
        monkeypatch.delenv(name, raising=False)


def _wired_runner(repo: _FakeStaleRepo) -> IngestRunner:
    return IngestRunner(
        repo,  # type: ignore[arg-type]  # stale 3 メソッドのみ使用
        embedder=object(),  # type: ignore[arg-type]  # handler は monkeypatch 済で未使用
        owner_email="test@vectorinc.co.jp",
        dry_run=False,
        alerter=_FakeAlerter(),  # type: ignore[arg-type]
    )


def test_run_wires_source_failure_into_stale_guard(
    _stale_env: pytest.MonkeyPatch,
) -> None:
    """run() 配線: gdrive source の列挙失敗（fail-open skip）が stale ガードへ届き mark 不発。"""
    _stale_env.setenv("INGEST_MARK_STALE", "true")
    _isolate_run_gates(_stale_env)

    def _boom(spec: Any, **kwargs: Any) -> tuple[int, int]:
        raise RuntimeError("walk failed")

    _stale_env.setattr("teamagent.ingest.pipeline._ingest_gdrive_folder", _boom)
    repo = _FakeStaleRepo([("a", False), ("b", False)])
    result = _wired_runner(repo).run(_sources_one_gdrive_folder(), kinds=["gdrive"])
    assert result.by_kind["gdrive"].errors  # 失敗は従来どおり集計される（exit 1 判定用）
    assert repo.marked is None  # 部分失敗 run では mark 不発
    assert repo.cleared == []  # clear は実行される（観測ゼロなので空）


def test_run_wires_walk_truncation_into_stale_guard(
    _stale_env: pytest.MonkeyPatch,
) -> None:
    """run() 配線: handler が立てた打ち切りフラグが stale ガードへ届き mark 不発・clear 実行。"""
    _stale_env.setenv("INGEST_MARK_STALE", "true")
    _isolate_run_gates(_stale_env)

    def _truncated_handler(
        spec: Any,
        *,
        observed_gdrive_ids: set[str],
        truncated_walk_roots: set[str],
        **kwargs: Any,
    ) -> tuple[int, int]:
        observed_gdrive_ids.add("a")
        truncated_walk_roots.add(spec.folder_id)  # walk 打ち切り検知の相当分
        return 1, 1

    _stale_env.setattr("teamagent.ingest.pipeline._ingest_gdrive_folder", _truncated_handler)
    repo = _FakeStaleRepo([("a", False), ("b", False)])
    _wired_runner(repo).run(_sources_one_gdrive_folder(), kinds=["gdrive"])
    assert repo.marked is None  # 打ち切り run では b を誤 stale しない
    assert repo.cleared == ["a"]  # 観測できた a の stale 解除は続行


# -----------------------------------------------------------
# 4. プレースホルダ skip（loader）
# -----------------------------------------------------------
def test_loader_skips_replace_prefixed_folder_id(tmp_path: Path) -> None:
    """folder_id が REPLACE_ 始まりのエントリは WARNING 付き skip（取り込まれない）。"""
    yaml_path = _write_yaml(
        tmp_path,
        (
            "version: 1\n"
            "gdrive_folders:\n"
            '  - folder_id: "REPLACE_WITH_KNOWLEDGE_01"\n'
            '    folder_name: "01_提案事例"\n'
            '  - folder_id: "REPLACE_LATER_06"\n'  # 旧 marker 一覧に無い REPLACE_ 接頭
            '    folder_name: "06_価格・契約"\n'
            '  - folder_id: "1RealFolderId"\n'
            '    folder_name: "02_議事録"\n'
        ),
    )
    sources = load_ingest_sources(yaml_path)
    assert [f.folder_id for f in sources.gdrive_folders] == ["1RealFolderId"]


def test_loader_rulebook_root_placeholder_falls_back_to_none(tmp_path: Path) -> None:
    """gdrive_rulebook_root_folder_id が placeholder ならルート検査を誤発火させない。"""
    yaml_path = _write_yaml(
        tmp_path,
        'version: 1\ngdrive_rulebook_root_folder_id: "REPLACE_WITH_KNOWLEDGE_ROOT"\n',
    )
    assert load_ingest_sources(yaml_path).gdrive_rulebook_root_folder_id is None


def test_loader_rulebook_root_real_id_is_kept(tmp_path: Path) -> None:
    """実 ID はそのまま保持される（ルート検査が有効になる）。"""
    yaml_path = _write_yaml(tmp_path, 'version: 1\ngdrive_rulebook_root_folder_id: "1RootId"\n')
    assert load_ingest_sources(yaml_path).gdrive_rulebook_root_folder_id == "1RootId"


# -----------------------------------------------------------
# 5. ルート検査 preflight（_check_rulebook_root）
# -----------------------------------------------------------
class _FakeRootClient:
    """ルート直下フォルダの列挙だけを返す fake GDriveClient。"""

    def __init__(
        self, subfolders: list[tuple[str, str]], *, error: Exception | None = None
    ) -> None:
        self._subfolders = subfolders
        self._error = error

    def list_files(
        self,
        folder_id: str | None,
        request_id: str,
        *,
        page_size: int = 100,
        page_token: str | None = None,
        mime_type_filter: str | None = None,
        **_: Any,
    ) -> tuple[list[DriveFile], None]:
        if self._error is not None:
            raise self._error
        files = [
            DriveFile(id=fid, name=name, mime_type=_FOLDER_MIME, modified_time=None, size=None)
            for fid, name in self._subfolders
        ]
        return files, None


def _sources_with_folders(
    folder_ids: list[str], *, root_id: str = "ROOT", exclude_re: str | None = None
) -> IngestSources:
    return IngestSources(
        version=1,
        slack_channels=(),
        gdrive_folders=tuple(
            GDriveFolderSpec(folder_id=fid, folder_name=f"name-{fid}", description="")
            for fid in folder_ids
        ),
        gsheets=(),
        gdrive_exclude_folder_name_re=exclude_re,
        gdrive_rulebook_root_folder_id=root_id,
    )


@pytest.fixture()
def _root_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.delenv("INGEST_ROOT_CHECK_WARN_ONLY", raising=False)
    return monkeypatch


def test_root_check_passes_when_all_nn_folders_covered(_root_env: pytest.MonkeyPatch) -> None:
    """NN_ フォルダが全部 yaml に載っていて 99_ 系が未登録 → 通過（例外なし）。"""
    client = _FakeRootClient(
        [("F01", "01_提案事例"), ("F02", "02_議事録"), ("F99", "99_一次倉庫"), ("FX", "その他")]
    )
    _check_rulebook_root(_sources_with_folders(["F01", "F02"]), request_id="req-t", client=client)


def test_root_check_missing_nn_folder_exits_1(_root_env: pytest.MonkeyPatch) -> None:
    """NN_ フォルダが yaml に不足 → exit 1（silent 未取込の防止）。"""
    client = _FakeRootClient([("F01", "01_提案事例"), ("F02", "02_議事録")])
    with pytest.raises(SystemExit) as excinfo:
        _check_rulebook_root(_sources_with_folders(["F01"]), request_id="req-t", client=client)
    assert excinfo.value.code == 1


def test_root_check_banned_99_folder_in_yaml_exits_1(_root_env: pytest.MonkeyPatch) -> None:
    """99_ 系フォルダが yaml に登録されている → exit 1（一次倉庫の誤取込防止）。"""
    client = _FakeRootClient([("F01", "01_提案事例"), ("F99", "99_一次倉庫")])
    with pytest.raises(SystemExit) as excinfo:
        _check_rulebook_root(
            _sources_with_folders(["F01", "F99"]), request_id="req-t", client=client
        )
    assert excinfo.value.code == 1


def test_root_check_warn_only_downgrades_exit(_root_env: pytest.MonkeyPatch) -> None:
    """INGEST_ROOT_CHECK_WARN_ONLY=true で exit を WARNING に降格できる。"""
    _root_env.setenv("INGEST_ROOT_CHECK_WARN_ONLY", "true")
    client = _FakeRootClient([("F01", "01_提案事例"), ("F99", "99_一次倉庫")])
    # 不足（F01 未登録）と 99_ 誤登録（F99 登録）を同時に踏んでも例外にならない
    _check_rulebook_root(_sources_with_folders(["F99"]), request_id="req-t", client=client)


def test_root_check_list_failure_is_fail_loud(_root_env: pytest.MonkeyPatch) -> None:
    """ルート列挙自体の失敗も exit 1（検査できない状態で黙って進まない）。"""
    client = _FakeRootClient([], error=RuntimeError("boom"))
    with pytest.raises(SystemExit) as excinfo:
        _check_rulebook_root(_sources_with_folders([]), request_id="req-t", client=client)
    assert excinfo.value.code == 1


def test_root_check_non_nn_folders_are_ignored(_root_env: pytest.MonkeyPatch) -> None:
    """NN_ 接頭でないフォルダは検査対象外（yaml 未登録でも通過）。"""
    client = _FakeRootClient([("FX", "アーカイブ"), ("FY", "撮影素材")])
    _check_rulebook_root(_sources_with_folders([]), request_id="req-t", client=client)


# -----------------------------------------------------------
# 6. export_vault の stale 除外
# -----------------------------------------------------------
def test_export_documents_sql_excludes_stale_by_default() -> None:
    """既定 SQL は metadata.stale='true' を除外する節を含む。"""
    sql = _export_vault.documents_sql()
    assert "d.metadata->>'stale' IS DISTINCT FROM 'true'" in sql


def test_export_documents_sql_include_stale_restores_legacy() -> None:
    """include_stale=True では stale 節が消え、従来挙動（全件）に戻る。"""
    sql = _export_vault.documents_sql(include_stale=True)
    assert "'stale'" not in sql


def test_export_load_clients_data_defaults_to_exclude_stale() -> None:
    """stale は既定除外、shared_group は省略不可。"""
    import inspect

    sig = inspect.signature(_export_vault.load_clients_data)
    assert sig.parameters["include_stale"].default is False
    assert sig.parameters["shared_group"].annotation == "str"
    assert sig.parameters["shared_group"].default is inspect.Parameter.empty
