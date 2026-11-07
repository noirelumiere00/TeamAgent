"""保管場所の説明を回答へ出さず、資料名・日付・原本リンクで示す。"""

import re


def without_source_attribution(text: str) -> str:
    text = re.sub(r"[（(](?:Slack|金庫)\s*[:：][^()（）\n]*[）)]", "", text)
    text = re.sub(
        r"(?:金庫の資料には無く、)?Slack\s*の投稿に(?:だけ)?(?:あります|ある)",
        "資料に記載があります",
        text,
    )
    text = re.sub(r"社内資料(?:と|・)\s*Slack(?:の投稿)?", "社内資料", text)
    text = re.sub(
        r"Slack\s*(?:での共有から|での共有|で共有された|共有|にだけある資料)", "資料", text
    )
    return text
