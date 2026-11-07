"""段階公開と公開資料の相互取得。非公開では他人の token を参照しない。"""

import ast
import inspect
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from slack_sdk.errors import SlackApiError

from teamagent.adapters.oauth_token_store import InMemorySlackTokenStore, SlackOAuthToken
from teamagent.adapters.slack_client import SlackClient
from teamagent.skills._shared import slack_file_delivery as delivery
from tests.skills.knowledge_deliver.test_knowledge_deliver import _ctx

ME = "u@vectorinc.co.jp"
OTHER = "other@vectorinc.co.jp"
FID = "F0SHARED"
URL = "https://files.slack.com/files-pri/T0-F0SHARED/proposal.pdf"
PUBLIC = {"public_channel_ids": ["C0PUBLIC"]}
PRIVATE = {"private_channel_ids": ["C0PRIVATE"]}


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setenv(delivery.ENABLED_ENV, "1")
    monkeypatch.setenv(delivery.ALLOWED_EMAILS_ENV, "*")
    monkeypatch.setattr(delivery.tempfile, "mkdtemp", lambda **kw: str(tmp_path))
    store = MagicMock(
        wraps=InMemorySlackTokenStore(
            {
                OTHER: SlackOAuthToken("xoxp-other", ("files:read",)),
            }
        )
    )
    client = MagicMock()
    client.file_info = AsyncMock(return_value={"id": FID, "url_private_download": URL})
    client.download_file_guarded = AsyncMock(return_value=b"%PDF-public")
    factory = MagicMock(return_value=client)
    monkeypatch.setattr(SlackClient, "from_user_token", factory)
    return store, client, factory


async def prepare(store, metadata, *, fn=delivery.prepare_slack_files):
    return await fn(
        [(FID, "proposal.pdf")], _ctx(), slack_store=store, metadata_by_id={FID: metadata}
    )


@pytest.mark.parametrize(
    "flag,emails",
    [
        (None, "*"),
        ("0", "*"),
        ("false", "*"),
        ("1", ""),
        ("1", OTHER),
    ],
)
async def test_deliver_rollout_off_never_reads_any_token(setup, monkeypatch, flag, emails):
    store, _, factory = setup
    if flag is None:
        monkeypatch.delenv(delivery.ENABLED_ENV)
    else:
        monkeypatch.setenv(delivery.ENABLED_ENV, flag)
    monkeypatch.setenv(delivery.ALLOWED_EMAILS_ENV, emails)
    assert await prepare(store, PUBLIC) == (None, [])
    factory.assert_not_called()
    store.scopes.assert_not_called()
    store.list_file_reader_emails.assert_not_called()


@pytest.mark.parametrize("metadata", [PUBLIC, {**PUBLIC, **PRIVATE}])
async def test_deliver_no_owner_token_public_file_uses_another_reader(setup, metadata, capsys):
    store, client, factory = setup
    tmpdir, files = await prepare(store, metadata)
    assert tmpdir and len(files) == 1
    assert Path(files[0][1]).read_bytes() == b"%PDF-public"
    factory.assert_called_once_with("xoxp-other")
    store.list_file_reader_emails.assert_called_once_with()
    client.download_file_guarded.assert_awaited_once()
    assert OTHER not in capsys.readouterr().out


@pytest.mark.parametrize(
    "metadata",
    [
        PRIVATE,
        {},
        {"public_channel_ids": []},
        {"public_channel_ids": "C0PUBLIC"},
        {"public_channel_ids": ["not-a-channel"]},
        {**PUBLIC, "channel_type": "im"},
        {**PUBLIC, "channel_type": "mpim"},
        {**PUBLIC, "source_uri": "slack://D0DM/1784424000.000001"},
        {**PUBLIC, "channel_ids": ["D0DM"]},
        {**PUBLIC, "channel_name": "mpdm-user-1"},
    ],
)
async def test_deliver_no_owner_token_private_or_unknown_never_calls_other_tokens(setup, metadata):
    store, _, factory = setup
    _, files = await prepare(store, metadata)
    assert files == []
    store.list_file_reader_emails.assert_not_called()
    store.scopes.assert_called_once_with(ME)
    store.get.assert_not_called()
    factory.assert_not_called()


async def test_deliver_missing_owner_scope_public_file_falls_back(setup):
    store, _, factory = setup
    store.put(ME, SlackOAuthToken("xoxp-owner", ("search:read",)))
    _, files = await prepare(store, PUBLIC)
    assert len(files) == 1
    assert all(c.args != (ME,) for c in store.get.call_args_list)
    factory.assert_called_once_with("xoxp-other")


@pytest.mark.parametrize("error", ["file_not_found", "access_denied"])
async def test_deliver_owner_api_denied_public_metadata_tries_next_reader(
    setup, monkeypatch, error
):
    store, other, _ = setup
    store.put(ME, SlackOAuthToken("xoxp-owner", ("files:read",)))
    owner = MagicMock()
    owner.file_info = AsyncMock(side_effect=SlackApiError("denied", {"ok": False, "error": error}))
    factory = MagicMock(side_effect=lambda token: owner if token == "xoxp-owner" else other)
    monkeypatch.setattr(SlackClient, "from_user_token", factory)
    _, files = await prepare(store, PUBLIC)
    assert len(files) == 1
    assert [call.args[0] for call in factory.call_args_list] == ["xoxp-owner", "xoxp-other"]


@pytest.mark.parametrize("public", [False, True, None])
async def test_deliver_owner_info_is_public_controls_fallback_after_download_refusal(
    setup, monkeypatch, public
):
    store, other, _ = setup
    store.put(ME, SlackOAuthToken("xoxp-owner", ("files:read",)))
    owner = MagicMock()
    owner.file_info = AsyncMock(return_value={"id": FID, "url_private": URL, "is_public": public})
    owner.download_file_guarded = AsyncMock(side_effect=PermissionError("403"))
    factory = MagicMock(side_effect=lambda token: owner if token == "xoxp-owner" else other)
    monkeypatch.setattr(SlackClient, "from_user_token", factory)
    _, files = await prepare(store, {})
    assert len(files) == int(public is True)
    if public is not True:
        store.list_file_reader_emails.assert_not_called()
        assert factory.call_count == 1


async def test_deliver_current_private_info_overrides_old_public_metadata(setup, monkeypatch):
    store, client, _ = setup
    store.put(ME, SlackOAuthToken("xoxp-owner", ("files:read",)))
    client.file_info.return_value["is_public"] = False
    client.download_file_guarded.side_effect = PermissionError("403")
    _, files = await prepare(store, PUBLIC)
    assert files == []
    store.list_file_reader_emails.assert_not_called()


async def test_deliver_public_readers_are_tried_in_order_without_owner_or_bot_retry(
    setup, monkeypatch
):
    store, success, _ = setup
    store.put("a-first@vectorinc.co.jp", SlackOAuthToken("xoxp-first", ("files:read",)))
    denied = MagicMock()
    denied.file_info = AsyncMock(
        side_effect=SlackApiError("denied", {"ok": False, "error": "access_denied"})
    )
    factory = MagicMock(side_effect=lambda token: denied if token == "xoxp-first" else success)
    monkeypatch.setattr(SlackClient, "from_user_token", factory)
    _, files = await prepare(store, PUBLIC)
    assert len(files) == 1
    assert [call.args[0] for call in factory.call_args_list] == ["xoxp-first", "xoxp-other"]


async def test_deliver_private_branch_mutation_is_detected(setup):
    source = ast.parse(textwrap.dedent(inspect.getsource(delivery.prepare_slack_files)))
    guards = [
        node
        for node in ast.walk(source)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.UnaryOp)
        and isinstance(node.test.operand, ast.Call)
        and isinstance(node.test.operand.func, ast.Name)
        and node.test.operand.func.id == "_can_fallback"
        and isinstance(node.test.operand.args[1], ast.Name)
        and node.test.operand.args[1].id == "owner_info"
    ]
    assert len(guards) == 1
    guards[0].body = [ast.Pass()]
    namespace = dict(vars(delivery))
    exec(compile(ast.fix_missing_locations(source), "<private-guard-mutation>", "exec"), namespace)
    store, _, _ = setup
    await prepare(store, PRIVATE, fn=namespace["prepare_slack_files"])
    with pytest.raises(AssertionError):
        store.list_file_reader_emails.assert_not_called()


@pytest.mark.parametrize("allowed", [ME.upper(), f"unused@vectorinc.co.jp, {ME.upper()} "])
async def test_deliver_allowlist_accepts_normalized_email(setup, monkeypatch, allowed):
    monkeypatch.setenv(delivery.ALLOWED_EMAILS_ENV, allowed)
    store, _, _ = setup
    _, files = await prepare(store, PUBLIC)
    assert len(files) == 1


async def test_deliver_alternate_current_private_info_never_downloads(setup):
    store, client, _ = setup
    client.file_info.return_value["is_public"] = False
    _, files = await prepare(store, PUBLIC)
    assert files == []
    client.download_file_guarded.assert_not_awaited()
