"""名前の頭で Slack channel をまとめて取り込む（slack_channel_prefixes・2026-10-02 fp連携）。

固定すること:
- loader: name_prefix は "#" なし・空不可、max_channels は 1 以上。fp連携 のエントリがある
- 展開: 頭が一致し、かつ Aico（bot）が参加している channel だけを取り込む。全角（ｆｐ）も一致
- 未参加の public channel は取り込まず、名前を ingest_slack_prefix_not_member に出す
- yaml に channel_id で書いた channel は二重に取り込まない
- 一覧の取得に失敗した種類（private の missing_scope 等）は飛ばし、残りと明示の channel は続ける
- ページをまたいだ一覧も拾う・上限を超えた分は取り込まない
- IngestRunner の slack kind が、明示の channel に展開分を足して取り込む
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from slack_sdk.errors import SlackApiError
from slack_sdk.web.slack_response import SlackResponse
from structlog.testing import capture_logs

from teamagent.adapters.slack_channel_ingest_client import SlackConversation
from teamagent.ingest.loader import (
    IngestSources,
    SlackChannelPrefixSpec,
    SlackChannelSpec,
    _parse_slack_channel_prefixes,
    load_ingest_sources,
)
from teamagent.ingest.pipeline import _expand_slack_channel_prefixes

ROOT = Path(__file__).resolve().parents[2]
FP = SlackChannelPrefixSpec(
    name_prefix="fp連携", description="d", extra_metadata={"topic": "fp連携"}
)


def _conv(cid: str, name: str, *, member: bool = True, private: bool = False) -> SlackConversation:
    return SlackConversation(channel_id=cid, name=name, is_private=private, is_member=member)


def _missing_scope() -> SlackApiError:
    resp = SlackResponse(
        client=None,
        http_verb="POST",
        api_url="https://slack.com/api/conversations.list",
        req_args={},
        data={"ok": False, "error": "missing_scope", "needed": "groups:read"},
        headers={},
        status_code=200,
    )
    return SlackApiError("The request to the Slack API failed.", resp)


class _FakeClient:
    """conversations.list の振る舞いを写す: types ごとにページ送り・private は参加分だけ・
    scope が無い種類は SlackApiError（missing_scope）。"""

    def __init__(
        self,
        pages: dict[str, list[list[SlackConversation]]],
        *,
        fail: dict[str, Exception] | None = None,
    ) -> None:
        self._pages = pages
        self._fail = fail or {}
        self.calls: list[tuple[str, str | None]] = []

    def list_conversations(
        self, request_id: str, *, types: str, cursor: str | None = None, limit: int = 1000
    ) -> tuple[list[SlackConversation], str | None]:
        self.calls.append((types, cursor))
        if types in self._fail:
            raise self._fail[types]
        pages = self._pages.get(types, [[]])
        index = int(cursor or 0)
        next_cursor = str(index + 1) if index + 1 < len(pages) else None
        return pages[index], next_cursor


# ── loader ────────────────────────────────────────────────


def test_loader_reads_the_fp_prefix_entry() -> None:
    sources = load_ingest_sources(ROOT / "data" / "ingest_sources.yaml")
    (spec,) = sources.slack_channel_prefixes
    assert spec.name_prefix == "fp連携"
    assert spec.include_files is False and spec.oldest_days == 365 and spec.max_channels == 200
    assert spec.extra_metadata == {"topic": "fp連携"}


@pytest.mark.parametrize(
    "item",
    [{"name_prefix": ""}, {"name_prefix": "#fp連携"}, {"name_prefix": "fp", "max_channels": 0}],
)
def test_loader_rejects_bad_prefix_entries(item: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        _parse_slack_channel_prefixes([item])


def test_sources_without_prefixes_default_to_empty() -> None:
    assert (
        IngestSources(
            version=1, slack_channels=(), gdrive_folders=(), gsheets=()
        ).slack_channel_prefixes
        == ()
    )


# ── 展開 ──────────────────────────────────────────────────


def test_expands_only_member_channels_matching_the_prefix() -> None:
    client = _FakeClient(
        {
            "public_channel": [
                [_conv("C1", "fp連携_アース製薬"), _conv("C2", "proj-fp連携-ではない")],
                [_conv("C3", "ｆｐ連携_全角"), _conv("C4", "fp連携_未参加", member=False)],
            ],
            "private_channel": [[_conv("G1", "fp連携_非公開", private=True)]],
        }
    )
    with capture_logs() as logs:
        specs = _expand_slack_channel_prefixes((FP,), (), request_id="r", client=client)
    assert [(s.channel_id, s.channel_name) for s in specs] == [
        ("C1", "#fp連携_アース製薬"),
        ("G1", "#fp連携_非公開"),
        ("C3", "#ｆｐ連携_全角"),
    ]
    assert specs[0].extra_metadata == {"topic": "fp連携", "channel_prefix": "fp連携"}
    assert specs[0].include_files is False and specs[0].oldest_days == 90
    assert ("public_channel", "1") in client.calls  # 2 ページ目も読む
    (not_member,) = [e for e in logs if e["event"] == "ingest_slack_prefix_not_member"]
    assert not_member["channels"] == ["fp連携_未参加"]  # 招待の手がかり


def test_explicit_channel_ids_are_not_ingested_twice() -> None:
    client = _FakeClient({"public_channel": [[_conv("C1", "fp連携_a"), _conv("C2", "fp連携_b")]]})
    explicit = (SlackChannelSpec(channel_id="C1", channel_name="#fp連携_a", description=""),)
    specs = _expand_slack_channel_prefixes((FP,), explicit, request_id="r", client=client)
    assert [s.channel_id for s in specs] == ["C2"]


def test_list_failure_skips_only_that_type() -> None:
    client = _FakeClient(
        {"public_channel": [[_conv("C1", "fp連携_a")]]},
        fail={"private_channel": _missing_scope()},
    )
    with capture_logs() as logs:
        specs = _expand_slack_channel_prefixes((FP,), (), request_id="r", client=client)
    assert [s.channel_id for s in specs] == ["C1"]
    (failed,) = [e for e in logs if e["event"] == "ingest_slack_prefix_list_failed"]
    assert failed["types"] == "private_channel" and "missing_scope" in failed["error"]


def test_more_than_max_channels_are_not_ingested() -> None:
    capped = SlackChannelPrefixSpec(name_prefix="fp連携", description="", max_channels=2)
    client = _FakeClient({"public_channel": [[_conv(f"C{i}", f"fp連携_{i}") for i in range(5)]]})
    specs = _expand_slack_channel_prefixes((capped,), (), request_id="r", client=client)
    assert [s.channel_id for s in specs] == ["C0", "C1"]


def test_no_prefixes_never_touch_slack() -> None:
    class _Boom:
        def list_conversations(self, *a: Any, **k: Any) -> Any:
            raise AssertionError("prefix が無ければ一覧を読まない")

    assert _expand_slack_channel_prefixes((), (), request_id="r", client=_Boom()) == ()


def test_missing_token_keeps_explicit_channels(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    with capture_logs() as logs:
        assert _expand_slack_channel_prefixes((FP,), (), request_id="r") == ()
    assert [e["types"] for e in logs if e["event"] == "ingest_slack_prefix_list_failed"] == [
        "client"
    ]


# ── IngestRunner の slack kind ─────────────────────────────


def test_runner_ingests_explicit_plus_expanded_channels(monkeypatch: pytest.MonkeyPatch) -> None:
    from teamagent.adapters import slack_channel_ingest_client
    from teamagent.ingest import pipeline

    from .test_pipeline import _FakeEmbedder, _FakeRepository

    client = _FakeClient({"public_channel": [[_conv("C9", "fp連携_x"), _conv("C1", "fp連携_dup")]]})
    monkeypatch.setattr(
        slack_channel_ingest_client.SlackChannelIngestClient,
        "from_env",
        classmethod(lambda cls: client),
    )
    seen: list[str] = []

    def _fake_ingest(spec: SlackChannelSpec, **kwargs: Any) -> tuple[int, int]:
        seen.append(spec.channel_id)
        return 1, 1

    monkeypatch.setattr(pipeline, "_ingest_slack_channel", _fake_ingest)
    runner = pipeline.IngestRunner(
        repository=_FakeRepository(),  # type: ignore[arg-type]
        embedder=_FakeEmbedder(),
        owner_email="x@y.jp",
        dry_run=True,
    )
    sources = IngestSources(
        version=1,
        slack_channels=(SlackChannelSpec(channel_id="C1", channel_name="#明示", description=""),),
        gdrive_folders=(),
        gsheets=(),
        slack_channel_prefixes=(FP,),
    )
    runner.run(sources, kinds=["slack"])
    assert seen == ["C1", "C9"]


# ── client: conversations.list の写し ───────────────────────


def test_client_maps_conversations_list_and_cursor() -> None:
    from teamagent.adapters.slack_channel_ingest_client import SlackChannelIngestClient

    class _Web:
        def __init__(self) -> None:
            self.kwargs: dict[str, Any] = {}

        async def conversations_list(self, **kwargs: Any) -> dict[str, Any]:
            self.kwargs = kwargs
            return {
                "channels": [
                    {"id": "C1", "name": "fp連携_a", "is_private": False, "is_member": True},
                    {"id": "G1", "name": "fp連携_b", "is_private": True, "is_member": True},
                    {"id": "", "name": "壊れ"},  # id 無しは捨てる
                ],
                "response_metadata": {"next_cursor": "dXNlcjpV"},
            }

    web = _Web()
    client = SlackChannelIngestClient(bot_token="xoxb-test", client=web)  # type: ignore[arg-type]
    page, cursor = client.list_conversations("r", types="public_channel", cursor="abc")
    assert page == [
        SlackConversation(channel_id="C1", name="fp連携_a", is_private=False, is_member=True),
        SlackConversation(channel_id="G1", name="fp連携_b", is_private=True, is_member=True),
    ]
    assert cursor == "dXNlcjpV"
    assert web.kwargs == {
        "types": "public_channel",
        "exclude_archived": True,
        "limit": 1000,
        "cursor": "abc",
    }
