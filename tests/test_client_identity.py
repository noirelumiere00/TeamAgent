from teamagent.client_identity import client_identity_key


def test_client_identity_absorbs_safe_company_name_variants() -> None:
    expected = client_identity_key("ポート株式会社")
    assert expected
    assert client_identity_key("株式会社 ポート 御中") == expected
    assert client_identity_key("ポート（株）") == expected
    assert client_identity_key("ＰＯＲＴ株式会社") == client_identity_key("port")


def test_client_identity_does_not_remove_honorific_inside_product_name() -> None:
    assert client_identity_key("熱さまシート") == "熱さまシート"
    assert client_identity_key("熱さまシート") != client_identity_key("熱シート")


def test_client_identity_does_not_use_substring_matching() -> None:
    port = client_identity_key("ポート株式会社")
    assert client_identity_key("レポート") != port
    assert client_identity_key("株主パスポート") != port
    assert client_identity_key("ポートフォリオ") != port
