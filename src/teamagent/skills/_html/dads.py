"""デジタル庁デザインシステム（DADS）に沿った HTML レポートの共通スタイル。

値の出典: デジタル庁デザインシステム https://design.digital.go.jp/dads/ のデザイントークン
（npm `@digital-go-jp/design-tokens` 2.0.1・MIT License・Copyright (c) 2023 デジタル庁）。
使う値だけを写し、CSS 変数名は元のトークン名にそろえる（どの値を使ったか追えるように）。

方針（DADS のルールのうちレポートに効くもの）:
- 本文 16px・行間 1.7、表は 14px の密な組み（Dns）。見出しは 32/24/20px の太字・行間 1.5。
- 文字色は solid-gray-800、補足は solid-gray-600。文字のコントラスト比 4.5:1 以上。
- 線・部品の境界は solid-gray-420（白地で 3:1 以上）。キーカラーは blue-900。
- リンクは blue-1000 の下線付き。フォーカスは黒の枠線＋黄色（yellow-300）の背景。
- 色だけで意味を伝えない（凡例・ラベルの文字を必ず添える）。
"""

from __future__ import annotations

DADS_TOKENS_CSS = """
:root{
--color-neutral-white:#ffffff;--color-neutral-black:#000000;
--color-neutral-solid-gray-50:#f2f2f2;--color-neutral-solid-gray-100:#e6e6e6;
--color-neutral-solid-gray-200:#cccccc;--color-neutral-solid-gray-420:#949494;
--color-neutral-solid-gray-536:#767676;--color-neutral-solid-gray-600:#666666;
--color-neutral-solid-gray-700:#4d4d4d;--color-neutral-solid-gray-800:#333333;
--color-neutral-solid-gray-900:#1a1a1a;
--color-primitive-blue-50:#e8f1fe;--color-primitive-blue-100:#d9e6ff;
--color-primitive-blue-800:#0031d8;--color-primitive-blue-900:#0017c1;
--color-primitive-blue-1000:#00118f;--color-primitive-blue-1100:#000071;
--color-primitive-light-blue-900:#0055ad;
--color-primitive-cyan-900:#006f83;
--color-primitive-green-50:#e6f5ec;--color-primitive-green-800:#197a4b;
--color-primitive-green-900:#115a36;
--color-primitive-yellow-50:#fbf5e0;--color-primitive-yellow-300:#ffd43d;
--color-primitive-yellow-900:#927200;--color-primitive-yellow-1000:#806300;
--color-primitive-orange-800:#c74700;
--color-primitive-red-50:#fdeeee;--color-primitive-red-900:#ce0000;
--color-primitive-purple-700:#6f23d0;
--color-primitive-magenta-900:#8b008b;
--font-family-sans:'Noto Sans JP',-apple-system,BlinkMacSystemFont,'Hiragino Sans',
  'Hiragino Kaku Gothic ProN',Meiryo,sans-serif;
--border-radius-4:0.25rem;--border-radius-8:0.5rem;--border-radius-12:0.75rem;
}
"""

DADS_BASE_CSS = """
*,*::before,*::after{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--color-neutral-white);color:var(--color-neutral-solid-gray-800);
  font-family:var(--font-family-sans);font-size:16px;line-height:1.7;letter-spacing:0.02em}
.dads-container{max-width:1120px;margin:0 auto;padding:40px 24px 64px}
h1{font-size:32px;line-height:1.5;font-weight:700;margin:0 0 8px;letter-spacing:0.01em}
h2{font-size:24px;line-height:1.5;font-weight:700;margin:56px 0 16px;letter-spacing:0.02em}
h3{font-size:20px;line-height:1.5;font-weight:700;margin:32px 0 12px;letter-spacing:0.02em}
p{margin:0 0 16px}
a{color:var(--color-primitive-blue-1000);text-decoration:underline;text-underline-offset:3px}
a:hover{color:var(--color-primitive-blue-1100);text-decoration-thickness:3px}
a:focus-visible{outline:4px solid var(--color-neutral-black);outline-offset:2px;
  background:var(--color-primitive-yellow-300);border-radius:var(--border-radius-4)}
.dads-lead{font-size:16px;color:var(--color-neutral-solid-gray-600);margin:0 0 24px}
.dads-meta{display:flex;flex-wrap:wrap;gap:4px 24px;margin:0;padding:16px 0;
  border-top:1px solid var(--color-neutral-solid-gray-420);
  border-bottom:1px solid var(--color-neutral-solid-gray-420);font-size:14px}
.dads-meta div{display:flex;gap:8px}
.dads-meta dt{color:var(--color-neutral-solid-gray-600)}
.dads-meta dd{margin:0;font-weight:700}
.dads-table-wrap{overflow-x:auto;border:1px solid var(--color-neutral-solid-gray-420);
  border-radius:var(--border-radius-8)}
table.dads-table{width:100%;border-collapse:collapse;font-size:14px;line-height:1.5}
.dads-table th{background:var(--color-neutral-solid-gray-50);text-align:left;font-weight:700;
  padding:8px 12px;border-bottom:1px solid var(--color-neutral-solid-gray-420);white-space:nowrap}
.dads-table td{padding:8px 12px;border-bottom:1px solid var(--color-neutral-solid-gray-200);
  vertical-align:top}
.dads-table tr:last-child td{border-bottom:0}
.dads-table .num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
.dads-tag{display:inline-block;border:1px solid currentColor;border-radius:var(--border-radius-4);
  padding:0 6px;font-size:14px;line-height:1.6;font-weight:700;white-space:nowrap;
  background:var(--color-neutral-white)}
.dads-notice{border:1px solid var(--color-primitive-yellow-900);border-left-width:8px;
  border-radius:var(--border-radius-8);background:var(--color-primitive-yellow-50);
  padding:12px 16px;margin:16px 0;font-size:16px}
.dads-notice b{color:var(--color-neutral-solid-gray-900)}
.dads-footnote{margin-top:56px;padding-top:16px;
  border-top:1px solid var(--color-neutral-solid-gray-420);
  font-size:14px;color:var(--color-neutral-solid-gray-600)}
.dads-footnote ul{margin:0 0 12px;padding-left:1.4em}
@media print{.dads-container{padding:0}.dads-table-wrap{overflow:visible}}
"""

DADS_CREDIT = (
    "表示はデジタル庁デザインシステム（DADS）のデザイントークンに沿っています"
    "（@digital-go-jp/design-tokens・MIT License・Copyright (c) 2023 デジタル庁）。"
)


def dads_style(extra_css: str = "") -> str:
    """`<style>` 要素（トークン＋基本部品＋呼び出し側の追加分）。"""
    return (
        "<style>/* Design tokens: @digital-go-jp/design-tokens 2.0.1 "
        "(MIT License, Copyright (c) 2023 Digital Agency, Japan) */"
        f"{DADS_TOKENS_CSS}{DADS_BASE_CSS}{extra_css}</style>"
    )


__all__ = ["DADS_BASE_CSS", "DADS_CREDIT", "DADS_TOKENS_CSS", "dads_style"]
