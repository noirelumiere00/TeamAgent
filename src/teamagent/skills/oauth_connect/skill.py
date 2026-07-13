"""oauth_connect Skill 本体。

ユーザーが「連携」「Google連携」「Slack連携」「接続」等と言ったとき、本人メールに紐づく
個別の OAuth 認可 URL を返す。実トークンの取得・保存は connect-web の
/oauth2/callback（Google）・/slack/oauth/callback（Slack xoxp）が担う
（本 Skill は URL を生成して返すだけ）。

**未連携のサービスだけ案内する**:
  - Google も Slack も未連携 → 両方のリンク
  - 片方だけ未連携 → その未連携の方だけ
  - 両方連携済み → リンクを出さず「連携済み」と返す
  連携状態は per-user トークンストア（RdsTokenStore / SlackTokenStore の has()）で判定。
  判定に失敗した場合は fail-safe で「未連携扱い」＝リンクを出す（連携フローを絶対に塞がない。
  過剰にリンクを出すのは従来挙動と同じで安全側）。

セキュリティ:
  - 対象は常に「呼び出した本人」。user_email は SkillContext.metadata から取得し、
    無ければ fail-closed（他人分の URL を作らない）。MCP 境界の hybrid identity が
    slack_user_id → 本人 user_email を解決して metadata に載せている前提。
  - URL の state は本人 email で HMAC 署名済み（make_state）。万一他人がリンクを
    開いて別アカウントを認可しても、callback は state の email を key に保存するため
    「あなた専用」である旨を message に明記して誤共有を促さない。
  - トークンストアは RLS（本人行のみ）越しに has() するだけで、生トークンは扱わない。
"""

from __future__ import annotations

import os
from typing import Any, ClassVar

import structlog
from pydantic import BaseModel

from teamagent.adapters.google_oauth_flow import WORKSPACE_SCOPES, OAuthConsentFlow
from teamagent.adapters.slack_oauth_flow import SlackOAuthConsentFlow
from teamagent.skills.base import BaseSkill, SkillContext, register
from teamagent.skills.oauth_connect.schema import OAuthConnectInput, OAuthConnectOutput

logger = structlog.get_logger(__name__)


def _mask_email(email: str) -> str:
    if "@" not in email:
        return "***"
    local, _, domain = email.partition("@")
    return f"{local[:1] if local else ''}***@{domain}"


def _build_google_store() -> Any:
    """Google 用 per-user トークンストア（RdsTokenStore）を env から構築。"""
    from teamagent.adapters.oauth_token_store import KmsCipher, RdsTokenStore
    from teamagent.adapters.pgvector_client import PgVectorClient

    key_id = os.environ.get("OAUTH_KMS_KEY_ID")
    if not key_id:
        raise RuntimeError("OAUTH_KMS_KEY_ID が未設定です")
    return RdsTokenStore(PgVectorClient.from_env(), KmsCipher(key_id))


def _build_slack_store() -> Any:
    """Slack(xoxp) 用 per-user トークンストア（SlackTokenStore）を env から構築。"""
    from teamagent.adapters.oauth_token_store import KmsCipher, SlackTokenStore
    from teamagent.adapters.pgvector_client import PgVectorClient

    key_id = os.environ.get("OAUTH_KMS_KEY_ID")
    if not key_id:
        raise RuntimeError("OAUTH_KMS_KEY_ID が未設定です")
    return SlackTokenStore(PgVectorClient.from_env(), KmsCipher(key_id))


@register
class OAuthConnectSkill(BaseSkill[OAuthConnectInput, OAuthConnectOutput]):
    """本人専用の Google / Slack 連携リンクを発行する Skill（per-user・未連携のみ・URL のみ）。"""

    name: ClassVar[str] = "oauth_connect"
    description: ClassVar[str] = (
        "ユーザーが自分の Google と Slack のアカウントを連携（認可）するための"
        "本人専用リンクを発行する。『連携』『連携したい』『Google連携』『Slack連携』"
        "『接続』『connect』等を言われたら呼ぶ。対象は常に話しかけている本人で、引数は不要"
        "（本人は MCP 境界が解決する）。**まだ連携していないサービスのリンクだけ**を返す"
        "（両方連携済みなら『連携済み』と返る）。連携済みでも機能追加で必要な権限（スコープ）が"
        "増えている場合は自動で*再連携*リンクを返す（『再連携』『連携し直す』と言われた場合もこれで足りる）。"
        "返した message を**一字一句そのまま**本人に提示すること（message 内の [ラベル](URL) "
        "リンクを崩さない・URL を裸で貼り直さない・コードブロックで包まない・URL 文字列に"
        "手を加えない）。"
    )
    input_schema: ClassVar[type[BaseModel]] = OAuthConnectInput
    output_schema: ClassVar[type[BaseModel]] = OAuthConnectOutput

    def __init__(self, *, google_store: Any = None, slack_store: Any = None) -> None:
        # ストアは省略時 env から遅延構築（本番）。テストは注入して DB 依存を回避。
        self._google_store = google_store
        self._slack_store = slack_store

    def _is_connected(
        self, requester: str, injected: Any, builder: Any, log: Any, kind: str
    ) -> bool:
        """本人が該当サービスを連携済みか（store.has）。判定不能は未連携扱い(False)。

        DB/KMS 障害やテーブル未整備で例外が出ても、連携フローは絶対に塞がない
        （False＝リンクを出す＝従来挙動と同じ安全側）。
        """
        try:
            store = injected if injected is not None else builder()
            return bool(store.has(requester))
        except Exception as e:  # fail-safe: 判定不能は未連携扱いに倒す
            log.warning("oauth_connect_conn_check_failed", kind=kind, error=type(e).__name__)
            return False

    def _google_status(self, requester: str, log: Any) -> tuple[bool, bool]:
        """Google の連携状態を (connected, scope_upgrade_needed) で返す。

        v0.3 で WORKSPACE_SCOPES に calendar.events 等が追加されたが、既連携ユーザーの
        stored scopes は旧のまま＝「連携済みだが機能が動かない」状態になる。従来の
        has()（行の有無）では検知できず、しかも『連携済みはリンクを出さない』仕様のため
        本人が再連携したくてもリンクを入手できなかった（2026-07-13 パイロットで実害）。
        → stored scopes ⊇ WORKSPACE_SCOPES を要求し、不足なら再連携リンクを出す。
        判定不能（store が scopes 未実装/例外）は従来どおり has() ベースへフォールバック。
        """
        try:
            store = self._google_store if self._google_store is not None else _build_google_store()
            scopes_fn = getattr(store, "scopes", None)
            if not callable(scopes_fn):
                return bool(store.has(requester)), False
            stored = scopes_fn(requester)
            if stored is None:
                return False, False
            missing = set(WORKSPACE_SCOPES) - set(stored)
            if missing:
                log.info(
                    "oauth_connect_scope_upgrade_needed",
                    missing_count=len(missing),
                    stored_count=len(stored),
                )
                return False, True
            return True, False
        except Exception as e:  # fail-safe: 判定不能は未連携扱い（リンクを出す＝安全側）
            log.warning("oauth_connect_conn_check_failed", kind="google", error=type(e).__name__)
            return False, False

    def run(self, _input: OAuthConnectInput, ctx: SkillContext) -> OAuthConnectOutput:
        log = ctx.bind_logger(self.name)

        # G1: 本人 user_email 必須（fail-closed・他人分の URL を作らない）。
        # 観測性(柱2): 本人未解決での fail-closed は「連携が機能していない」直近シグナル。
        # 構造化 event を出して metric filter→alarm で拾えるようにする（無音にしない）。
        requester = ctx.metadata.get("user_email")
        if not requester or not isinstance(requester, str) or not requester.strip():
            log.warning("oauth_connect_fail_closed", reason="no_user_email")
            raise PermissionError("oauth_connect は本人 user_email が必須です（本人専用リンク）")
        requester = requester.strip()

        # 連携状態（未連携のものだけ案内する）。Google はスコープ不足も「要再連携」として検知。
        google_connected, google_scope_upgrade = self._google_status(requester, log)
        slack_configured = bool(os.environ.get("SLACK_OAUTH_REDIRECT_URI", "").strip())
        slack_connected = slack_configured and self._is_connected(
            requester, self._slack_store, _build_slack_store, log, "slack"
        )

        # Google 認可URL（未連携時のみ生成）。
        url: str | None = None
        if not google_connected:
            redirect = os.environ.get("OAUTH_REDIRECT_URI", "").strip()
            if not redirect:
                raise ValueError(
                    "OAUTH_REDIRECT_URI が未設定です（connect-web の公開 callback URL）"
                )
            try:
                url, _state = OAuthConsentFlow(redirect_uri=redirect).authorization_url(requester)
            except Exception as e:
                log.warning("oauth_connect_url_failed", error=type(e).__name__)
                raise ValueError(
                    "連携リンクの生成に失敗しました（管理者へ: OAuth 系 env をご確認ください）"
                ) from e

        # Slack 個人トークン(xoxp) の認可URL（設定済み & 未連携時のみ生成）。
        # 生成失敗は Google のみで継続（fail-open）。
        slack_url: str | None = None
        if slack_configured and not slack_connected:
            slack_redirect = os.environ.get("SLACK_OAUTH_REDIRECT_URI", "").strip()
            try:
                slack_url, _ = SlackOAuthConsentFlow(redirect_uri=slack_redirect).authorization_url(
                    requester
                )
            except Exception as e:
                log.warning("oauth_connect_slack_url_failed", error=type(e).__name__)
                slack_url = None

        masked = _mask_email(requester)
        message = _compose_message(
            requester,
            url,
            slack_url,
            google_connected,
            slack_connected,
            google_scope_upgrade=google_scope_upgrade,
        )

        log.info(
            "oauth_connect_url_issued",
            user_email_masked=masked,
            google_included=bool(url),
            slack_included=bool(slack_url),
            google_connected=google_connected,
            slack_connected=slack_connected,
            google_scope_upgrade=google_scope_upgrade,
        )
        return OAuthConnectOutput(
            url=url, slack_url=slack_url, user_email_masked=masked, message=message
        )


def _compose_message(
    requester: str,
    url: str | None,
    slack_url: str | None,
    google_connected: bool,
    slack_connected: bool,
    *,
    google_scope_upgrade: bool = False,
) -> str:
    """未連携サービスの案内文を組み立てる（連携済みは省略・両方済みは完了案内）。

    表示契約（OpenClaw 経由の Slack 返信前提・2026-07-13 実機の生URL裸貼り対策）:
      - リンクは標準 Markdown の `[ラベル](URL)` で書く。OpenClaw(@openclaw/slack) は
        エージェント返信を markdown→mrkdwn 変換するため `<URL|ラベル>` の装飾リンク
        （ラベル付き青リンク）として表示される。生 URL の裸貼りは怪しく見えて
        踏まれないため出さない。
      - 各リンクはリスト項目（`- `）にする。リストはブロック要素なので、変換器の
        soft-break の扱いに依存せず確実に1行ずつ分かれて表示される。
      - 太字は `**…**`（標準 Markdown）。`*…*` は変換で italic になる。
      - SOUL.md 側で「message を一字一句そのまま返す」を指示しており、ここが
        Slack 表示の最終形。書式を変えるときは SOUL.md の連携セクションと合わせる。
    """
    targets: list[tuple[str, str]] = []  # (リンクラベル, URL)
    if url:
        # #188: v0.3 スコープ追加により既連携ユーザーは「連携済みだが権限不足」になる。
        # その場合は連携リンク自体は出す（google_connected=False）が、ラベルで再連携である旨を示す。
        if google_scope_upgrade:
            g_label = "Google を再連携する（権限追加のため・カレンダー登録/日程提案等）"
        else:
            g_label = "Google を連携する（メール・カレンダー等）"
        targets.append((g_label, url))
    if slack_url:
        targets.append(("Slack を連携する（本人としての検索・チャンネル巡回）", slack_url))

    # 出すものが無い＝すべて連携済み。
    if not targets:
        done = []
        if google_connected:
            done.append("Google")
        if slack_connected:
            done.append("Slack")
        joined = " と ".join(done) if done else "アカウント"
        return (
            f"✅ **{requester}** は既に {joined} を連携済みです。追加の操作は不要です。"
            "そのまま話しかけてください。"
        )

    lines = [f"👋 **{requester}** の連携リンクです（1回だけ・所要1分）。"]
    already = []
    if google_connected:
        already.append("Google")
    if slack_connected:
        already.append("Slack")
    if already:
        lines.append(f"（{' と '.join(already)} は連携済みのため省略しています）")
    lines.append("下のリンクは **あなた専用** です（他の人と共有しないでください）。")
    lines.append("")

    if len(targets) == 1:
        label, link = targets[0]
        lines.append(f"- [🔗 {label}]({link})")
    else:
        marks = ["①", "②", "③"]
        for i, (label, link) in enumerate(targets):
            lines.append(f"- [🔗 {marks[i]} {label}]({link})")

    lines.append("")
    lines.append(
        "リンクを開いて、表示される権限を **許可** してください。"
        "「✅ 連携が完了しました」が出れば成功です。あとは話しかけるだけ。"
    )
    return "\n".join(lines)
