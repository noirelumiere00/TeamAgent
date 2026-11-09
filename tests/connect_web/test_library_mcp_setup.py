"""The public guide renders honest links and a safely selectable connection URL."""

from bs4 import BeautifulSoup

from teamagent.connect_web.library_mcp_setup import setup_html


def test_setup_uses_real_settings_links_and_manual_registration_steps() -> None:
    page = BeautifulSoup(setup_html("https://connect.example.com/mcp"), "html.parser")
    assert page.html["lang"] == "ja"
    assert page.select_one('meta[name="viewport"]') is not None
    assert page.select_one("h1") is not None
    assert page.select_one('a[href="https://chatgpt.com/plugins"]') is not None
    assert page.select_one('a[href="https://claude.ai/customize/connectors"]') is not None
    assert page.select_one('a[href="https://claude.ai/admin-settings/connectors"]') is not None
    assert page.select_one('a[href="/mcp-auth/connections"]') is not None
    for link in page.select('a[target="_blank"]'):
        assert {"noopener", "noreferrer"} <= set(link["rel"])
    assert "Register automatically" in page.get_text()
    assert "本人の動画閲覧" in page.get_text()
    assert "接続を自動更新" in page.get_text()
    assert "10分" not in page.get_text()
    assert page.select("form, iframe, img, script[src], link[rel='stylesheet']") == []


def test_setup_url_is_escaped_and_copy_feedback_is_accessible() -> None:
    resource = (
        'https://example.com/mcp?x=" autofocus onfocus="alert(1)&y=</script><script>evil()</script>'
    )
    page = BeautifulSoup(setup_html(resource), "html.parser")
    field = page.select_one("#mcp-url")
    assert field["value"] == resource
    assert field.has_attr("readonly")
    assert not field.has_attr("autofocus") and not field.has_attr("onfocus")
    assert page.select_one('label[for="mcp-url"]') is not None
    assert page.select_one('#copy-url[type="button"][aria-controls="mcp-url"]') is not None
    feedback = page.select_one("#copy-status")
    assert feedback["role"] == "status" and feedback["aria-live"] == "polite"
    assert "copy-help" in field["aria-describedby"].split()
    assert "手動でコピー" in page.select_one("#copy-help").get_text()
    assert len(page.select("script")) == 1
    script = page.select_one("script").get_text()
    assert resource not in script and "evil()" not in script
    assert "urlField.select()" in script
    assert "fetch(" not in script and "innerHTML" not in script
