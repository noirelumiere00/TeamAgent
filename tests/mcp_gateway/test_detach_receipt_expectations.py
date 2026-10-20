from teamagent.mcp_gateway.detached_jobs import queued_receipt_text, receipt_text


def test_detached_receipt_does_not_promise_unmeasured_eta_or_dm_thread() -> None:
    for render in (receipt_text, queued_receipt_text):
        text = render("新宿ランチ")
        assert "この会話にお届けします" in text
        assert "このスレッド" not in text
        assert "目安" not in text
        assert "10 分" not in text
