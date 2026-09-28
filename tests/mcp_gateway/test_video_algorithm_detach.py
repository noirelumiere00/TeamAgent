"""video_algorithm の切り離し（detach）のテスト（mcp_gateway/detached_jobs.py・server.dispatch_tool）。

本番の失敗モード（2026-09-25）: OpenClaw が約 6 分で実行を打ち切り、mcp が 555 秒で完走しても
結果の戻り先が無かった。ここでは次を偽物で再現して確かめる:
- skill が長く止まる（Event で塞ぐ）→ 受付が先に返り、解放後に検証済みの宛先へ届く
- 待っている dispatch が打ち切られる（CancelledError）→ ジョブは止まらず完走して届く
- raw の ``_user_context`` に別の宛先を混ぜる → 投稿先は署名検証済み claim のまま
- プロセス終了（lifespan 終了・uvicorn の shutdown）→ 処理中ジョブの宛先へ中断通知
- 同じ引数・同じ KW の二重依頼 → quota と Gemini は 1 回分（本物の skill＋S3 lease の偽物）
- Gemini の 429 → 本物の call_with_retry で再試行した後に届く
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel

from teamagent.adapters.gemini_client import GeminiClient, GeminiResponse
from teamagent.adapters.quota_store import QuotaResult
from teamagent.adapters.slack_client import SlackPostResult
from teamagent.adapters.video_algorithm_cache import VideoAlgorithmResultCache
from teamagent.identity import ResolvedIdentity
from teamagent.mcp_gateway import detached_jobs, server
from teamagent.mcp_gateway.caller_claim import VerifiedCallerClaim
from teamagent.mcp_gateway.server import USER_CONTEXT_KEY, dispatch_tool
from teamagent.orchestrator.tools import ToolSpec
from teamagent.skills._shared.slack_mrkdwn import markdown_bold_to_mrkdwn
from teamagent.skills.base import BaseSkill, SkillContext
from teamagent.skills.video_algorithm.schema import VideoAlgorithmInput, VideoMeta
from teamagent.skills.video_algorithm.skill import VideoAlgorithmSkill

ME = "s-komata@vectorinc.co.jp"
OTHER = "someone@vectorinc.co.jp"
USER_ID = "U0123456789"
TEAM_ID = "T0123456789"
DM = "D0123456789"
CHANNEL = "C0123456789"
TOOL = "video_algorithm"
INTERNAL_WORDS = ("job_id", "error_code", "VIDEO_", "MEDIA_", "s3://", "amazonaws", "request_id")


# ── 共通の偽物 ───────────────────────────────────────────────────────────


def _claim(
    *, channel: str = DM, thread_ts: str | None = None, message_id: str = "1784424000.000001"
) -> VerifiedCallerClaim:
    return VerifiedCallerClaim(
        slack_user_id=USER_ID,
        slack_team_id=TEAM_ID,
        channel_id=channel,
        thread_ts=thread_ts,
        message_id=message_id,
        session_sha256="0" * 64,
        run_id="11111111-1111-4111-8111-111111111111",
        tool_call_id="toolu_0123456789abcdef",
        nonce="test-nonce",
        issued_at=1,
        expires_at=2,
    )


class _Verifier:
    """署名 claim の検証済み結果を返す（本番の CallerClaimVerifier.verify と同じ呼び方）。"""

    def __init__(self, claim: VerifiedCallerClaim) -> None:
        self.claim = claim

    async def verify(self, *, tool: str, arguments: dict[str, Any]) -> VerifiedCallerClaim:
        return self.claim


def _resolver_for(email: str) -> Callable[[str], Any]:
    async def _resolve(slack_user_id: str) -> ResolvedIdentity:
        return ResolvedIdentity(slack_user_id=slack_user_id, email=email)

    return _resolve


class _FakeSlack:
    """SlackClient.post_message の代わり。失敗回数を指定すると最初の n 回は例外（本番の一過性障害）。"""

    def __init__(self, fail_first: int = 0) -> None:
        self.posts: list[dict[str, Any]] = []
        self.fail_first = fail_first
        self.lock = threading.Lock()

    async def post_message(
        self,
        channel: str,
        text: str,
        request_id: str,
        thread_ts: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
    ) -> SlackPostResult:
        with self.lock:
            if self.fail_first > 0:
                self.fail_first -= 1
                raise ConnectionError("slack temporarily unavailable")
            self.posts.append({"channel": channel, "text": text, "thread_ts": thread_ts})
        return SlackPostResult(channel=channel, ts="1784424999.000100", ok=True)


class _Out(BaseModel):
    query: str
    slack_summary: str = ""
    total_cost_usd: float = 0.0


class _GateSkill(BaseSkill[VideoAlgorithmInput, _Out]):
    """query が「遅い」で始まるときだけ release まで止まる video_algorithm の偽物。"""

    name: ClassVar[str] = TOOL
    description: ClassVar[str] = "fake"
    input_schema: ClassVar[type[BaseModel]] = VideoAlgorithmInput
    output_schema: ClassVar[type[BaseModel]] = _Out

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.runs = 0
        self.cleanups = 0
        self.error: BaseException | None = None

    def run(self, input: VideoAlgorithmInput, ctx: SkillContext) -> _Out:
        self.runs += 1
        self.started.set()
        if input.query.startswith("遅い"):
            assert self.release.wait(10), "test forgot to release the skill"
        if self.error is not None:
            raise self.error
        return _Out(
            query=input.query,
            slack_summary=(
                f"🔎 **VSEO動画アルゴリズム分析** 完了「{input.query}」（上位5本／分析成功5本）\n"
                "📄 詳細レポート（7日有効）: https://example.invalid/r/abc\n"
                "_概算 $0.0200・n=5 の観測仮説（相関≠因果）_"
            ),
            total_cost_usd=0.02,
        )

    def cleanup_output(self, output: _Out) -> None:
        self.cleanups += 1


def _policy(**overrides: Any) -> detached_jobs.DetachPolicy:
    values: dict[str, Any] = {
        "enabled": True,
        "detach_after_s": 0.05,
        "allowed_emails": frozenset({ME}),
        "dm_only": True,
        "max_background": 2,
    }
    values.update(overrides)
    return detached_jobs.DetachPolicy(**values)


@pytest.fixture
def slack(monkeypatch: pytest.MonkeyPatch) -> _FakeSlack:
    fake = _FakeSlack()
    monkeypatch.setattr(detached_jobs, "_slack_client", lambda timeout_seconds: fake)
    monkeypatch.setattr(detached_jobs, "_POST_RETRY_WAIT_S", 0.0)
    return fake


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """登録簿をテストごとに作り直し、usage 記録は数えるだけにする。"""
    for name in (
        detached_jobs.ENABLED_ENV,
        detached_jobs.AFTER_ENV,
        detached_jobs.ALLOWED_EMAILS_ENV,
        detached_jobs.DM_ONLY_ENV,
        detached_jobs.MAX_BACKGROUND_ENV,
        "ENABLE_PROGRESS_NOTIFY",
        "USE_PAYLOAD_OFFLOAD",
        "VIDEO_QUOTA_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(detached_jobs, "REGISTRY", detached_jobs.DetachedJobRegistry())
    records: list[dict[str, Any]] = []
    monkeypatch.setattr(server, "_record_usage", lambda **kw: records.append(kw))
    return records


@pytest.fixture
def usage(_isolated: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _isolated


def _spec(skill: BaseSkill[Any, Any]) -> dict[str, ToolSpec]:
    return {TOOL: ToolSpec(TOOL, "fake", type(skill), factory=lambda: skill)}


async def _call(
    by_name: dict[str, ToolSpec],
    args: dict[str, Any],
    *,
    claim: VerifiedCallerClaim | None = None,
    raw: dict[str, Any] | None = None,
    email: str = ME,
) -> dict[str, Any]:
    contents = await dispatch_tool(
        by_name,
        TOOL,
        {**args, USER_CONTEXT_KEY: raw or {}},
        identity_resolver=_resolver_for(email),
        caller_claim_verifier=_Verifier(claim or _claim()),  # type: ignore[arg-type]
    )
    return json.loads(contents[0].text)  # type: ignore[no-any-return]


async def _eventually(check: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not met in time")


def _assert_no_internal_words(text: str) -> None:
    for word in INTERNAL_WORDS:
        assert word not in text, word


# ── 受付・届け先・変換 ──────────────────────────────────────────────────────


async def test_receipt_returns_first_and_result_is_posted_later(
    monkeypatch: pytest.MonkeyPatch, slack: _FakeSlack, usage: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(detached_jobs, "load_policy", lambda: _policy())
    skill = _GateSkill()

    started = time.monotonic()
    out = await _call(_spec(skill), {"query": "遅い 新宿ランチ"})
    assert time.monotonic() - started < 3  # skill は止まったまま＝受付が先に返る

    assert out["status"] == "running"
    assert "終わったらこの会話にお届けします" in out["message"]
    assert out["slack_summary"] == out["message"]
    _assert_no_internal_words(json.dumps(out, ensure_ascii=False))
    assert slack.posts == []
    assert skill.cleanups == 0 and usage == []  # 完了前に後始末も課金記録もしない

    skill.release.set()
    await _eventually(lambda: len(slack.posts) == 1 and len(usage) == 1)
    assert skill.cleanups == 1
    assert usage[0]["status"] == "ok" and usage[0]["cost_usd"] == 0.02
    assert usage[0]["user_id"] == USER_ID and usage[0]["user_email"] == ME
    assert detached_jobs.REGISTRY.active_count() == 0


async def test_destination_is_the_verified_claim_not_raw_user_context(
    monkeypatch: pytest.MonkeyPatch, slack: _FakeSlack
) -> None:
    """raw の _user_context に別の宛先を混ぜても、投稿先は署名検証済み claim のまま。"""
    monkeypatch.setattr(detached_jobs, "load_policy", lambda: _policy())
    skill = _GateSkill()
    out = await _call(
        _spec(skill),
        {"query": "遅い 新宿ランチ"},
        claim=_claim(channel=DM, thread_ts="1784424000.000009"),
        raw={"channel_id": "C0SPOOFED99", "thread_ts": "1111111111.000001", "slack_user_id": "UX"},
    )
    assert out["status"] == "running"
    skill.release.set()
    await _eventually(lambda: len(slack.posts) == 1)
    assert slack.posts[0]["channel"] == DM
    assert slack.posts[0]["thread_ts"] == "1784424000.000009"


async def test_completion_post_converts_markdown_bold_to_mrkdwn(
    monkeypatch: pytest.MonkeyPatch, slack: _FakeSlack
) -> None:
    """直接投稿は OpenClaw の Markdown→mrkdwn 変換を通らないので、`**語**` を `*語*` に直す。"""
    monkeypatch.setattr(detached_jobs, "load_policy", lambda: _policy())
    skill = _GateSkill()
    await _call(_spec(skill), {"query": "遅い 新宿ランチ"})
    skill.release.set()
    await _eventually(lambda: len(slack.posts) == 1)
    text = slack.posts[0]["text"]
    assert text.startswith("🔎 *VSEO動画アルゴリズム分析* 完了「遅い 新宿ランチ」")
    assert "**" not in text
    assert "https://example.invalid/r/abc" in text  # レポート URL はそのまま
    assert "_概算 $0.0200" in text  # 斜体は素通し


def test_markdown_bold_converter_leaves_other_markup() -> None:
    assert (
        markdown_bold_to_mrkdwn("**見出し** と _斜体_ と *太字*") == "*見出し* と _斜体_ と *太字*"
    )
    assert markdown_bold_to_mrkdwn("** 空白 **") == "** 空白 **"


async def test_fast_completion_stays_synchronous_and_unchanged(
    monkeypatch: pytest.MonkeyPatch, slack: _FakeSlack, usage: list[dict[str, Any]]
) -> None:
    """待ち時間内に終われば今までどおり同期で返す（キャッシュヒット等）。投稿はしない。"""
    monkeypatch.setattr(detached_jobs, "load_policy", lambda: _policy(detach_after_s=5.0))
    skill = _GateSkill()
    out = await _call(_spec(skill), {"query": "新宿ランチ"})
    assert "status" not in out
    assert out["slack_summary"].startswith(
        "🔎 **VSEO動画アルゴリズム分析**"
    )  # 変換しない（OC が変換）
    await asyncio.sleep(0.05)
    assert slack.posts == []
    assert skill.cleanups == 1 and len(usage) == 1


# ── 打ち切り（CancelledError）───────────────────────────────────────────────


async def test_caller_cancel_does_not_stop_the_job_and_result_is_delivered(
    monkeypatch: pytest.MonkeyPatch, slack: _FakeSlack, usage: list[dict[str, Any]]
) -> None:
    """OpenClaw が先に切っても（dispatch の task が cancel）、ジョブは完走して会話に届く。"""
    monkeypatch.setattr(detached_jobs, "load_policy", lambda: _policy(detach_after_s=30.0))
    skill = _GateSkill()
    task = asyncio.create_task(_call(_spec(skill), {"query": "遅い 新宿ランチ"}))
    await _eventually(skill.started.is_set)
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    skill.release.set()
    await _eventually(lambda: len(slack.posts) == 1 and len(usage) == 1)
    assert slack.posts[0]["channel"] == DM
    assert "VSEO動画アルゴリズム分析" in slack.posts[0]["text"]
    assert skill.runs == 1 and skill.cleanups == 1


# ── 失敗時の文面 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            RuntimeError(
                "VIDEO_QUOTA_EXCEEDED: 今月の動画分析上限（50本）に達しました（使用 50本）。"
                "リセットは来月1日（JST）です。"
            ),
            "今月の動画分析上限（50本）に達しました",
        ),
        (RuntimeError("MEDIA_FRAME_JOB_FAILED"), "動画の取得・変換で一時的な不具合"),
        (
            RuntimeError("VIDEO_ALGORITHM_CACHE_UNAVAILABLE: 処理中リースを確認できない"),
            "一時的な不具合",
        ),
        (ValueError("boom s3://bucket/key"), "分析の途中で問題が起きて"),
    ],
)
async def test_failure_after_detach_posts_user_words_only(
    monkeypatch: pytest.MonkeyPatch,
    slack: _FakeSlack,
    usage: list[dict[str, Any]],
    error: BaseException,
    expected: str,
) -> None:
    monkeypatch.setattr(detached_jobs, "load_policy", lambda: _policy())
    skill = _GateSkill()
    skill.error = error
    await _call(_spec(skill), {"query": "遅い 新宿ランチ"})
    skill.release.set()
    await _eventually(lambda: len(slack.posts) == 1 and len(usage) == 1)
    text = slack.posts[0]["text"]
    assert expected in text
    _assert_no_internal_words(text)
    assert usage[0]["status"] == "error" and usage[0]["error_code"] == type(error).__name__
    assert skill.cleanups == 0  # 出力が無いので後始末の対象も無い


def test_post_retries_once_after_a_transient_slack_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeSlack(fail_first=1)
    monkeypatch.setattr(detached_jobs, "_slack_client", lambda timeout_seconds: fake)
    monkeypatch.setattr(detached_jobs, "_POST_RETRY_WAIT_S", 0.0)
    dest = detached_jobs.Destination(channel_id=DM, thread_ts=None)
    assert detached_jobs.post_to_origin("hi", dest, request_id="r") is True
    assert len(fake.posts) == 1


# ── 対象外は今と同じ（同期のまま）───────────────────────────────────────────


class _MustNotStart(detached_jobs.DetachedJobRegistry):
    def start(self, **kwargs: Any) -> Any:
        raise AssertionError("this call must stay synchronous")


@pytest.mark.parametrize(
    ("policy", "email", "claim"),
    [
        (_policy(enabled=False), ME, _claim()),  # フラグ OFF
        (_policy(allowed_emails=frozenset()), ME, _claim()),  # allowlist 空＝誰にも適用しない
        (_policy(), OTHER, _claim()),  # allowlist 外
        (_policy(), ME, _claim(channel=CHANNEL, thread_ts="1784424000.000009")),  # DM 以外
        (_policy(max_background=0), ME, _claim()),  # 上限 0
    ],
    ids=["flag_off", "empty_allowlist", "not_allowed", "not_dm", "no_capacity"],
)
async def test_calls_outside_the_policy_stay_synchronous(
    monkeypatch: pytest.MonkeyPatch,
    slack: _FakeSlack,
    usage: list[dict[str, Any]],
    policy: detached_jobs.DetachPolicy,
    email: str,
    claim: VerifiedCallerClaim,
) -> None:
    monkeypatch.setattr(detached_jobs, "load_policy", lambda: policy)
    monkeypatch.setattr(detached_jobs, "REGISTRY", _MustNotStart())
    skill = _GateSkill()
    out = await _call(_spec(skill), {"query": "新宿ランチ"}, claim=claim, email=email)
    assert "status" not in out and out["query"] == "新宿ランチ"
    assert slack.posts == [] and skill.cleanups == 1 and len(usage) == 1


async def test_flag_off_by_default_never_touches_the_registry(
    monkeypatch: pytest.MonkeyPatch, slack: _FakeSlack
) -> None:
    """env 未設定（本番の現状）では登録簿にも触れない＝今と完全に同じ経路。"""

    class _Untouchable:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"registry.{name} must not be used when the flag is off")

    monkeypatch.setattr(detached_jobs, "REGISTRY", _Untouchable())
    skill = _GateSkill()
    out = await _call(_spec(skill), {"query": "新宿ランチ"})
    assert "status" not in out and slack.posts == []


async def test_legacy_mode_without_verified_caller_stays_synchronous(
    monkeypatch: pytest.MonkeyPatch, slack: _FakeSlack
) -> None:
    """resolver の無い LEGACY は宛先を信用できないので切り離さない。"""
    monkeypatch.setattr(detached_jobs, "load_policy", lambda: _policy())
    monkeypatch.setattr(detached_jobs, "REGISTRY", _MustNotStart())
    skill = _GateSkill()
    contents = await dispatch_tool(
        _spec(skill),
        TOOL,
        {"query": "新宿ランチ", USER_CONTEXT_KEY: {"user_email": ME, "channel_id": DM}},
        require_rls=True,
    )
    out = json.loads(contents[0].text)
    assert "status" not in out and slack.posts == []


async def test_capacity_full_falls_back_to_synchronous(
    monkeypatch: pytest.MonkeyPatch, slack: _FakeSlack, usage: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(detached_jobs, "load_policy", lambda: _policy(max_background=1))
    skill = _GateSkill()
    first = await _call(_spec(skill), {"query": "遅い 新宿ランチ"})
    assert first["status"] == "running"
    second = await _call(_spec(skill), {"query": "渋谷カフェ"})  # 別 KW・上限超え＝同期
    assert "status" not in second and second["query"] == "渋谷カフェ"
    skill.release.set()
    await _eventually(lambda: len(slack.posts) == 1 and len(usage) == 2)


# ── 再デプロイ（プロセス終了）────────────────────────────────────────────────


def _load_http_server_module() -> Any:
    path = Path(__file__).resolve().parents[2] / "scripts" / "run_mcp_http_server.py"
    spec = importlib.util.spec_from_file_location("run_mcp_http_server_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


async def test_lifespan_shutdown_notifies_interrupted_jobs_once(
    monkeypatch: pytest.MonkeyPatch, slack: _FakeSlack, usage: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(detached_jobs, "load_policy", lambda: _policy())
    skill = _GateSkill()
    await _call(_spec(skill), {"query": "遅い 新宿ランチ"})

    module = _load_http_server_module()
    monkeypatch.setattr(module, "build_production_server", lambda: server.build_server(specs=[]))
    app = module.build_app(bearer="x" * 32, path="/mcp")
    async with app.router.lifespan_context(app):
        pass

    assert len(slack.posts) == 1
    assert slack.posts[0]["channel"] == DM
    assert "システム更新で中断されました" in slack.posts[0]["text"]
    _assert_no_internal_words(slack.posts[0]["text"])

    # 2 回目（uvicorn の shutdown 入口と lifespan の両方から呼ばれる）でも重ねて送らない。
    assert await detached_jobs.notify_interrupted() == 0
    # 中断を知らせた後に完了しても、矛盾する 2 通目は出さない（後始末と記録は 1 回ずつ行う）。
    skill.release.set()
    await _eventually(lambda: len(usage) == 1)
    assert len(slack.posts) == 1 and skill.cleanups == 1


async def test_uvicorn_shutdown_entry_notifies_before_draining_connections(
    monkeypatch: pytest.MonkeyPatch, slack: _FakeSlack
) -> None:
    """SSE 接続が残ると lifespan 終了に届く前に SIGKILL されうるので、shutdown の入口で先に送る。"""
    import uvicorn

    monkeypatch.setattr(detached_jobs, "load_policy", lambda: _policy())
    skill = _GateSkill()
    await _call(_spec(skill), {"query": "遅い 新宿ランチ"})

    module = _load_http_server_module()
    drained: list[int] = []

    async def _base_shutdown(self: Any, sockets: Any = None) -> None:
        drained.append(len(slack.posts))

    monkeypatch.setattr(uvicorn.Server, "shutdown", _base_shutdown)
    srv = module._DetachAwareServer(uvicorn.Config(app=lambda *a: None))
    await srv.shutdown()
    assert drained == [1]  # 接続の片付け（基底の shutdown）より前に中断通知が出ている
    skill.release.set()
    await _eventually(lambda: skill.cleanups == 1)


# ── 二重依頼（本物の skill・S3 lease の偽物・quota を数える）─────────────────────


class _FakeS3:
    """S3 の条件付き書込（IfNoneMatch/IfMatch）を再現する（lease の排他はこれに依存する）。"""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.etags: dict[str, str] = {}
        self._version = 0
        self._lock = threading.Lock()

    def get_object(self, Bucket: str, Key: str) -> Any:  # noqa: N803 - boto3 naming
        with self._lock:
            if Key not in self.store:
                raise type("NoSuchKey", (Exception,), {})()
            body = self.store[Key]
            etag = self.etags.get(Key, "")
        return {"Body": type("_Body", (), {"read": lambda self2: body})(), "ETag": etag}

    def put_object(self, Bucket: str, Key: str, Body: bytes, **kwargs: Any) -> None:  # noqa: N803
        with self._lock:
            failed = (kwargs.get("IfNoneMatch") == "*" and Key in self.store) or (
                kwargs.get("IfMatch") is not None and self.etags.get(Key) != kwargs["IfMatch"]
            )
            if failed:
                error = type("PreconditionFailed", (Exception,), {})()
                error.response = {"Error": {"Code": "PreconditionFailed"}}  # type: ignore[attr-defined]
                raise error
            self._version += 1
            self.store[Key] = Body
            self.etags[Key] = f'"v{self._version}"'

    def delete_object(self, Bucket: str, Key: str, **kwargs: Any) -> None:  # noqa: N803
        with self._lock:
            self.store.pop(Key, None)
            self.etags.pop(Key, None)


class _BlockingGemini:
    model_id = "gemini-3.5-flash"

    def __init__(self) -> None:
        self.video_calls = 0
        self.release = threading.Event()
        self._lock = threading.Lock()

    def analyze_video_bytes(self, **kwargs: Any) -> GeminiResponse:
        with self._lock:
            self.video_calls += 1
        assert self.release.wait(10)
        return GeminiResponse(
            text="```json\n{}\n```",
            input_tokens=10,
            output_tokens=10,
            cost_usd=0.01,
            model_id=self.model_id,
            latency_ms=1,
        )

    def generate_text(self, *args: Any, **kwargs: Any) -> GeminiResponse:
        return GeminiResponse(
            text="```json\n{}\n```",
            input_tokens=10,
            output_tokens=10,
            cost_usd=0.01,
            model_id=self.model_id,
            latency_ms=1,
        )


def _real_skill(
    tmp_path: Path, gemini: Any, cache: VideoAlgorithmResultCache
) -> VideoAlgorithmSkill:
    metas = [VideoMeta(rank=i, url=f"https://example.invalid/{i}") for i in range(1, 6)]
    return VideoAlgorithmSkill(
        gemini=gemini,
        searcher=lambda query, limit, request_id: metas[:limit],
        downloader=lambda url: (b"video", "video/mp4"),
        proxy=lambda data, mime: (data, mime),
        report_dir=str(tmp_path),
        result_cache=cache,
    )


@pytest.fixture
def quota(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    import teamagent.adapters.quota_store as quota_store

    monkeypatch.setenv("VIDEO_QUOTA_ENABLED", "1")
    monkeypatch.setenv("TEAMAGENT_LOCAL_MEDIA_RUNTIME", "true")
    consumed: list[int] = []
    lock = threading.Lock()

    def _consume(self: Any, email: str, count: int, *, request_id: str) -> QuotaResult:
        with lock:
            consumed.append(count)
            return QuotaResult(allowed=True, used=sum(consumed), limit=50)

    monkeypatch.setattr(quota_store.VideoQuotaStore, "try_consume", _consume)
    return consumed


async def test_duplicate_requests_use_quota_and_gemini_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, slack: _FakeSlack, quota: list[int]
) -> None:
    """同じ引数・同じ KW（本数違い）の再依頼は、どちらも quota と Gemini を使わない。"""
    monkeypatch.setattr(detached_jobs, "load_policy", lambda: _policy())
    gemini = _BlockingGemini()
    cache = VideoAlgorithmResultCache(bucket="b", client=_FakeS3(), lease_seconds=600)
    spec = {
        TOOL: ToolSpec(
            TOOL, "fake", VideoAlgorithmSkill, factory=lambda: _real_skill(tmp_path, gemini, cache)
        )
    }
    args = {"query": "新宿 ランチ", "max_videos": 1, "board_size": 5, "outputs": ["report"]}

    first = await _call(spec, args)
    assert first["status"] == "running"
    same = await _call(spec, args)
    other_count = await _call(spec, {**args, "max_videos": 2})

    gemini.release.set()
    await _eventually(lambda: len(slack.posts) >= 1 and detached_jobs.REGISTRY.active_count() == 0)
    await asyncio.sleep(0.1)
    # 課金の境界（quota の事前消費と Gemini）が 1 回分だけであること
    assert quota == [1]
    assert gemini.video_calls == 1
    assert len(slack.posts) == 1
    for dup in (same, other_count):
        assert dup["status"] == "running"
        assert "まだ分析中です" in dup["message"]
        _assert_no_internal_words(json.dumps(dup, ensure_ascii=False))


async def test_lease_held_elsewhere_is_rephrased_without_code_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, slack: _FakeSlack, quota: list[int]
) -> None:
    """別プロセスが同じ引数の処理中リースを持っている＝コード名を出さずに言い換え、課金しない。"""
    monkeypatch.setattr(detached_jobs, "load_policy", lambda: _policy(detach_after_s=5.0))
    gemini = _BlockingGemini()
    gemini.release.set()
    cache = VideoAlgorithmResultCache(bucket="b", client=_FakeS3(), lease_seconds=600)
    skill = _real_skill(tmp_path, gemini, cache)
    input_obj = VideoAlgorithmInput(
        query="新宿 ランチ", max_videos=1, board_size=5, outputs=["report"]
    )
    key = cache.cache_key(
        query=input_obj.query,
        max_videos=input_obj.max_videos,
        prompt_version=skill._prompt_version,
        model_id=gemini.model_id,
        board_size=input_obj.board_size,
        outputs=input_obj.outputs,
        kw_set=input_obj.kw_set,
        client_name=input_obj.client_name,
        acquire_job_id=input_obj.acquire_job_id,
        search_volume=input_obj.search_volume,
        requester=ME,
    )
    assert cache.acquire_lease(key, request_id="other-process") is not None

    out = await _call(
        {TOOL: ToolSpec(TOOL, "fake", VideoAlgorithmSkill, factory=lambda: skill)},
        {"query": "新宿 ランチ", "max_videos": 1, "board_size": 5, "outputs": ["report"]},
    )
    assert out["status"] == "running"
    assert "同じ内容の分析がまだ続いています" in out["message"]
    _assert_no_internal_words(json.dumps(out, ensure_ascii=False))
    assert quota == [] and gemini.video_calls == 0 and slack.posts == []


# ── Gemini の 429（本物の GeminiClient と call_with_retry）──────────────────────


class _RateLimitedError(Exception):
    code = 429


class _FakeGenAI:
    """google-genai の client.models.generate_content。最初の n 回は 429 を返す。"""

    def __init__(self, rate_limited_times: int) -> None:
        self.remaining = rate_limited_times
        self.calls = 0
        self.models = self

    def generate_content(self, **kwargs: Any) -> Any:
        self.calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise _RateLimitedError("429 RESOURCE_EXHAUSTED")
        usage = type("U", (), {"prompt_token_count": 10, "candidates_token_count": 10})()
        return type("R", (), {"text": "```json\n{}\n```", "usage_metadata": usage})()


async def test_result_is_delivered_after_gemini_rate_limits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, slack: _FakeSlack, quota: list[int]
) -> None:
    import teamagent.adapters.retry as retry

    monkeypatch.setattr(retry.random, "uniform", lambda a, b: 0.0)  # 待ちは下限 0.5 秒だけ
    monkeypatch.setattr(detached_jobs, "load_policy", lambda: _policy())
    genai = _FakeGenAI(rate_limited_times=2)
    gemini = GeminiClient(model_id="gemini-3.5-flash", client=genai)
    cache = VideoAlgorithmResultCache(bucket="b", client=_FakeS3(), lease_seconds=600)
    out = await _call(
        {
            TOOL: ToolSpec(
                TOOL,
                "fake",
                VideoAlgorithmSkill,
                factory=lambda: _real_skill(tmp_path, gemini, cache),
            )
        },
        {"query": "新宿 ランチ", "max_videos": 1, "board_size": 5, "outputs": ["report"]},
    )
    assert out["status"] == "running"
    await _eventually(lambda: len(slack.posts) == 1, timeout=15)
    assert genai.calls >= 3  # 429 を 2 回受けたあと成功（横断 synthesis は 1 本なので呼ばれない）
    assert "分析成功1本" in slack.posts[0]["text"]
    assert quota == [1]


# ── env の読み方と判定 ──────────────────────────────────────────────────────


def test_policy_defaults_are_off_and_values_are_clamped(monkeypatch: pytest.MonkeyPatch) -> None:
    default = detached_jobs.DetachPolicy.from_env()
    assert default == detached_jobs.DetachPolicy()
    assert (default.enabled, default.detach_after_s, default.allowed_emails) == (
        False,
        30.0,
        frozenset(),
    )
    assert default.dm_only is True and default.max_background == 2

    monkeypatch.setenv(detached_jobs.ENABLED_ENV, " 1 ")
    monkeypatch.setenv(detached_jobs.AFTER_ENV, "1")
    monkeypatch.setenv(detached_jobs.ALLOWED_EMAILS_ENV, " S-Komata@vectorinc.co.jp , ")
    monkeypatch.setenv(detached_jobs.DM_ONLY_ENV, "0")
    monkeypatch.setenv(detached_jobs.MAX_BACKGROUND_ENV, "99")
    policy = detached_jobs.DetachPolicy.from_env()
    assert policy.enabled is True and policy.detach_after_s == 5.0
    assert policy.allowed_emails == frozenset({ME})
    assert policy.dm_only is False and policy.max_background == 10

    monkeypatch.setenv(detached_jobs.AFTER_ENV, "9999")
    assert detached_jobs.DetachPolicy.from_env().detach_after_s == 240.0
    monkeypatch.setenv(detached_jobs.AFTER_ENV, "abc")
    assert detached_jobs.DetachPolicy.from_env().detach_after_s == 30.0


def test_channel_request_without_thread_uses_the_request_message_as_parent() -> None:
    dest = detached_jobs.destination_from_claim(_claim(channel=CHANNEL))
    assert dest == detached_jobs.Destination(channel_id=CHANNEL, thread_ts="1784424000.000001")
    # ts の形でない message_id ではチャンネル直下へ投げない（切り離さない）
    assert detached_jobs.destination_from_claim(_claim(channel=CHANNEL, message_id="m-1")) is None


def test_inflight_key_ignores_case_width_and_spacing() -> None:
    assert detached_jobs.inflight_key(USER_ID, "新宿　ランチ") == detached_jobs.inflight_key(
        USER_ID, " 新宿 ランチ "
    )
    assert detached_jobs.inflight_key(USER_ID, "ABC") == detached_jobs.inflight_key(USER_ID, "abc")
    assert detached_jobs.inflight_key(USER_ID, "a") != detached_jobs.inflight_key("U999", "a")
