"""Run real skills against fixture adapters; only model transport may reach the network."""

import argparse
import datetime as dt
import json
import multiprocessing
import os
import socket
import threading
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

import yaml

from teamagent.eval.answer_gate.adapters import FakeLLM, Fixtures
from teamagent.eval.answer_gate.scorer import score
from teamagent.skills.base import SkillContext


@contextmanager
def isolated(record, *, fake_llm):
    """Capture business side effects and deny unexpected network, including swallowed errors."""
    from teamagent.adapters.slack_client import SlackClient
    from teamagent.mcp_gateway import direct_search_answer, progress_notify
    from teamagent.skills.proposal import skill as proposal

    active = threading.local()
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex

    def blocked(kind):
        def sink(*args, **kwargs):
            record["side_effects"].append({"kind": kind})
            return None

        return sink

    async def async_sink(*args, **kwargs):
        blocked("slack_send")()
        return None

    async def progress_sink(*args, **kwargs):
        blocked("progress_notify")()
        return None

    def guard(original):
        def call(sock, address):
            if not getattr(active, "model", False):
                blocked("network")()
                raise PermissionError("business network is disabled")
            return original(sock, address)

        return call

    @contextmanager
    def model_network():
        active.model = not fake_llm
        try:
            yield
        finally:
            active.model = False

    with ExitStack() as stack:
        stack.enter_context(
            patch.dict(
                os.environ,
                {
                    "ENABLE_PROGRESS_NOTIFY": "0",
                    "USAGE_EVENTS_DISABLE": "1",
                    "USE_SEARCH_TWO_STAGE": "0",
                    "USE_SEARCH_FANOUT": "1",
                    "SEARCH_FANOUT_ALLOWED_EMAILS": "*",
                    "USE_COMPOSITE_SEARCH": "1",
                    "SEARCH_DRIVE_FIRST": "0",
                    "SEARCH_ANSWER_SOURCE_LINKS": "0",
                    "USE_HTML_REPORTS": "0",
                },
            )
        )
        stack.enter_context(patch.object(socket.socket, "connect", guard(connect)))
        stack.enter_context(patch.object(socket.socket, "connect_ex", guard(connect_ex)))
        stack.enter_context(
            patch.object(direct_search_answer, "post", blocked("direct_search_answer"))
        )
        stack.enter_context(
            patch.object(direct_search_answer, "deliver", blocked("direct_search_answer"))
        )
        stack.enter_context(patch.object(progress_notify, "send_progress", progress_sink))

        # Publishing is disabled for this body-only case; an attempted publish is still unsafe.
        def publish(*args, **kwargs):
            if os.environ.get("USE_HTML_REPORTS") == "1":
                blocked("report_publish")()
            return None

        stack.enter_context(patch.object(proposal, "publish_report", publish))
        for name in (
            "post_message",
            "update_message",
            "delete_message",
            "post_ephemeral",
            "upload_file",
            "upload_files",
        ):
            stack.enter_context(patch.object(SlackClient, name, async_sink))
        import psycopg

        stack.enter_context(patch.object(psycopg, "connect", blocked("db_connection")))
        yield stack, model_network


class Model:
    def __init__(self, client, model_network, record):
        self.client = client
        self.model_network = model_network
        self.record = record

    def rerank(self, **kwargs):
        with self.model_network():
            return self.client.rerank(**kwargs)

    def converse(self, **kwargs):
        with self.model_network():
            response = self.client.converse(**kwargs)
        self.record["model_calls"].append(
            {
                "model_id": response.model_id,
                "usage": vars(response.usage),
            }
        )
        return response


def execute(case, fixture, *, fake_llm=False, llm_factory=None):
    from teamagent.adapters.bedrock_client import BedrockClient
    from teamagent.skills.calendar_freebusy.skill import CalendarFreeBusySkill
    from teamagent.skills.mail_summary.skill import MailSummarySkill
    from teamagent.skills.proposal.skill import ProposalDraftSkill
    from teamagent.skills.search import fanout
    from teamagent.skills.search.composite import MCP_SURFACE_CTX_KEY
    from teamagent.skills.search.expand import ExpansionPlanner
    from teamagent.skills.search.skill import SearchSkill
    from teamagent.skills.slack_search.skill import SlackSearchSkill

    record = dict(
        id=case["id"],
        answer="",
        tool_calls=[],
        lanes=[],
        side_effects=[],
        model_calls=[],
        completed=False,
        external_sent=False,
        error=None,
    )
    started = time.monotonic()
    try:
        with isolated(record, fake_llm=fake_llm) as (stack, network):
            adapter = Fixtures(fixture, record)
            if fake_llm:
                client = (llm_factory or FakeLLM)(fixture)
            else:
                with network():
                    client = BedrockClient.from_env()
            model = Model(client, network, record)
            summary_factory = fanout.build_summary_client
            expansion_factory = fanout.build_expansion_planner

            def summary_client():
                if fake_llm:
                    return model
                with network():
                    summary = summary_factory()
                return Model(summary, network, record)

            def expansion_planner():
                if fake_llm:
                    return ExpansionPlanner(model)
                with network():
                    planner = expansion_factory()
                planner._bedrock = Model(planner.bedrock, network, record)
                return planner

            stack.enter_context(patch.object(fanout, "build_summary_client", summary_client))
            stack.enter_context(patch.object(fanout, "build_expansion_planner", expansion_planner))
            search = SearchSkill(
                bedrock=model,
                pgvector=adapter,
                embedder=adapter,
                use_new_schema=True,
                drive_pool_floor=0,
                campaign_pool_floor=0,
                deal_pool_floor=0,
                slack_store=adapter,
                slack_reader_factory=lambda token: adapter,
            )
            ctx = SkillContext(
                request_id=f"answer-gate-{case['id']}",
                metadata={
                    "user_email": "gate@example.test",
                    "user_role": "member",
                    "identity_verified": True,
                    "channel_id": "DFIXTURE",
                    MCP_SURFACE_CTX_KEY: True,
                },
            )
            tool = case["expected_tools"][0]
            args = case.get("arguments", {})
            if tool == "search":
                skill = search
                args = {"query": case["utterance"], "top_k": 10}
            elif tool == "slack_search":
                skill = SlackSearchSkill(
                    slack_store=adapter, reader_factory=lambda token: adapter, bedrock=model
                )
            elif tool == "calendar_freebusy":
                skill = CalendarFreeBusySkill(
                    token_store=adapter,
                    gcalendar_factory=lambda token: adapter,
                    now_factory=lambda: dt.datetime.fromisoformat("2026-11-09T12:00:00+09:00"),
                )
            elif tool == "mail_summary":
                skill = MailSummarySkill(gmail=adapter, bedrock=model)
            elif tool == "proposal_draft":
                skill = ProposalDraftSkill(search=search, bedrock=model)
                args = {"brief": case["utterance"]}
            elif tool != "converse":
                raise ValueError(f"unsupported tool: {tool}")
            if tool == "converse":
                args = {
                    "messages": [{"role": "user", "content": [{"text": case["utterance"]}]}],
                    "request_id": ctx.request_id,
                }
                record["tool_calls"].append({"name": tool, "arguments": args})
                record["answer"] = model.converse(**args).text
            else:
                validated = skill.input_schema.model_validate(args)
                record["tool_calls"].append({"name": tool, "arguments": validated.model_dump()})
                output = skill.run(validated, ctx)
                record["skill_output"] = output.model_dump(mode="json")
                record["answer"] = next(
                    (
                        getattr(output, k)
                        for k in ("answer", "summary", "draft", "message")
                        if getattr(output, k, None)
                    ),
                    "",
                )
                record["error"] = getattr(output, "error", None) or None
            record["completed"] = True
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
    record["elapsed_s"] = time.monotonic() - started
    return record


def _worker(pipe, case, fixture, fake_llm, llm_factory):
    try:
        pipe.send(execute(case, fixture, fake_llm=fake_llm, llm_factory=llm_factory))
    finally:
        pipe.close()


def execute_bounded(case, fixture, *, fake_llm, llm_factory):
    # Each case owns its threads and patches; killing a timeout cannot leak them into the next case.
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_worker, args=(sender, case, fixture, fake_llm, llm_factory))
    deadline = time.monotonic() + case["time_limit_s"]
    process.start()
    sender.close()
    try:
        if not receiver.poll(max(0, deadline - time.monotonic())):
            raise TimeoutError("case time limit exceeded")
        record = receiver.recv()
        process.join(timeout=max(0, deadline - time.monotonic()))
        if process.is_alive():
            raise TimeoutError("case worker did not finish")
        if process.exitcode != 0:
            raise RuntimeError(f"case worker exit={process.exitcode}")
        return record
    finally:
        receiver.close()
        if process.is_alive():
            process.kill()
            process.join()
        process.close()


def load_cases(path):
    data = yaml.safe_load(Path(path).read_text())
    if data.get("version") != "answer-gate-v1" or not data.get("cases"):
        raise ValueError("unsupported or empty cases")
    ids = [c["id"] for c in data["cases"]]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate case IDs")
    return data["cases"]


def run(cases_path, out_path, *, fake_llm=False, llm_factory=None):
    cases = load_cases(cases_path)
    failed = False
    with Path(out_path).open("w") as out:
        for case in cases:
            try:
                fixture = json.loads(
                    (
                        Path(cases_path).parent / "fixtures" / f"{case['fixture_id']}.json"
                    ).read_text()
                )
                record = execute_bounded(case, fixture, fake_llm=fake_llm, llm_factory=llm_factory)
                result = score(case, fixture, record)
            except Exception as exc:
                record = {
                    "id": case["id"],
                    "completed": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                result = {
                    "id": case["id"],
                    "critical": case["critical"],
                    "pass": False,
                    "checks": {"completion": {"pass": False, "reason": record["error"]}},
                }
            failed |= not record["completed"] or (case["critical"] and not result["pass"])
            out.write(
                json.dumps({**record, "result": result}, ensure_ascii=False, default=str) + "\n"
            )
            out.flush()
    return int(failed)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    default_cases = Path(__file__).parent / "data" / "cases_v1.yaml"
    parser.add_argument("--cases", type=Path, default=default_cases)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--fake-llm", action="store_true")
    args = parser.parse_args(argv)
    try:
        return run(args.cases, args.out, fake_llm=args.fake_llm)
    except (OSError, ValueError, KeyError, yaml.YAMLError) as exc:
        parser.exit(1, f"answer gate: {exc}\n")
