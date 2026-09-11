"""mcp 便ランチャーの buildspec 世代プリフライトと失敗ログ抜粋のテスト。

由来: 2026-09-11 の mcp 便 r20 段2
（build teamagent-dev-mcp-source-publisher:679b024e-1208-474f-80e0-08c8cde4c0a1）。
契約 JSON を変えた #398 が merge された一方で buildspec の S3 publish /
UpdateProject が未実施だったため、live 世代 1ed75a2e… が repo 期待値 8e27d780… から
取り残され、段2 pre_build の
`FATAL: embedded release contract hash mismatch` で停止した。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
LAUNCHER = PROJECT_ROOT / "scripts" / "aws" / "release_mcp.sh"
ASSERT_GENERATION = PROJECT_ROOT / "infra" / "deploy" / "assert_published_generation.py"
FAILURE_EXCERPT = PROJECT_ROOT / "scripts" / "aws" / "codebuild_failure_excerpt.py"
GENERATION_MANIFEST = PROJECT_ROOT / "infra" / "deploy" / "buildspec_generation_inputs.json"

BUCKET = "teamagent-dev-image-release-evidence"
SOURCE_PUBLISHER = "teamagent-dev-mcp-source-publisher"

# r20 の実測値（CloudWatch / batch-get-projects で確認済み）。
LIVE_STALE_GENERATION = "1ed75a2efc69b675d32e9a8197fa4943378e8448ec566d5e9c9bba6040cf7245"
REPO_EXPECTED_GENERATION = "8e27d780948858e00b2f2f41c2a76772e405931a351bc31fcf7aa2ae9574f6eb"


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def generation_mod() -> ModuleType:
    return _load(ASSERT_GENERATION, "assert_published_generation")


@pytest.fixture(scope="module")
def excerpt_mod() -> ModuleType:
    return _load(FAILURE_EXCERPT, "codebuild_failure_excerpt")


def _pin(project: str, generation: str) -> str:
    return f"arn:aws:s3:::{BUCKET}/codebuild-buildspecs/{project}/{generation}.yml"


# --------------------------------------------------------------------------
# 世代プリフライト
# --------------------------------------------------------------------------


def test_manifest_still_declares_the_source_publisher_generation() -> None:
    """manifest の綴りが変わったらプリフライトが空振りするので固定する。"""
    manifest = json.loads(GENERATION_MANIFEST.read_text(encoding="utf-8"))
    expected = manifest["expected_generation_sha256"]
    assert SOURCE_PUBLISHER in expected
    for project, generation in expected.items():
        assert isinstance(generation, str) and len(generation) == 64, project


def test_r20_stale_pin_is_rejected(generation_mod: ModuleType) -> None:
    """r20 の実測 pin をそのまま与えると落ちること（本件の再現）。"""
    expected = {SOURCE_PUBLISHER: REPO_EXPECTED_GENERATION}
    pins = {SOURCE_PUBLISHER: _pin(SOURCE_PUBLISHER, LIVE_STALE_GENERATION)}
    with pytest.raises(generation_mod.GenerationPinError) as excinfo:
        generation_mod.assert_pins(expected, pins)
    message = str(excinfo.value)
    assert "STALE" in message
    assert LIVE_STALE_GENERATION[:16] in message
    assert REPO_EXPECTED_GENERATION[:16] in message


def test_matching_pin_is_accepted(generation_mod: ModuleType) -> None:
    expected = {SOURCE_PUBLISHER: REPO_EXPECTED_GENERATION}
    pins = {SOURCE_PUBLISHER: _pin(SOURCE_PUBLISHER, REPO_EXPECTED_GENERATION)}
    assert generation_mod.assert_pins(expected, pins)


def test_missing_project_fails_closed(generation_mod: ModuleType) -> None:
    """読めなかったプロジェクトを「問題なし」に倒さない。"""
    expected = {SOURCE_PUBLISHER: REPO_EXPECTED_GENERATION, "other": REPO_EXPECTED_GENERATION}
    pins = {SOURCE_PUBLISHER: _pin(SOURCE_PUBLISHER, REPO_EXPECTED_GENERATION)}
    with pytest.raises(generation_mod.GenerationPinError, match="取得できなかった"):
        generation_mod.assert_pins(expected, pins)


def test_inline_buildspec_is_rejected(generation_mod: ModuleType) -> None:
    """content-addressed pin が外れている（インライン buildspec）状態を通さない。"""
    with pytest.raises(generation_mod.GenerationPinError, match="インライン"):
        generation_mod.normalize_pins([{"name": SOURCE_PUBLISHER, "buildspec": None}])


def test_cross_project_pin_is_rejected(generation_mod: ModuleType) -> None:
    """別プロジェクトの buildspec を指す取り違え publish を検出する。"""
    expected = {SOURCE_PUBLISHER: REPO_EXPECTED_GENERATION}
    pins = {SOURCE_PUBLISHER: _pin("teamagent-dev-image-promoter", REPO_EXPECTED_GENERATION)}
    with pytest.raises(generation_mod.GenerationPinError, match="プロジェクト区画"):
        generation_mod.assert_pins(expected, pins)


def test_foreign_bucket_is_rejected(generation_mod: ModuleType) -> None:
    expected = {SOURCE_PUBLISHER: REPO_EXPECTED_GENERATION}
    pins = {
        SOURCE_PUBLISHER: (
            f"arn:aws:s3:::attacker-bucket/codebuild-buildspecs/"
            f"{SOURCE_PUBLISHER}/{REPO_EXPECTED_GENERATION}.yml"
        )
    }
    with pytest.raises(generation_mod.GenerationPinError, match="bucket"):
        generation_mod.assert_pins(expected, pins)


def test_cli_accepts_aws_cli_shape(generation_mod: ModuleType, tmp_path: Path) -> None:
    """aws codebuild batch-get-projects --query の出力形をそのまま食えること。"""
    manifest = tmp_path / "m.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "expected_generation_sha256": {SOURCE_PUBLISHER: REPO_EXPECTED_GENERATION},
            }
        ),
        encoding="utf-8",
    )
    aws_shape = [
        {"name": SOURCE_PUBLISHER, "buildspec": _pin(SOURCE_PUBLISHER, LIVE_STALE_GENERATION)}
    ]
    assert (
        generation_mod.main(["--manifest", str(manifest), "--pins-json", json.dumps(aws_shape)])
        == 1
    )


# --------------------------------------------------------------------------
# 失敗ログ抜粋
# --------------------------------------------------------------------------


def _codebuild_log(script_lines: list[str], real_output: list[str]) -> dict:
    """CodeBuild の実ログ構造を再現する。

    本番の失敗モードを写す: ①commands ブロック全文が 1 行 1 イベントでエコーされる
    ②エコーは 1 回の書き込みなので全行が同一 timestamp ③失敗時に全文がもう一度
    エコーされ、そちらはログ側で打ち切られることがある。
    """
    echo_ts = 1_789_095_923_017
    out_ts = 1_789_095_937_653
    events: list[dict] = [
        {
            "timestamp": echo_ts,
            "message": "[Container] 2026/09/11 03:05:19 Entering phase PRE_BUILD\n",
        },
        {
            "timestamp": echo_ts,
            "message": f"[Container] 2026/09/11 03:05:20 Running command {script_lines[0]}\n",
        },
    ]
    events += [{"timestamp": echo_ts, "message": f"{line}\n"} for line in script_lines[1:]]
    events += [{"timestamp": out_ts, "message": f"{line}\n"} for line in real_output]
    events.append(
        {
            "timestamp": out_ts,
            "message": f"[Container] 2026/09/11 03:05:37 Command did not exit successfully {script_lines[0]}\n",
        }
    )
    # 2 度目のエコーは途中で打ち切られる（実測: 208 行中 122 行）。
    events += [
        {"timestamp": out_ts, "message": f"{line}\n"}
        for line in script_lines[1 : len(script_lines) // 2]
    ]
    return {"events": events}


def test_excerpt_returns_real_output_not_echoed_source(excerpt_mod: ModuleType) -> None:
    """本件の核心: エコーされたソース断片ではなく本物の出力を返すこと。"""
    script = [
        "set -euo pipefail",
        '[ "$a" = "$b" ] || { echo "FATAL: embedded release contract hash mismatch"; exit 1; }',
        '[ "${#REC[@]}" -eq 4 ] || { echo "FATAL: latest production app record is incomplete"; exit 1; }',
        '  || { echo "FATAL: baked fallback key differs from the fixed release location"; exit 1; }',
        "readonly BAKED_APP_HTML_KEY APP_PROVENANCE_SHA256",
    ]
    log = _codebuild_log(script, ["", "FATAL: embedded release contract hash mismatch", ""])
    lines = excerpt_mod.extract(excerpt_mod.load_events(log))
    assert lines == ["FATAL: embedded release contract hash mismatch"]
    # 旧実装が拾っていたソース断片が 1 つも混じらないこと。
    assert not any("|| {" in line for line in lines)


def test_excerpt_survives_missing_timestamps(excerpt_mod: ModuleType) -> None:
    """timestamp が無い入力でも lockstep 副系統でエコーを削れること。"""
    script = ["set -euo pipefail", "echo one", "echo two", "echo three", "echo four"]
    log = _codebuild_log(script, ["real failure line"])
    for event in log["events"]:
        del event["timestamp"]
    lines = excerpt_mod.extract(excerpt_mod.load_events(log))
    assert "real failure line" in lines


def test_excerpt_never_raises_on_garbage(excerpt_mod: ModuleType) -> None:
    """診断の失敗で本体の失敗を隠さない。"""
    assert excerpt_mod.extract(excerpt_mod.load_events({"events": []})) == []
    assert excerpt_mod.extract(excerpt_mod.load_events([])) == []


# --------------------------------------------------------------------------
# ランチャー本体（静的検証）
# --------------------------------------------------------------------------


def test_launcher_syntax_ok() -> None:
    result = subprocess.run(
        ["bash", "-n", str(LAUNCHER)], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr


def test_launcher_calls_the_preflight_before_stage_one() -> None:
    """プリフライトが段1 の StartBuild より前にあること（承認を無駄に焼かない）。"""
    body = LAUNCHER.read_text(encoding="utf-8")
    preflight = body.index("assert_published_generation die")
    stage_one = body.index("start_and_wait teamagent-dev-approval-publisher")
    assert preflight < stage_one


def test_launcher_no_longer_greps_the_echoed_tail() -> None:
    """`--limit 40` 末尾 grep（誤診断の元）が復活していないこと。

    由来をコメントで残しているので、コード行だけを見る。
    """
    body = LAUNCHER.read_text(encoding="utf-8")
    code = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))
    assert "--limit 40" not in code
    assert "codebuild_failure_excerpt.py" in code
    # 失敗出力は末尾側にあるので、取得窓は API 上限まで広げる。
    assert "--limit 10000" in code
    # 生ログを grep して「それらしい行」を出す経路が戻っていないこと。
    assert 'grep -iE "FATAL|error|fail"' not in code
