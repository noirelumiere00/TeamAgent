"""oauth_connect Skill 本体。

ユーザーが「連携」「Google連携」「Slack連携」「接続」等と言ったとき、本人メールに紐づく
個別の OAuth 認可 URL を返す。実トークンの取得・保存は connect-web の
/oauth2/callback（Google）・/slack/oauth/callback（Slack xoxp）が担う
（本 Skill は URL を生成して返すだけ）。

**未連携のサービスだけ案内する**:
  - Google も Slack も未連携 → 両方のリンク
  - 片方だけ未連携 → その未連携の方だけ
  - 両方連携済み → リンクを出さず「連携済み」と返す
  連携状態は per-user トークンストアで判定。Slack は保存済み user ID と検証済み caller ID
  も比較し、不一致なら本人が復旧できるよう再連携リンクを返す。
  判定に失敗した場合は fail-safe で「未連携扱い」＝リンクを出す（連携フローを絶対に塞がない。
  過剰にリンクを出すのは従来挙動と同じで安全側）。

セキュリティ:
  - 対象は常に「呼び出した本人」。user_email は SkillContext.metadata から取得し、
    無ければ fail-closed（他人分の URL を作らない）。MCP 境界の hybrid identity が
    slack_user_id → 本人 user_email を解決して metadata に載せている前提。
  - Slack URL の state は MCP 境界で検証済みの Slack user/team ID に束縛する。
    検証済み ID が無い経路では Slack URL を発行せず、Slack 内の安全な代替導線を案内する。
  - トークンストアは RLS（本人行のみ）越しに has() するだけで、生トークンは扱わない。

リンクの形（USE_OAUTH_START_LINKS・既定 OFF）:
  @Aico(openclaw) の LLM は約 600 字の Google 認可 URL（``?state=…&scope=…``）を再タイプして
  state の一部を変え、callback で HMAC 不一致にする（2026-08-31 / 09-02 実測。同じタスクが
  1〜2 分後に再発行したリンクは通る＝鍵ではなく転記の事故）。skills/_shared/report_delivery.py
  の ``/r/<token>`` と同じ対策＝**署名を ?query から path へ移す**。フラグ ON かつ
  ``CONNECT_BASE_URL`` 設定時だけ、Google/Slack のリンクを connect-web の
  ``/oauth2/start/{state}`` ``/slack/oauth/start/{state}``（query 無し・path のみ）に差し替える。
  connect-web 側は state を検証（消費はしない）して、ここと同一の認可 URL へ 302 する。
  既定 OFF の理由: connect-web に start ルートが着陸する前に ON にすると 404 になるため。
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


def start_links_enabled() -> bool:
    """USE_OAUTH_START_LINKS: 連携リンクを connect-web の path 形式で出す段階ゲート（既定 OFF）。

    report_delivery.short_url_enabled と同じ真偽値の読み方。connect-web に
    ``/oauth2/start`` ``/slack/oauth/start`` が着陸し、実機で 302 → 認可画面まで確認した後に ON。
    """
    return os.environ.get("USE_OAUTH_START_LINKS", "").strip().lower() in ("1", "true", "yes", "on")


def _start_link_base() -> str:
    """path 形式リンクの土台 URL（``CONNECT_BASE_URL``・末尾スラッシュ無し・未設定は ""）。"""
    from teamagent.skills.knowledge_search_url.skill import connect_base_url

    return connect_base_url()


def google_start_link(base: str, state: str) -> str:
    """connect-web の Google 連携開始リンク（query 無し・path のみ）。"""
    return f"{base}/oauth2/start/{state}"


def slack_start_link(base: str, state: str) -> str:
    """connect-web の Slack 連携開始リンク（query 無し・path のみ）。"""
    return f"{base}/slack/oauth/start/{state}"


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
        "『接続』『connect』等を言われたら呼ぶ。"
        "**メッセージが『連携』の一語だけでも呼ぶ**（雑談・聞き返しにしない）。"
        "**呼ぶ前に確認や聞き返しを挟まないこと**——『リンクを出しますか？』"
        "『Google と Slack のどちらですか？』と尋ねてはならない。未連携の Google と Slack を"
        "**まとめて 1 レスポンスで返す**ので、1 回の返信でリンクまで届けられる。"
        "対象は常に話しかけている本人で、引数は不要"
        "（本人は MCP 境界が解決する）。**まだ連携していないサービスのリンクだけ**を返す"
        "（両方連携済みなら『連携済み』と返る）。連携済みでも機能追加で必要な権限（スコープ）が"
        "増えている場合は自動で*再連携*リンクを返す（『再連携』『連携し直す』と言われた場合もこれで足りる）。"
        "返した message を本人に提示すること。"
    )
    input_schema: ClassVar[type[BaseModel]] = OAuthConnectInput
    output_schema: ClassVar[type[BaseModel]] = OAuthConnectOutput

    def __init__(self, *, google_store: Any = None, slack_store: Any = None) -> None:
        # ストアは省略時 env から遅延構築（本番）。テストは注入して DB 依存を回避。
        self._google_store = google_store
        self._slack_store = slack_store

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

    def _slack_status(
        self, requester: str, verified_uid: str | None, log: Any
    ) -> tuple[bool, bool]:
        """Slack の連携状態を ``(connected, rebind_needed)`` で返す。

        保存済み Slack user ID と検証済み caller ID がともにあり、不一致なら誤紐付けから
        本人が復旧できるよう未連携扱いにする。旧テストダブル等で ``slack_user_id()`` が未実装、
        または判定中に例外が出た場合だけ従来の ``has()`` 判定へフォールバックする。
        フォールバックも失敗した場合はリンクを出す側（False）に倒す。
        """
        try:
            store = self._slack_store if self._slack_store is not None else _build_slack_store()
            slack_user_id_fn = getattr(store, "slack_user_id", None)
            if not callable(slack_user_id_fn):
                return bool(store.has(requester)), False
            try:
                stored = slack_user_id_fn(requester)
            except Exception as e:
                log.warning(
                    "oauth_connect_slack_uid_check_failed",
                    error=type(e).__name__,
                )
                return bool(store.has(requester)), False
            if stored is None:
                return False, False
            stored_uid = stored.strip() if isinstance(stored, str) else ""
            if not stored_uid:
                log.info("oauth_connect_slack_rebind_needed", reason="stored_uid_missing")
                return False, True
            if verified_uid and stored_uid != verified_uid:
                log.info("oauth_connect_slack_rebind_needed", reason="uid_mismatch")
                return False, True
            return True, False
        except Exception as e:  # fail-safe: 判定不能は未連携扱い（リンクを出す＝安全側）
            log.warning("oauth_connect_conn_check_failed", kind="slack", error=type(e).__name__)
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
        verified_uid_raw = ctx.metadata.get("verified_slack_user_id")
        verified_team_raw = ctx.metadata.get("verified_slack_team_id")
        verified_uid = (
            verified_uid_raw.strip()
            if isinstance(verified_uid_raw, str) and verified_uid_raw.strip()
            else None
        )
        verified_team = (
            verified_team_raw.strip()
            if isinstance(verified_team_raw, str) and verified_team_raw.strip()
            else None
        )
        slack_configured = bool(os.environ.get("SLACK_OAUTH_REDIRECT_URI", "").strip())
        slack_connected, slack_rebind_needed = (
            self._slack_status(requester, verified_uid, log) if slack_configured else (False, False)
        )

        # Google 認可URL（未連携時のみ生成）。state は path 形式リンクへの差し替えに使う。
        url: str | None = None
        google_state: str | None = None
        if not google_connected:
            redirect = os.environ.get("OAUTH_REDIRECT_URI", "").strip()
            if not redirect:
                raise ValueError(
                    "OAUTH_REDIRECT_URI が未設定です（connect-web の公開 callback URL）"
                )
            try:
                url, google_state = OAuthConsentFlow(redirect_uri=redirect).authorization_url(
                    requester
                )
            except Exception as e:
                log.warning("oauth_connect_url_failed", error=type(e).__name__)
                raise ValueError(
                    "連携リンクの生成に失敗しました（管理者へ: OAuth 系 env をご確認ください）"
                ) from e

        # Slack 個人トークン(xoxp) の認可URL（設定済み & 未連携時のみ生成）。
        # 生成失敗は Google のみで継続（fail-open）。
        slack_url: str | None = None
        slack_state: str | None = None
        slack_url_suppressed = False
        if slack_configured and not slack_connected:
            slack_redirect = os.environ.get("SLACK_OAUTH_REDIRECT_URI", "").strip()
            if not verified_uid or not verified_team:
                slack_url_suppressed = True
                log.warning(
                    "oauth_connect_slack_url_suppressed",
                    reason=(
                        "no_verified_slack_user_id"
                        if not verified_uid
                        else "no_verified_slack_team_id"
                    ),
                )
            else:
                try:
                    slack_url, slack_state = SlackOAuthConsentFlow(
                        redirect_uri=slack_redirect
                    ).authorization_url(
                        requester,
                        slack_user_id=verified_uid,
                        slack_team_id=verified_team,
                    )
                except Exception as e:
                    log.warning("oauth_connect_slack_url_failed", error=type(e).__name__)
                    slack_url = None
                    slack_state = None

        # path 形式リンクへの差し替え（USE_OAUTH_START_LINKS=ON かつ CONNECT_BASE_URL 設定時のみ）。
        # OFF のときは上で組んだ認可 URL をそのまま返す（従来出力と同一）。ON なのに土台 URL が
        # 無い場合は黙って落とさず名指しで warning し、従来の認可 URL を返す（fail-open）。
        start_links = False
        if start_links_enabled() and (url or slack_url):
            base = _start_link_base()
            if not base:
                log.warning(
                    "oauth_connect_start_links_prereq_missing",
                    missing="CONNECT_BASE_URL",
                    hint=(
                        "USE_OAUTH_START_LINKS=1 だが CONNECT_BASE_URL 未設定のため"
                        "認可 URL を直接返す"
                    ),
                )
            else:
                start_links = True
                if url and google_state:
                    url = google_start_link(base, google_state)
                if slack_url and slack_state:
                    slack_url = slack_start_link(base, slack_state)

        masked = _mask_email(requester)
        message = _compose_message(
            requester,
            url,
            slack_url,
            google_connected,
            slack_connected,
            google_scope_upgrade=google_scope_upgrade,
            slack_rebind_needed=slack_rebind_needed,
            slack_url_suppressed=slack_url_suppressed,
        )

        log.info(
            "oauth_connect_url_issued",
            user_email_masked=masked,
            google_included=bool(url),
            slack_included=bool(slack_url),
            google_connected=google_connected,
            slack_connected=slack_connected,
            google_scope_upgrade=google_scope_upgrade,
            slack_rebind_needed=slack_rebind_needed,
            slack_url_suppressed=slack_url_suppressed,
            start_links=start_links,
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
    slack_rebind_needed: bool = False,
    slack_url_suppressed: bool = False,
) -> str:
    """未連携サービスの案内文を組み立てる（連携済みは省略・両方済みは完了案内）。"""
    targets: list[tuple[str, str, str]] = []
    if url:
        desc = "メールの読み取り・下書き作成、カレンダー等"
        if google_scope_upgrade:
            desc = (
                "機能追加により必要な権限が増えたため *再連携* が必要です"
                "（カレンダー登録・日程提案など）"
            )
        targets.append(("Google", desc, url))
    if slack_url:
        desc = "本人としての検索・チャンネル巡回"
        if slack_rebind_needed:
            desc = "現在の Slack アカウントと連携し直す必要があります"
        targets.append(("Slack", desc, slack_url))

    # 出すものが無い＝すべて連携済み。
    if not targets:
        if slack_url_suppressed:
            lines = []
            if google_connected:
                lines.append(f"✅ *{requester}* の Google は連携済みです。\n")
            lines.append("Slack で Aico に『連携』と話しかけてください。")
            return "".join(lines)
        done = []
        if google_connected:
            done.append("Google")
        if slack_connected:
            done.append("Slack")
        joined = " と ".join(done) if done else "アカウント"
        return (
            f"✅ *{requester}* は既に {joined} を連携済みです。追加の操作は不要です。"
            "そのまま話しかけてください。"
        )

    lines = [
        f"👋 *{requester}* の連携リンクです（1回だけ・所要1分）。\n",
        "下のリンクは *あなた専用* です（他の人と共有しないでください）。\n",
    ]
    already = []
    if google_connected:
        already.append("Google")
    if slack_connected:
        already.append("Slack")
    if already:
        lines.append(f"（{' と '.join(already)} は連携済みのため省略しています）\n")
    lines.append("開いて、表示される権限を *許可* してください:\n")

    if len(targets) == 1:
        label, desc, link = targets[0]
        lines.append(f"*{label} を連携*（{desc}）\n")
        lines.append(f"{link}\n")
    else:
        marks = ["①", "②", "③"]
        for i, (label, desc, link) in enumerate(targets):
            lines.append(f"\n*{marks[i]} {label} を連携*（{desc}）\n")
            lines.append(f"{link}\n")

    if slack_url_suppressed:
        lines.append("\nSlack で Aico に『連携』と話しかけてください。\n")
    lines.append("\n「✅ 連携が完了しました」が出れば成功です。あとは話しかけるだけ。")
    return "".join(lines)
