"""scripts/build_app_html.py の運用ガードのテスト（実 Vault 0・書き込みは tmp_path のみ）。

契約（repo 版で追加した4点。フィルタ/HTML 生成ロジック自体は元スクリプト同一なので対象外）:
- 正常系: ダミー Vault + サイドカーから HTML を生成し、<out>.stats.json と
  フッタ焼き込み（更新: YYYY-MM-DD JST・取引先N・資料M）が入る
- fail-loud: Vault 不在 / サイドカー欠落 / clients==0 / docs==0 は exit 1
  （silent fallback・空 HTML の配信を作らない）
- サニティゲート: 取引先数/資料数/バイト数のいずれかが前回比 20% 超減なら exit 1 で
  既存 out を保持。--allow-shrink で明示的に通過し統計基準がリセットされる
- exclude_stems.json のフィルタ配線: 除外 stem の資料が payload に載らない
- PII 決定論除外: 請求書系 stem（正規化後に「請求」を含む）はサイドカー列挙なしで除外
  （個人名入り stem を repo に平文で持たない）
- タグ第1弾: 媒体/動画形式/形式/横断（資料）・温度感/宿題（クライアント）の決定論判定と
  payload/JS 配線（グラフ用 _ctags/_dtags には載せない）＋テーブル「次アクション」列
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from datetime import date
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "build_app_html", _ROOT / "scripts" / "build_app_html.py"
)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
sys.modules["build_app_html"] = _mod
_spec.loader.exec_module(_mod)


# ---------------- fixtures ----------------


@pytest.fixture()
def sidecars(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """最小の有効サイドカー一式を tmp に用意し SIDECAR_DIR を差し替える。"""
    d = tmp_path / "filters"
    d.mkdir()
    (d / "exclude_stems.json").write_text(
        json.dumps(["除外対象資料"], ensure_ascii=False), encoding="utf-8"
    )
    (d / "dedup_drop_map.json").write_text(
        json.dumps({"drop": {}, "keep_canonical": []}, ensure_ascii=False), encoding="utf-8"
    )
    (d / "weird_rename_high.json").write_text("{}", encoding="utf-8")
    (d / "inter-var.b64").write_text("QUFBQQ==", encoding="utf-8")  # ダミー base64
    monkeypatch.setattr(_mod, "SIDECAR_DIR", d)
    return d


def _write_client(vault: Path, name: str) -> None:
    (vault / "clients" / f"{name}.md").write_text(
        f'---\nclient: "{name}"\nindustry: "エネルギー"\ndeal_phase: "提案"\n'
        f'bant_score: "B（前向き）"\nfb_count: "2"\ndoc_count: "3"\n---\n\n'
        f"# {name}\n\n### 商談メモ --1750000000\n前向き。\n\n## 関連資料\n- [[docs/提案書A]]\n",
        encoding="utf-8",
    )


def _write_doc(vault: Path, stem: str, client: str = "出光興産") -> None:
    (vault / "docs" / f"{stem}.md").write_text(
        f'---\ntitle: "{stem}"\nclient: "{client}"\nindustry: "エネルギー"\n'
        f'doc_type: "提案書"\nsolution: "動画広告"\nmodified_at: "2026-06-01"\n---\n\n'
        f"> {stem} の抜粋\n\n[[clients/{client}]]\n",
        encoding="utf-8",
    )


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    """取引先1・資料3（+除外対象1）の最小ダミー Vault。"""
    v = tmp_path / "vault"
    (v / "clients").mkdir(parents=True)
    (v / "docs").mkdir()
    _write_client(v, "出光興産")
    for stem in ("提案書A", "提案書B", "議事録C"):
        _write_doc(v, stem)
    _write_doc(v, "除外対象資料")  # exclude_stems.json に載っている → 表示されない
    return v


def _run(vault: Path, out: Path, *extra: str) -> int:
    rc = _mod.main(["--vault", str(vault), "--out", str(out), *extra])
    assert isinstance(rc, int)
    return rc


# ---------------- 正常系 ----------------


def test_build_success_writes_html_stats_and_stamp(
    sidecars: Path, vault: Path, tmp_path: Path
) -> None:
    out = tmp_path / "app.html"
    assert _run(vault, out) == 0

    html = out.read_text(encoding="utf-8")
    assert "出光興産" in html
    # フッタ焼き込み（statusbar 内・プレースホルダは残っていない）
    assert "__BUILDSTAMP__" not in html
    assert "更新: " in html and "・取引先1・資料3" in html
    # フォント埋め込み（fallback 廃止で必ず入る）
    assert "QUFBQQ==" in html

    stats = json.loads(Path(str(out) + ".stats.json").read_text(encoding="utf-8"))
    assert stats["clients"] == 1
    assert stats["docs"] == 3
    assert stats["bytes"] == len(out.read_bytes())


def test_excluded_stem_not_in_payload(sidecars: Path, vault: Path, tmp_path: Path) -> None:
    out = tmp_path / "app.html"
    _run(vault, out)
    assert "除外対象資料" not in out.read_text(encoding="utf-8")


def test_seikyusho_stem_excluded_without_sidecar_listing(
    sidecars: Path, vault: Path, tmp_path: Path
) -> None:
    """請求書系 stem は exclude_stems.json に列挙せず決定論ルールで除外される（PII regression）。

    氏名は架空。サイドカー fixture（"除外対象資料" のみ）に載せていないことが本質:
    列挙ではなく「正規化後 stem に『請求』を含む」ルールが効いていることを検証する。
    """
    stem = "99_山田 花子様_25年1月分_請求書.pdf"
    _write_doc(vault, stem)
    out = tmp_path / "app.html"
    _run(vault, out)
    html = out.read_text(encoding="utf-8")
    assert "請求書" not in html
    assert "山田 花子" not in html
    # 除外されるので資料数は既定の 3 のまま（stats にも計上されない）
    stats = json.loads(Path(str(out) + ".stats.json").read_text(encoding="utf-8"))
    assert stats["docs"] == 3


# ---------------- fail-loud ----------------


def test_vault_missing_exits_1(
    sidecars: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as ei:
        _mod.main(["--vault", str(tmp_path / "no_vault"), "--out", str(tmp_path / "o.html")])
    assert ei.value.code == 1
    assert "Vault が見つかりません" in capsys.readouterr().err


def test_sidecar_missing_exits_1(
    sidecars: Path, vault: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (sidecars / "exclude_stems.json").unlink()
    with pytest.raises(SystemExit) as ei:
        _run(vault, tmp_path / "o.html")
    assert ei.value.code == 1
    assert "サイドカー欠落: exclude_stems.json" in capsys.readouterr().err


def test_empty_clients_exits_1(
    sidecars: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    v = tmp_path / "vault"
    (v / "clients").mkdir(parents=True)
    (v / "docs").mkdir()
    _write_doc(v, "提案書A")
    with pytest.raises(SystemExit) as ei:
        _run(v, tmp_path / "o.html")
    assert ei.value.code == 1
    assert "clients==0" in capsys.readouterr().err


def test_empty_docs_exits_1(
    sidecars: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    v = tmp_path / "vault"
    (v / "clients").mkdir(parents=True)
    (v / "docs").mkdir()
    _write_client(v, "出光興産")
    with pytest.raises(SystemExit) as ei:
        _run(v, tmp_path / "o.html")
    assert ei.value.code == 1
    assert "docs==0" in capsys.readouterr().err


# ---------------- サニティゲート ----------------


def test_shrink_over_20pct_exits_1_and_keeps_previous_out(
    sidecars: Path, vault: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "app.html"
    _run(vault, out)
    first_bytes = out.read_bytes()

    # 資料 3 → 1（-66%）に痩せた Vault で再生成 → ゲートが止める
    (vault / "docs" / "提案書B.md").unlink()
    (vault / "docs" / "議事録C.md").unlink()
    with pytest.raises(SystemExit) as ei:
        _run(vault, out)
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "サニティゲート" in err and "--allow-shrink" in err
    # 既存 out・統計は上書きされていない
    assert out.read_bytes() == first_bytes
    assert json.loads(Path(str(out) + ".stats.json").read_text(encoding="utf-8"))["docs"] == 3


def test_shrink_passes_with_allow_shrink_and_resets_baseline(
    sidecars: Path, vault: Path, tmp_path: Path
) -> None:
    out = tmp_path / "app.html"
    _run(vault, out)
    (vault / "docs" / "提案書B.md").unlink()
    (vault / "docs" / "議事録C.md").unlink()
    assert _run(vault, out, "--allow-shrink") == 0
    # 基準がリセットされ、次回は縮小扱いにならない
    stats = json.loads(Path(str(out) + ".stats.json").read_text(encoding="utf-8"))
    assert stats["docs"] == 1
    assert _run(vault, out) == 0


def test_no_shrink_passes_without_flag(sidecars: Path, vault: Path, tmp_path: Path) -> None:
    out = tmp_path / "app.html"
    _run(vault, out)
    assert _run(vault, out) == 0  # 同一 Vault の再生成はゲートを通る


def test_corrupt_stats_exits_1(
    sidecars: Path, vault: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "app.html"
    Path(str(out) + ".stats.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(SystemExit) as ei:
        _run(vault, out)
    assert ei.value.code == 1
    assert "前回統計" in capsys.readouterr().err


# ---------------- parse_fb_events（施策タイムライン） ----------------


def test_parse_fb_date_from_quote_line_and_fields() -> None:
    """Slack 由来 FB: `> [YYYY-MM-DD HH:MM]` 行から日付、全フィールドを抽出。"""
    body = (
        "## 営業FB時系列（新しい順）\n\n"
        "### ---- #proj-ショート動画_営業フィードバック情報 1778489156.830189\n\n"
        "- フェーズ: ケイパ\n"
        "- BANT: C（検討）\n"
        "- チャネル: 代理店\n"
        "- ポジ反応: ・企業ブランディングの相談は多い。\n"
        "- ネガ反応: ・PIVOTの問い合わせは多い。\n"
        "- 次アクション: 全社に展開\n"
        "- 提案メニュー: UGC（TTO、切り抜きなど）\n\n"
        "> [2026-05-11 08:45] <B0AK>: 本文抜粋\n\n"
        "[出典](slack://C0A/1778489156.830189)\n\n"
        "## 関連資料\n\n- [[docs/x]]\n"
    )
    evs = _mod.parse_fb_events(body)
    assert len(evs) == 1
    ev = evs[0]
    assert ev["d"] == "2026-05-11"  # > [..] 行が epoch より優先
    assert ev["src"] == "Slack"
    assert ev["ph"] == "ケイパ"
    assert ev["bant"] == "C（検討）"
    assert ev["menu"] == "UGC（TTO、切り抜きなど）"
    assert ev["pos"] == "・企業ブランディングの相談は多い。"
    assert ev["neg"] == "・PIVOTの問い合わせは多い。"
    assert ev["next"] == "全社に展開"


def test_parse_fb_epoch_converts_to_jst_date() -> None:
    """epoch → UTC+9 で日付化(UTC のままだと前日になる境界 epoch で検証)。"""
    # 1779120000 = 2026-05-19 01:00 JST（UTC では 2026-05-18 16:00）
    body = "### ---- #proj-ch 1779120000.123456\n\n- ポジ反応: よい\n"
    evs = _mod.parse_fb_events(body)
    assert evs[0]["d"] == "2026-05-19"
    # 仕様書の例: 1779101519.347119 → 2026-05-18
    evs2 = _mod.parse_fb_events("### ---- #proj-ch 1779101519.347119\n\n- ポジ反応: 別内容\n")
    assert evs2[0]["d"] == "2026-05-18"


def test_parse_fb_dedup_keeps_dated_entry() -> None:
    """Slack/フォーム二重登録: 正規化 (ポジ+ネガ) 一致 → 日付を持つ方だけ残る。"""
    body = (
        "### ---- ショート動画営業 FB - フォーム回答 - フォーム回答 1 - row 9\n\n"
        "- フェーズ: ケイパ\n"
        "- ポジ反応: ・相談は 多い。\n"
        "- ネガ反応: ・質問　あり\n\n"
        "### ---- #proj-ch 1778489156.830189\n\n"
        "- フェーズ: ケイパ\n"
        "- ポジ反応: ・相談は多い。\n"
        "- ネガ反応: ・質問あり\n"
    )
    evs = _mod.parse_fb_events(body)
    assert len(evs) == 1  # 空白/全角空白差は正規化で吸収され dedup される
    assert evs[0]["src"] == "Slack"
    assert evs[0]["d"] == "2026-05-11"


def test_parse_fb_form_src_and_dateless() -> None:
    body = (
        "### ---- ショート動画営業 FB - フォーム回答 - フォーム回答 1 - row 3\n\n"
        "- フェーズ: ヒアリング\n- ポジ反応: 保証型がよい\n"
    )
    evs = _mod.parse_fb_events(body)
    assert evs[0]["src"] == "フォーム"
    assert evs[0]["d"] == ""  # フォーム回答は日付なしを許容


def test_parse_fb_broken_heading_and_missing_fields_fail_open() -> None:
    """壊れた見出し・欠損フィールドは空文字で許容し、非FBのh3配下は拾わない。"""
    body = (
        "### ---- 壊れた見出し（epochもrowも無い）\n\n"
        "自由記述のみでフィールド行なし\n\n"
        "### 通常のh3見出し（FBではない）\n\n"
        "- フェーズ: 誤検知しない\n"
    )
    evs = _mod.parse_fb_events(body)
    assert len(evs) == 1
    ev = evs[0]
    assert ev["d"] == "" and ev["ph"] == "" and ev["pos"] == "" and ev["next"] == ""
    assert ev["src"] == "Slack"  # 既定
    # 非入力・破損入力でも例外を漏らさない
    assert _mod.parse_fb_events("") == []
    assert _mod.parse_fb_events("FBセクションなしの本文") == []


def test_parse_fb_sorted_desc_dateless_last_and_capped_at_30() -> None:
    blocks = []
    for i in range(33):
        blocks.append(
            f"### ---- #proj-ch 通番{i}\n\n- ポジ反応: 個別内容{i}\n\n"
            f"> [2026-01-{(i % 28) + 1:02d} 10:00] x\n"
        )
    evs = _mod.parse_fb_events("\n".join(blocks))
    assert len(evs) == 30  # cap
    dates = [e["d"] for e in evs]
    assert dates == sorted(dates, reverse=True)  # 日付降順

    # 日付なしは末尾
    evs2 = _mod.parse_fb_events(
        "### ---- 日付なし row 1\n\n- ポジ反応: あ\n\n"
        "### ---- #ch 1779101519.347119\n\n- ポジ反応: い\n"
    )
    assert [e["d"] for e in evs2] == ["2026-05-18", ""]


def test_parse_fb_field_truncation() -> None:
    body = (
        "### ---- #ch 1779101519\n\n"
        "- ポジ反応: " + "あ" * 500 + "\n"
        "- ネガ反応: " + "え" * 500 + "\n"
        "- 提案メニュー: " + "い" * 100 + "\n"
        "- 次アクション: " + "う" * 200 + "\n"
    )
    ev = _mod.parse_fb_events(body)[0]
    # pos/neg=120 は実 Vault 計測（+400KB 超過）で 160 から落とした値（本体 FB_FIELD_MAP と対）
    assert len(ev["pos"]) == 120
    assert len(ev["neg"]) == 120
    assert len(ev["menu"]) == 60
    assert len(ev["next"]) == 120


# ---------------- 施策タイムライン: payload / DOM 配線 ----------------


def test_timeline_payload_and_dom_in_html(sidecars: Path, vault: Path, tmp_path: Path) -> None:
    """FB 持ちクライアントを追加して build → tl/lastfb/cnorm が payload に載り DOM/JS が入る。"""
    (vault / "clients" / "帝人.md").write_text(
        '---\nclient: "帝人"\nindustry: "メーカー"\ndeal_phase: "ヒアリング"\n'
        'bant_score: "B（前向き）"\nfb_count: 1\ndoc_count: 0\n---\n\n# 帝人\n\n'
        "## 営業FB時系列（新しい順）\n\n"
        "### ---- #proj-ショート動画_営業フィードバック情報 1779101519.347119\n\n"
        "- フェーズ: ヒアリング\n- BANT: B（前向き）\n- ポジ反応: ・保証型がよい\n"
        "- 次アクション: 与件獲得\n- 提案メニュー: UGC\n\n"
        "## 関連資料\n",
        encoding="utf-8",
    )
    out = tmp_path / "app.html"
    _run(vault, out)
    html = out.read_text(encoding="utf-8")
    # DOM/JS: タイムラインセクション・トグル・最終接点列
    assert "tlwrap" in html and "施策タイムライン" in html
    assert "tlmorebtn" in html
    assert "最終接点" in html
    # payload: FB イベント JSON（日付は epoch → JST）と lastfb / cnorm
    assert '"src": "Slack"' in html
    assert '"d": "2026-05-18"' in html
    assert '"lastfb": "2026-05-18"' in html
    assert '"pos": "・保証型がよい"' in html
    assert '"cnorm": "帝人"' in html


def test_timeline_merged_on_name_dedup(sidecars: Path, vault: Path, tmp_path: Path) -> None:
    """名寄せ統合時: 表記ゆれ 2 カルテの FB が結合される（正本1つに tl 2件・lastfb=最新日）。"""
    for fname, cname, epoch, pos in (
        ("帝人.md", "帝人", "1779101519.347119", "・保証型がよい"),
        ("帝人様.md", "帝人様", "1779120000.000000", "・単価感が抑えられる"),
    ):
        (vault / "clients" / fname).write_text(
            f'---\nclient: "{cname}"\nindustry: "メーカー"\ndeal_phase: ""\n'
            f'bant_score: ""\nfb_count: 1\ndoc_count: 0\n---\n\n# {cname}\n\n'
            "## 営業FB時系列（新しい順）\n\n"
            f"### ---- #proj-ch {epoch}\n\n- ポジ反応: {pos}\n\n"
            "## 関連資料\n",
            encoding="utf-8",
        )
    out = tmp_path / "app.html"
    _run(vault, out)
    html = out.read_text(encoding="utf-8")
    # 名寄せで 1 クライアントに統合され、tl は両カルテの FB を含む
    stats = json.loads(Path(str(out) + ".stats.json").read_text(encoding="utf-8"))
    assert stats["clients"] == 2  # 出光興産 + 帝人（帝人様は統合）
    assert '"pos": "・保証型がよい"' in html
    assert '"pos": "・単価感が抑えられる"' in html
    # 最新日（2026-05-19 = 1779120000 JST）が lastfb
    assert '"lastfb": "2026-05-19"' in html


# ---------------- タグ拡充（最終接点/更新/情報源） ----------------


def test_doc_src_parsed_from_shutten_line(sidecars: Path, vault: Path, tmp_path: Path) -> None:
    """出典行のトークンが src に写像される（gdrive→Drive / gsheets→フォーム）。"""
    p = vault / "docs" / "提案書A.md"
    p.write_text(
        p.read_text() + "\n- 出典: [gdrive](https://drive.google.com/file/d/x/view)\n",
        encoding="utf-8",
    )
    (vault / "docs" / "フォーム回答X.md").write_text(
        '---\ntitle: "フォーム回答X"\nclient: "出光興産"\nindustry: ""\n'
        'doc_type: "議事録"\nsolution: ""\nmodified_at: "2026-07-01"\n---\n\n'
        "> 抜粋\n\n- 出典: [gsheets](https://docs.google.com/spreadsheets/d/x)\n",
        encoding="utf-8",
    )
    out = tmp_path / "o.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    assert '"src": "Drive"' in html
    assert '"src": "フォーム"' in html


def test_client_last_is_max_of_lastfb_and_doc_modified(
    sidecars: Path, vault: Path, tmp_path: Path
) -> None:
    """最終接点 = max(最終FB日, 関連資料の最新 modified)。FB日付なしなら資料側が採用される。"""
    out = tmp_path / "o.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    assert '"last": "2026-06-01"' in html


def test_freshness_tag_wiring_in_js(sidecars: Path, vault: Path, tmp_path: Path) -> None:
    """ageBucket と 最終接点/更新/情報源 タグの JS 配線が生成 HTML に存在する。"""
    out = tmp_path / "o.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    for token in ("function ageBucket", "最終接点/", '"更新/"+a', '"情報源/"+d.src'):
        assert token in html, token


# ---------------- タグ第1弾: 媒体/（決定論 regex・誤爆対策） ----------------


def test_media_tags_positive_cases() -> None:
    """各媒体の正常系（TikTok ほか）と複数付与・MEDIA_RES 定義順。"""
    assert _mod.media_tags("TikTok縦型動画のご提案") == ["TikTok"]
    assert _mod.media_tags("YouTube・Instagram同時配信企画") == ["YouTube", "Instagram"]
    assert _mod.media_tags("インスタ運用レポート") == ["Instagram"]
    assert _mod.media_tags("LINE公式アカウント設計") == ["LINE"]
    assert _mod.media_tags("Facebook広告メニュー") == ["Facebook"]
    assert _mod.media_tags("テレビCM連動・TVer配信") == ["テレビ"]
    assert _mod.media_tags("駅サイネージ×OOH展開") == ["OOH"]
    assert _mod.media_tags("") == []


def test_media_tags_x_variants_and_guards() -> None:
    """X: 「X（旧Twitter）」「Twitter」表記は付き、DX・小文字コラボ表記 x は付かない。"""
    assert _mod.media_tags("X（旧Twitter）キャンペーン") == ["X"]
    assert _mod.media_tags("Twitterトレンド分析") == ["X"]
    assert _mod.media_tags("Ｘ（旧Ｔｗｉｔｔｅｒ）施策") == ["X"]  # 全角も NFKC で吸収
    assert _mod.media_tags("X運用のご提案") == ["X"]  # 英数字境界の単独 X
    assert _mod.media_tags("DX推進のご提案") == []
    assert _mod.media_tags("ブランドA x ブランドB コラボ") == []  # コラボ表記の小文字 x
    assert _mod.media_tags("https://x.com/example の投稿") == []  # URL の小文字 x


def test_media_tags_line_not_confused_by_english_words() -> None:
    """LINE: online/deadline/GUIDELINE/カタカナのライン系は誤爆しない。"""
    for text in (
        "online配信のご案内",
        "提出deadlineの共有",
        "ブランドGUIDELINE策定",
        "オンラインセミナー実施",
        "デッドライン注意",
        "商品ラインナップ一覧",
    ):
        assert "LINE" not in _mod.media_tags(text), text
    assert _mod.media_tags("LINEヤフー共同企画") == ["LINE"]  # 直後がカタカナなら付く


# ---------------- タグ第1弾: 動画形式/・形式/ ----------------


def test_video_format_tags() -> None:
    assert _mod.video_format_tags("縦型ショート動画企画") == ["ショート"]
    assert _mod.video_format_tags("切り抜き二次利用について") == ["切り抜き"]
    assert _mod.video_format_tags("ライブコマース実施要領") == ["ライブ配信"]
    assert _mod.video_format_tags("ライブ配信＋長尺アーカイブ") == ["ライブ配信", "長尺"]
    assert _mod.video_format_tags("ショートステイのご案内") == []  # 「ショート」単体では付けない


def test_file_format_tag_from_stem_suffix() -> None:
    """stem 末尾の拡張子表記のみ判定（大文字小文字無視・拡張子なし/途中出現は付けない）。"""
    assert _mod.file_format_tag("新提案書FMT.pptx") == "PPTX"
    assert _mod.file_format_tag("実績レポート.PDF") == "PDF"
    assert _mod.file_format_tag("進行管理表.xlsx") == "Excel"
    assert _mod.file_format_tag("覚書ドラフト.docx") == "Word"
    assert _mod.file_format_tag("議事録（拡張子なし）") == ""
    assert _mod.file_format_tag("pptx資料まとめ") == ""
    assert _mod.file_format_tag("") == ""


# ---------------- タグ第1弾: 温度感/・宿題/（クライアント） ----------------


def _tl1(pos: str, neg: str, next_: str = "", d: str = "2026-06-01") -> list[dict]:
    """温度感/宿題テスト用の最小 tl（parse_fb_events 出力と同形・先頭が最新）。"""
    return [
        {
            "d": d,
            "src": "Slack",
            "ph": "",
            "bant": "",
            "menu": "",
            "pos": pos,
            "neg": neg,
            "next": next_,
        }
    ]


def test_temperature_tag_four_branches_and_nashi() -> None:
    """4分岐: 高（neg空/特になし系）／ポジ優勢／ネガ優勢／拮抗。tl 空はタグなし。"""
    assert _mod.temperature_tag(_tl1("よい", "")) == "高"
    assert _mod.temperature_tag(_tl1("よい", "特になし")) == "高"
    assert _mod.temperature_tag(_tl1("よい", "・なし")) == "高"
    assert _mod.temperature_tag(_tl1("よい", "-")) == "高"
    assert _mod.temperature_tag(_tl1("あ" * 10, "い" * 5)) == "ポジ優勢"
    assert _mod.temperature_tag(_tl1("あ" * 4, "い" * 8)) == "ネガ優勢"
    assert _mod.temperature_tag(_tl1("あ" * 6, "い" * 5)) == "拮抗"
    assert _mod.temperature_tag(_tl1("", "予算が合わない")) == "ネガ優勢"  # pos 空は neg 側
    assert _mod.temperature_tag([]) == ""


def test_temperature_tag_uses_latest_event_only() -> None:
    """判定は先頭（最新）イベントのみ。過去の「特になし」に引きずられない。"""
    tl = _tl1("あ" * 3, "い" * 8, d="2026-06-01") + _tl1("よい", "特になし", d="2026-05-01")
    assert _mod.temperature_tag(tl) == "ネガ優勢"


def test_next_action_first_40_chars_of_latest_event() -> None:
    tl = _tl1("", "", next_="あ" * 100) + _tl1("", "", next_="古い方", d="2026-05-01")
    assert _mod.next_action(tl) == "あ" * 40
    assert _mod.next_action([]) == ""
    assert _mod.next_action(_tl1("", "", next_="")) == ""


def test_homework_flag_gating() -> None:
    """次アクションあり×最終接点が新しい→付かない。31日超過去 or 日付なし→付く。"""
    today = date(2026, 7, 11)
    assert _mod.homework_flag("与件獲得", "2026-07-01", today) is False  # 10日前 → 追えている
    assert _mod.homework_flag("与件獲得", "2026-06-10", today) is False  # ちょうど31日 → 付けない
    assert _mod.homework_flag("与件獲得", "2026-06-09", today) is True  # 32日前 → 放置
    assert _mod.homework_flag("与件獲得", "", today) is True  # 日付なし
    assert _mod.homework_flag("与件獲得", "invalid-date", today) is True  # 不正日付は日付なし扱い
    assert _mod.homework_flag("", "2026-01-01", today) is False  # 次アクションなしなら常に付けない


# ---------------- タグ第1弾: payload / JS 配線（統合） ----------------


def _write_client_links(vault: Path, name: str, doc_stems: list[str]) -> None:
    """FB なし・関連資料リンクだけ持つ最小クライアント（横断/ カウント用）。"""
    links = "\n".join(f"- [[docs/{s}]]" for s in doc_stems)
    (vault / "clients" / f"{name}.md").write_text(
        f'---\nclient: "{name}"\nindustry: "メーカー"\ndeal_phase: ""\nbant_score: ""\n'
        f"fb_count: 0\ndoc_count: {len(doc_stems)}\n---\n\n# {name}\n\n## 関連資料\n{links}\n",
        encoding="utf-8",
    )


def _doc_field(html: str, stem: str, key: str) -> str | None:
    """payload JSON から stem の doc オブジェクト内フィールド値を取り出す（テスト用）。"""
    m = re.search(f'"stem": "{re.escape(stem)}".*?"{key}": "([^"]*)"', html)
    return m.group(1) if m else None


def test_cross_reference_tag_counts_distinct_clients(
    sidecars: Path, vault: Path, tmp_path: Path
) -> None:
    """横断/: 2社→2社タグ・3社→3社以上・1社のみは付かない（wikilink 逆引き）。"""
    # fixture の出光興産は 提案書A のみ参照。帝人・花王を追加して参照網を作る
    _write_client_links(vault, "帝人", ["提案書A", "議事録C"])
    _write_client_links(vault, "花王", ["提案書A", "議事録C"])
    out = tmp_path / "app.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    assert _doc_field(html, "提案書A", "xc") == "3社以上"  # 出光興産+帝人+花王
    assert _doc_field(html, "議事録C", "xc") == "2社"  # 帝人+花王
    assert _doc_field(html, "提案書B", "xc") == ""  # 誰からもリンクされない → 付かない


def test_doc_tag_fields_in_payload(sidecars: Path, vault: Path, tmp_path: Path) -> None:
    """媒体/動画形式/形式が doc payload に載る（title+excerpt 判定・stem 拡張子判定）。"""
    _write_doc(vault, "TikTok縦型企画案.pptx")
    out = tmp_path / "app.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    assert '"media": ["TikTok"]' in html
    assert '"vfmt": ["ショート"]' in html
    assert _doc_field(html, "TikTok縦型企画案.pptx", "fmt") == "PPTX"
    assert _doc_field(html, "提案書A", "fmt") == ""  # 拡張子なし stem はタグなし


def test_client_tag_fields_and_js_wiring(sidecars: Path, vault: Path, tmp_path: Path) -> None:
    """温度感/宿題/次アクションが client payload に載り、JS 配線・テーブル列が生成される。"""
    (vault / "clients" / "帝人.md").write_text(
        '---\nclient: "帝人"\nindustry: "メーカー"\ndeal_phase: "ヒアリング"\n'
        'bant_score: "B（前向き）"\nfb_count: 1\ndoc_count: 0\n---\n\n# 帝人\n\n'
        "## 営業FB時系列（新しい順）\n\n"
        "### ---- #proj-ショート動画_営業フィードバック情報 1779101519.347119\n\n"
        "- フェーズ: ヒアリング\n- ポジ反応: ・保証型がよい\n- ネガ反応: 特になし\n"
        "- 次アクション: 与件獲得に向けた提案書再提出\n\n"
        "## 関連資料\n",
        encoding="utf-8",
    )
    out = tmp_path / "app.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    # 帝人: 最終接点 2026-05-18（31日超過去）× 次アクションあり → temp/nx/hw
    assert '"temp": "高"' in html
    assert '"nx": "与件獲得に向けた提案書再提出"' in html
    assert '"hw": 1' in html
    # 出光興産: FB時系列見出しなし（tl 空）→ 温度感なし・宿題なし
    assert re.search(r'"stem": "出光興産".*?"temp": ""', html)
    assert re.search(r'"stem": "出光興産".*?"hw": 0', html)
    # JS 配線: 新タグは docTags/clientTags のみ（グラフ用 _ctags/_dtags には載せない）
    for token in (
        '"温度感/"+c.temp',
        '"宿題/あり"',
        '"媒体/"+m',
        '"動画形式/"+v',
        '"形式/"+d.fmt',
        '"横断/"+d.xc',
    ):
        assert token in html, token
    # テーブル「次アクション」列（TCOLS + 行セル）
    assert '["nx","次アクション",c=>c.nx||"",0]' in html
    assert "c.nx?esc(c.nx)" in html


def test_homework_tag_not_set_when_last_contact_fresh(
    sidecars: Path, vault: Path, tmp_path: Path
) -> None:
    """宿題ゲーティング: 次アクションありでも最終接点が新しければ hw=0。"""
    fresh = date.today().strftime("%Y-%m-%d")
    (vault / "clients" / "花王.md").write_text(
        '---\nclient: "花王"\nindustry: "日用品"\ndeal_phase: ""\nbant_score: ""\n'
        "fb_count: 1\ndoc_count: 1\n---\n\n# 花王\n\n"
        "## 営業FB時系列（新しい順）\n\n"
        "### ---- #proj-ch 1779101519.347119\n\n"
        "- ポジ反応: ・よい\n- 次アクション: 見積提出\n\n"
        "## 関連資料\n- [[docs/花王向け資料]]\n",
        encoding="utf-8",
    )
    # 今日更新の関連資料 → last が今日になり 31 日以内 → 宿題は付かない
    (vault / "docs" / "花王向け資料.md").write_text(
        f'---\ntitle: "花王向け資料"\nclient: "花王"\nindustry: "日用品"\n'
        f'doc_type: "提案書"\nsolution: ""\nmodified_at: "{fresh}"\n---\n\n> 抜粋\n',
        encoding="utf-8",
    )
    out = tmp_path / "app.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    m = re.search(r'"stem": "花王".*?"nx": "([^"]*)".*?"hw": (\d)', html)
    assert m is not None
    assert m.group(1) == "見積提出"
    assert m.group(2) == "0"


# ---------------- 管理用レポートの既定非搭載 ----------------


def test_reports_excluded_by_default(sidecars: Path, vault: Path, tmp_path: Path) -> None:
    """_reports/ が実在しても既定では payload に載らない（16名向け画面から管理物を排除）。"""
    rdir = vault / "_reports"
    rdir.mkdir()
    (rdir / "followup_gaps.md").write_text("# レポート\n\n中身", encoding="utf-8")
    out = tmp_path / "o.html"
    assert _run(vault, out) == 0
    html = out.read_text(encoding="utf-8")
    assert '"reports": []' in html
    # payload に report エントリが無いこと（JS ソース内の openReport("...") リテラルは残ってよい）
    assert '"stem": "followup_gaps"' not in html


def test_reports_included_with_flag(sidecars: Path, vault: Path, tmp_path: Path) -> None:
    """--include-reports 指定時のみ従来どおり搭載（管理セッション用）。"""
    rdir = vault / "_reports"
    rdir.mkdir()
    (rdir / "followup_gaps.md").write_text("# レポート\n\n中身", encoding="utf-8")
    out = tmp_path / "o.html"
    assert _run(vault, out, "--include-reports") == 0
    html = out.read_text(encoding="utf-8")
    assert '"stem": "followup_gaps"' in html


# ---------------- タグUX改善（審査済み6機能）: 生成 HTML への配線 ----------------


@pytest.fixture()
def ux_html(sidecars: Path, vault: Path, tmp_path: Path) -> str:
    """タグUX検証用に1回だけ build した HTML（機能別テストで共有）。"""
    out = tmp_path / "ux.html"
    assert _run(vault, out) == 0
    return out.read_text(encoding="utf-8")


def test_ux1_chip_foundation_and_runquery(ux_html: str) -> None:
    """機能1: CATMETA 14系統・chipHtml 部品・runQuery 集約・ダークテーマ hover 修正。"""
    html = ux_html
    # 系統色辞書: 14系統すべて定義（色は既存パレット hex）
    m = re.search(r"const CATMETA=\{([^}]*)\}", html)
    assert m is not None
    for cat in (
        "宿題",
        "温度感",
        "フェーズ",
        "BANT",
        "業種",
        "最終接点",
        "資料種別",
        "施策",
        "媒体",
        "動画形式",
        "形式",
        "横断",
        "更新",
        "情報源",
    ):
        assert f'"{cat}":"#' in m.group(1), cat
    # チップ部品: 文字色はCSS変数のみ（--muted/--text）・7pxドット・hoverで系統色枠
    assert "function chipHtml(tag,mode)" in html
    assert ".tagchip .tck{color:var(--muted)" in html
    assert ".tagchip .tcv{color:var(--text)" in html
    assert ".tcdot{display:inline-block;width:7px;height:7px" in html
    assert ".tagchip:hover{border-color:var(--cc2,var(--accent))}" in html
    # runQuery 集約: 4重複コードが単一路に（タグペイン/本文タグ/右パネル・グラフ=tagJump）
    assert "function runQuery(q)" in html
    assert "function tagJump(t){runQuery(tagQ(t));}" in html
    assert "onclick:()=>runQuery(tagQ(full))" in html
    assert (
        '.tg,.ntag,.note-tags .tagchip").forEach(el=>el.onclick=()=>runQuery(tagQ(el.dataset.tag)))'
        in html
    )
    # 空白入りタグ値は tag:"値" で発行（引用符対応）
    assert "function tagQ(t){return /\\s/.test(t)?'tag:\"'+t+'\"':\"tag:\"+t;}" in html
    # ダークテーマバグ修正: .wcard:hover のハードコード #f0f0f3 廃止
    assert "#f0f0f3" not in html
    assert ".wcard:hover{border-color:var(--border-focus);background:var(--hover)}" in html


def test_ux2_active_filter_chips_and_qhelp(ux_html: str) -> None:
    """機能2: ×付きフィルタチップ・すべて解除・0件回復・qhelp実例・debounce。"""
    html = ux_html
    # parseQuery と同一 regex の表示用トークナイザ + 完全一致除去（fail-safe）
    assert "function qTokens(q)" in html
    assert "function rmToken(val,raw)" in html
    assert 'data-rm="' in html
    assert "すべて解除" in html
    assert "×で条件を外してください" in html  # 0件時の回復導線
    # qhelp: 実データ由来のクリック実行例 + 従来リファレンス
    assert "function qhelpHtml()" in html
    assert "例（クリックで実行）" in html
    assert "tag:業種/食品" in html  # 構文リファレンスは維持
    # 120ms debounce（__lastQ は即時更新）
    assert "dt=setTimeout(()=>runSearchPane(inp.value,out),120)" in html
    # parseQuery/matchItem は不改造（検索コアの回帰担保・文字列レベル）
    assert (
        "function parseQuery(q){const P={plain:[],neg:[],tag:[],path:[],file:[],ntag:[],npath:[],nfile:[],props:[],regex:[]};"
        in html
    )
    assert "for(const t of P.tag)if(!tagMatch(it.tags,t))return false;" in html


def test_ux3_chips_on_results_note_rightpanel(ux_html: str) -> None:
    """機能3: 結果行チップ=AND追加（行openと分離）・カルテ note-tags・右パネルチップ化。"""
    html = ux_html
    # 結果行: 先頭3個+「+n」・stopPropagation で行クリックと分離・AND追記は結果行のみ
    assert 'it.tags.slice(0,3).map(t=>chipHtml(t,"and"))' in html
    assert (
        '".sr .tagchip").forEach(ch=>ch.onclick=e=>{e.stopPropagation();addTagToQuery(ch.dataset.tag);})'
        in html
    )
    assert "function addTagToQuery(tag)" in html
    assert "クリックで絞り込みに追加" in html
    # カルテ: 未使用だった .note-tags を実際に描画（クリック=置換は afterOpen でバインド）
    assert '\'<div class="note-tags">\'+ctg.map(t=>chipHtml(t)).join("")' in html
    assert '\'<div class="note-tags">\'+dtg.map(t=>chipHtml(t)).join("")' in html
    # 右パネル tags タブ: 素テキスト #タグ をチップに置換（tagJump のまま）
    assert '<div class="rtagwrap">' in html
    assert (
        'b.querySelectorAll(".tagchip").forEach(el=>el.onclick=()=>tagJump(el.dataset.tag))' in html
    )


def test_ux4_tag_pane_enhancements(ux_html: str) -> None:
    """機能4: 意味順+見出し・系統色・ペイン内絞り込み・件数/五十音トグル・12超畳み・active。"""
    html = ux_html
    assert (
        'const TAGORDER=["宿題","温度感","フェーズ","BANT","業種","最終接点","資料種別","施策","媒体","動画形式","形式","横断","更新","情報源"]'
        in html
    )
    assert "取引先のタグ" in html and "資料のタグ" in html
    assert "タグを絞り込み…" in html
    assert "tagSortAlpha" in html and "件数順⇄五十音順" in html
    assert '"他 "+(vk.length-12)+" 件を表示"' in html  # 葉12超の畳み
    assert "act.some(a=>tagMatch([t],a))" in html  # __lastQ と tagMatch 照合で .active
    assert "window.__tagFocus" in html  # ホームのテーザー経由初期展開


def test_ux5_welcome_quickfilter_and_teaser(ux_html: str) -> None:
    """機能5: クイックフィルタ（動的存在チェック・件数バッジ）+ タグ系統テーザー。"""
    html = ux_html
    assert "クイックフィルタ" in html and "タグで探す" in html
    # プリセットは1箇所定義 + tagCount による動的存在チェック（0件は非表示）
    assert 'const QF=[["宿題あり","宿題/あり"]' in html
    assert "QF.filter(([l,t])=>tagCount[t]>0).slice(0,8)" in html
    # クリック=runQuery 置換 / テーザー=タグペインを該当系統展開で開く
    assert '".qfc").forEach(el=>el.onclick=()=>runQuery(el.dataset.q))' in html
    assert 'window.__tagFocus=el.dataset.c;setPane("tags");' in html


def test_ux6_table_temp_and_homework_filters(ux_html: str) -> None:
    """機能6: 温度感セレクト+宿題チェック（tblFilter 2本追加のみ・TCOLS/ソート不介入）。"""
    html = ux_html
    assert 'tblFilter={q:"",ind:"",phase:"",temp:"",hw:false}' in html
    assert "if(tblFilter.temp)rows=rows.filter(c=>c.temp===tblFilter.temp);" in html
    assert "if(tblFilter.hw)rows=rows.filter(c=>c.hw);" in html
    # 温度感4値は実値集合から生成（ラベルはタグペインと同一文字列）+ 宿題チェック
    assert '["高","ポジ優勢","拮抗","ネガ優勢"].filter(t=>DATA.clients.some(c=>c.temp===t))' in html
    assert "温度感（すべて）" in html and "宿題ありのみ" in html
    # 0件時メッセージ + 適用中チップ（native select の補完）
    assert "条件に一致する取引先がありません — フィルタを1つ外してください" in html
    assert 'chipHtml("温度感/"+tblFilter.temp)' in html
    # 既存挙動の不介入: TCOLS・列定義は従来のまま（次アクション列まで9列）
    assert '["nx","次アクション",c=>c.nx||"",0]' in html
    # 行クリックはデータ行のみ（0件メッセージ行に openClient を配線しない）
    assert (
        'tb.querySelectorAll("tr[data-s]").forEach(tr=>tr.onclick=()=>openClient(tr.dataset.s))'
        in html
    )


def test_ux5_quickfilter_counts_in_stats(sidecars: Path, vault: Path, tmp_path: Path) -> None:
    """機能5: stats.json に QF プリセット8件の件数が焼き込まれる（回帰検知）。"""
    out = tmp_path / "app.html"
    assert _run(vault, out) == 0
    stats = json.loads(Path(str(out) + ".stats.json").read_text(encoding="utf-8"))
    qf = stats["qf"]
    assert set(qf.keys()) == set(_mod.QF_PRESET_TAGS)
    # ダミー Vault: 資料3件は全て doc_type=提案書 / 宿題・温度感・横断等の該当なし
    assert qf["資料種別/提案書"] == 3
    assert qf["宿題/あり"] == 0
    assert qf["横断"] == 0


def test_quickfilter_counts_unit() -> None:
    """_quickfilter_counts: tagMatch 同等の「子タグも一致」判定と ageBucket ミラー。"""
    today = date(2026, 7, 12)
    clients = [
        {
            "industry": "IT",
            "phase": "提案",
            "bantg": "B",
            "last": "2026-07-01",
            "temp": "ネガ優勢",
            "hw": 1,
        },
        {"industry": "", "phase": "", "bantg": "", "last": "2024-01-01", "temp": "", "hw": 0},
    ]
    docs = [
        {
            "doc_type": "提案書",
            "industry": "IT",
            "solution": "動画広告",
            "modified": "2026-07-05",
            "src": "Drive",
            "media": ["TikTok"],
            "vfmt": ["ショート"],
            "fmt": "PPTX",
            "xc": "2社",
        },
    ]
    qf = _mod._quickfilter_counts(clients, docs, today)
    assert qf["宿題/あり"] == 1
    assert qf["温度感/ネガ優勢"] == 1
    assert qf["温度感/高"] == 0
    assert qf["最終接点/1年以上前"] == 1  # 2024-01-01 → 366日超
    assert qf["更新/1ヶ月以内"] == 1
    assert qf["横断"] == 1  # 横断/2社 が親 preset「横断」に一致（tagMatch 同等）
    assert qf["動画形式/ショート"] == 1
    assert qf["資料種別/提案書"] == 1


def test_age_bucket_mirror_boundaries() -> None:
    """_age_bucket: JS ageBucket と同じ 31/92/183/366 閾値・不正/空は空文字。"""
    t = date(2026, 7, 12)
    ab = _mod._age_bucket
    assert ab("2026-07-12", t) == "1ヶ月以内"
    assert ab("2026-06-11", t) == "1ヶ月以内"  # 31日前
    assert ab("2026-06-10", t) == "3ヶ月以内"  # 32日前
    assert ab("2026-04-11", t) == "3ヶ月以内"  # 92日前
    assert ab("2026-01-10", t) == "半年以内"  # 183日前
    assert ab("2025-07-11", t) == "1年以内"  # 366日前
    assert ab("2025-07-10", t) == "1年以上前"  # 367日前
    assert ab("", t) == ""
    assert ab("invalid", t) == ""
