"""Read adapters replay fixtures; unimplemented calls fail closed."""

import base64
from contextlib import nullcontext
from types import SimpleNamespace

from teamagent.adapters.bedrock_client import ConverseResponse, TokenUsage
from teamagent.adapters.gmail_client import GmailMessage, GmailMessageRef
from teamagent.adapters.pgvector_client import SearchHit
from teamagent.adapters.slack_user_reader import SlackSearchMatch, SlackSearchRead


class Fixtures:
    scopes = ()

    def __init__(self, fixture, record):
        self.data = fixture
        self.record = record

    def __getattr__(self, name):
        self.record["side_effects"].append({"kind": "unsupported_adapter_call", "name": name})
        raise AttributeError(name)

    def lane(self, name, args):
        self.record["lanes"].append({"name": name, "status": "ok", "arguments": args})
        return self.data.get(name, [])

    def embed(self, text):
        # Retrieval is replayed, so no model/embedding download is needed.
        return [0.0] * 8

    def get(self, requester):
        return SimpleNamespace(access_token="fixture-only")

    def connection(self, **kwargs):
        return nullcontext(self)

    def transaction(self):
        return nullcontext(self)

    def cursor(self):
        return nullcontext(self)

    def execute(self, query, params=None):
        if query.startswith(("SELECT current_setting", "SELECT set_config")):
            return
        self.record["side_effects"].append({"kind": "db_write", "query": query})
        raise PermissionError("unexpected SQL")

    def fetchone(self):
        return {"timeout": "0"}

    def search_similar_new_schema(self, **kwargs):
        return [
            SearchHit(
                chunk_id=i,
                content=r["content"],
                score=0.95,
                metadata={
                    "title": r["title"],
                    "source_uri": r["url"],
                    "document_id": r["item_id"],
                    "source_type": r["source_type"],
                    "case_corpus": r.get("case_corpus", "false"),
                    "cls_industry": "飲料",
                    "updated_at": "2026-11-01",
                },
            )
            for i, r in enumerate(self.lane("vault", {"limit": kwargs.get("limit")}), 1)
        ]

    def list_client_names(self, **kwargs):
        return []

    def list_client_timeline_recent(self, *args, **kwargs):
        return []

    def find_documents_for_advertiser(self, *args, **kwargs):
        return []

    def resolve_file_urls_by_titles(self, *args, **kwargs):
        return {}

    def list_case_studies(self, *args, **kwargs):
        return [
            dict(
                title=r["title"],
                source_uri=r["url"],
                case_client="",
                case_brand="",
                case_product="飲料",
                case_effect=r["content"],
                case_industry="飲料",
                case_source="deck",
                case_external_use="unknown",
            )
            for r in self.lane("case", kwargs)
        ]

    def search_checked(self, query, request_id, **kwargs):
        rows = self.lane("slack", {"query": query, **kwargs})
        matches = tuple(
            SlackSearchMatch(
                ts=f"{1794186000 + i}.000000",
                text=r["content"],
                channel_id="CFIXTURE",
                channel_name="架空企画",
                permalink=r["url"],
                channel_is_channel=True,
                channel_is_private=False,
                channel_is_im=False,
                channel_is_mpim=False,
            )
            for i, r in enumerate(rows)
        )
        return SlackSearchRead(matches=matches, total=len(matches))

    def get_display_name(self, *args, **kwargs):
        return None

    def list_events(self, request_id, **kwargs):
        self.lane("calendar", {"method": "list_events", **kwargs})
        return [SimpleNamespace(**r) for r in self.data["events"]]

    def freebusy(self, request_id, **kwargs):
        self.lane("calendar", {"method": "freebusy", **kwargs})
        return [SimpleNamespace(**r) for r in self.data["busy"]]

    def list_messages(self, query, request_id, *, max_results):
        rows = self.lane("mail", {"query": query, "max_results": max_results})[:max_results]
        self.mail_rows = rows
        return [GmailMessageRef(r["ref"], r["ref"]) for r in rows], None

    def get_message(self, message_id, request_id):
        r = next(r for r in self.mail_rows if r["ref"] == message_id)
        body = f"{r['title']}\n{r['content']}\n{r['url']}"
        return GmailMessage(
            r["ref"],
            r["ref"],
            ("UNREAD",),
            body,
            1794186000000,
            {"Subject": r["title"]},
            {
                "mimeType": "text/plain",
                "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()},
            },
        )


class FakeLLM:
    """Fake the model transport only, without consulting the case's expected checks."""

    def __init__(self, fixture, *, one_item=False):
        self.fixture = fixture
        self.one_item = one_item

    def converse(self, *, messages, **kwargs):
        prompt = messages[0]["content"][0]["text"]
        if "広げた検索語と業種" in prompt:
            text = '{"terms": [], "industry": "飲料"}'
        elif "llm_text" in self.fixture:
            text = self.fixture["llm_text"]
        else:
            refs = [r for r in self.fixture["refs"] if r["title"] in prompt]
            if self.one_item:
                refs = refs[:1]
            text = "\n".join(
                f"{i}. {r['title']} — 準備状況を確認しました。 [{r['title']}]({r['url']})"
                for i, r in enumerate(refs, 1)
            )
            if refs and len(refs) < 5:
                text += f"\n見つかったのは {len(refs)} 件です。"
        return ConverseResponse(text, TokenUsage(0, 0, 0, 0, 0.0), "fixture-llm", 0, "end_turn")
