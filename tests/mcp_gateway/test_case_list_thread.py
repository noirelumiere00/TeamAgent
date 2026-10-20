from types import SimpleNamespace

from teamagent.mcp_gateway.detached_jobs import destination_from_claim
from teamagent.skills._shared.long_jobs import Origin


def test_long_job_dm_uses_verified_request_thread():
    claim = SimpleNamespace(channel_id="D12345678", thread_ts=None, message_id="1792461784.695389")
    assert destination_from_claim(claim, threaded=True).thread_ts == claim.message_id
    assert destination_from_claim(claim).thread_ts is None


def test_receipt_thread_survives_same_channel_upload(tmp_path):
    path = tmp_path / "JTB_HIS.pptx"
    path.write_bytes(b"presentation")
    origin = Origin("D12345678", "1792461784.695389", "U12345678")
    try:
        origin.defer(object(), str(path), "JTB HIS", "summary", "request", channel_id="D12345678")
        assert origin._uploads[0].thread_ts == "1792461784.695389"
    finally:
        origin.discard()


def test_pptx_and_summary_keep_receipt_thread(tmp_path):
    from unittest.mock import AsyncMock

    path = tmp_path / "JTB_HIS.pptx"
    path.write_bytes(b"presentation")
    slack = SimpleNamespace(upload_file=AsyncMock(return_value=True))
    target = Origin("D12345678", "1792461784.695389", "U12345678")
    target.defer(slack, str(path), "JTB HIS", "JTBとHISの比較結果です。", "req")
    assert target.deliver()
    call = slack.upload_file.call_args
    assert call.kwargs["thread_ts"] == "1792461784.695389"
    assert call.kwargs["initial_comment"] == "JTBとHISの比較結果です。"
    assert not target.redirected
