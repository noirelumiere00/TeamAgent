"""既存の名寄せ辞書・語境界判定を再利用する検索候補と範囲表示。"""

from __future__ import annotations

import unicodedata

from teamagent.skills.search.client_match import _LEGAL_SUFFIX_RE, alias_pairs, aliases
from teamagent.skills.search.composite import SlackLookup
from teamagent.skills.search.result_guard import _fold, _mention_at, find_client_mention
from teamagent.skills.search.schema import SearchInput


def retry_inputs(input: SearchInput) -> list[SearchInput]:
    """正規化・明示的な別名で最大2候補。商品名・stickyフィルタは落とさない。"""
    normalized = _LEGAL_SUFFIX_RE.sub("", unicodedata.normalize("NFKC", input.query)).strip()
    names = list(dict.fromkeys(name for pair in alias_pairs() for name in pair))
    mentioned = find_client_mention(normalized, names, strict=True)
    candidates: list[SearchInput] = []
    seen = {(input.query, input.filter_client)}

    def add(query: str, client: str | None) -> None:
        if query and len(query) <= 1000 and (query, client) not in seen and len(candidates) < 2:
            seen.add((query, client))
            candidates.append(input.model_copy(update={"query": query, "filter_client": client}))

    client = input.filter_client
    if normalized != input.query:
        add(normalized, client)
    if mentioned:
        start = _mention_at(_fold(normalized), _fold(mentioned))
        if start is not None:
            for alias in sorted(aliases(mentioned), key=lambda name: (-len(name), name)):
                replacement = normalized[:start] + alias + normalized[start + len(mentioned) :]
                # 別名で検索する際は、明示された取引先フィルタもその同じ別名に変える。
                new_client = client
                if client and alias in aliases(client):
                    new_client = alias
                add(replacement, new_client)
    elif client:
        for alias in sorted(aliases(client)):
            add(normalized, alias)
    return candidates


def scope_footer(queries: list[str], *, slack: SlackLookup | None, incomplete: bool = False) -> str:
    """実際に試した語と状態だけを1行。入力の改行・通知・長大文は無害化する。"""
    from teamagent.skills._shared.slack_context import _neutralize

    labels = [
        _neutralize(" ".join(q.split()), per_msg=80)
        .replace("<", "‹")
        .replace(">", "›")
        .replace("『", "")
        .replace("』", "")
        for q in queries
    ]
    scope = f"探した範囲: 金庫を『{'／'.join(dict.fromkeys(labels))}』で検索"
    if incomplete:
        scope += "（再検索の一部は時間切れ・失敗）"
    if slack is not None:
        if slack.status == "ok":
            scope += "・Slack も確認"
        else:
            scope += "・Slack は確認できませんでした"
    return scope
