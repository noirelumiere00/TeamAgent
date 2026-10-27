"""Real stage/CLI composition with anonymous photos and fake S3, media transport and models."""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import pytest
from PIL import Image
from pptx import Presentation

from teamagent.adapters.media_job import MediaJobClient, MediaJobError
from teamagent.adapters.proposal_job_store import ProposalJobStore
from teamagent.mcp_gateway import async_job_notify as notify
from teamagent.media.contracts import MediaArtifact, MediaJobResult, S3ObjectRef
from teamagent.media.sales25_runner import render_sales25
from teamagent.skills._shared.long_jobs import ORIGIN_KEY
from teamagent.skills.first_visit import sales25_contract as contract
from teamagent.skills.omiyage_report import sales25
from teamagent.skills.omiyage_report.sales25_wire import Wire, production_stages
from teamagent.skills.omiyage_report.schema import OmiyageReportStatusInput, Sales25Intake
from teamagent.skills.omiyage_report.skill import (
    JobAdmission,
    OmiyageReportStatusSkill,
    OmiyageReportSubmitSkill,
)
from tests.skills.first_visit.sales25_fixture import inputs
from tests.skills.first_visit.test_sales25_analysis import fake_model, package, reply
from tests.skills.omiyage_report.test_sales25_parent import FakeSlack, ctx, flags, request, result

__all__ = ["flags"]


def build(
    store: ProposalJobStore, *, sales25_stages: sales25.Stages, thread_launcher: Any = None
) -> OmiyageReportSubmitSkill:
    return OmiyageReportSubmitSkill(
        store=store,
        sales25_stages=sales25_stages,
        thread_launcher=thread_launcher or (lambda target, name: target()),
        heartbeat_seconds=0,
        admission=JobAdmission(2),
    )


def photos(post: dict[str, Any]) -> dict[str, bytes]:
    artifacts = package(post, audio=False)
    manifest = json.loads(artifacts["media-manifest.json"])
    bundle = io.BytesIO()
    with zipfile.ZipFile(bundle, "w") as archive:
        for index, asset in enumerate(manifest["assets"]):
            data = io.BytesIO()
            Image.new("RGB", (72, 128), (40 + index * 20, 100, 180)).save(data, format="JPEG")
            body = data.getvalue()
            asset.update(bytes=len(body), sha256=hashlib.sha256(body).hexdigest())
            archive.writestr(asset["path"], body)
    manifest["identity"]["source_sha256"] = manifest["assets"][0]["sha256"]
    return {
        "media-manifest.json": contract.json_bytes(manifest),
        "media-bundle.zip": bundle.getvalue(),
    }


class Transport(MediaJobClient):
    def __init__(self, failure: str = "") -> None:
        super().__init__(
            queue_url="https://sqs.example.invalid", table="synthetic", bucket="synthetic-case"
        )
        self.failure = failure
        self.objects: dict[tuple[str, str], bytes] = {}
        self.latest: dict[str, S3ObjectRef] = {}
        self.requests: list[Any] = []

    def stage_bytes(self, **kwargs: Any) -> S3ObjectRef:
        body = kwargs["body"]
        path = f"media-jobs/{kwargs['job_id']}/input/{kwargs['name']}"
        version = str(len(self.objects) + 1)
        ref = S3ObjectRef(
            bucket="synthetic-case",
            key=path,
            version_id=version,
            size=len(body),
            sha256=hashlib.sha256(body).hexdigest(),
            content_type=kwargs["content_type"],
        )
        self.objects[path, version] = body
        self.latest[path] = ref
        return ref

    def find_staged(self, **kwargs: Any) -> S3ObjectRef | None:
        return self.latest.get(f"media-jobs/{kwargs['job_id']}/input/{kwargs['name']}")

    def download(self, ref: S3ObjectRef, **kwargs: Any) -> bytes:
        return self.objects[ref.key, ref.version_id]

    def run_sync(self, request: Any, **kwargs: Any) -> tuple[dict[str, bytes], dict[str, Any]]:
        self.requests.append(request)
        op = request.operation
        if op.kind == "tiktok_acquire":
            assert len(op.keywords) == 1 and op.n_per_kw == 100 and op.videos_per_kw == 0
            index = [
                "ブランドA",
                "ブランドB",
                "ブランドC",
                "朝のケア",
                "乾燥対策",
                "時短ケア",
            ].index(op.keywords[0])
            if self.failure == "timeout" and index == 1:
                raise MediaJobError("MEDIA_JOB_TIMEOUT")
            raw = inputs()[1][f"q{index}"]
            raw.update(
                ok=True, query=op.keywords[0], videos=raw["videos"][:12], stop_reason="exhausted"
            )
            accepted = not (self.failure in {"refused", "worker_timeout", "failed"} and index == 1)
            if not accepted:
                raw.update(
                    status="failed", stop_reason="refused", diagnostics={"captchaDetected": True}
                )
                if self.failure != "refused":
                    raw.update(
                        stop_reason="exception",
                        diagnostics={},
                        error_code="MEDIA_TIKTOK_TIMEOUT"
                        if self.failure == "worker_timeout"
                        else "TIKTOK_EXCEPTION",
                    )
            status = dict(
                query=raw["query"],
                accepted=accepted,
                status="partial" if accepted else "failed",
                target=100,
                actual=12,
                shortfall=88,
                stop_reason=raw["stop_reason"],
                diagnostics=raw["diagnostics"],
                raw_ref="sales25-raw.json",
            )
            files = {
                "sales25-raw.json": contract.json_bytes(raw),
                "acquisition-status.json": contract.json_bytes(status),
            }
        elif op.kind == "sales25_media":
            files = photos({"id": op.post_id, "url": op.url})
        elif op.kind == "sales25_pptx":
            with tempfile.TemporaryDirectory() as work:
                directory = Path(work).resolve()
                source = directory / "input.zip"
                source.write_bytes(self.download(op.source))
                render_sales25(directory, source, op.owner_id)
                files = {
                    name: (directory / name).read_bytes()
                    for name in ("deck.pptx", "sales25-qa.json", "sales25-qa.zip")
                }
        else:
            pytest.fail(f"unexpected operation {op.kind}")
        artifacts = tuple(
            MediaArtifact(
                name=name,
                object=self.stage_bytes(
                    job_id=request.job_id,
                    name=name,
                    body=body,
                    content_type=sales25.PPTX_TYPE
                    if name == "deck.pptx"
                    else "application/zip"
                    if name.endswith("zip")
                    else "application/json",
                ),
            )
            for name, body in files.items()
        )
        if kwargs.get("capture_result"):
            kwargs["capture_result"](
                MediaJobResult(job_id=request.job_id, status="done", artifacts=artifacts)
            )
        return files, {}


def model() -> Any:
    approved = fake_model()
    slide_text = approved.generate_text.side_effect

    def generate(prompt: str, request_id: str, **kwargs: Any) -> Any:
        if request_id.startswith("sales25-relevance-"):
            evidence = json.loads(prompt.split("Input:\n", 1)[1])
            return reply(
                {
                    "decisions": [
                        {
                            "id": p["id"],
                            "eligible": True,
                            "reason": "合成の関連投稿。",
                            "brand_matches": [evidence["query"]["group"]]
                            if evidence["query"]["role"] != "market_keyword"
                            else [],
                        }
                        for p in evidence["posts"]
                    ]
                }
            )
        return slide_text(prompt, request_id, **kwargs)

    approved.generate_text.side_effect = generate
    return approved


@pytest.mark.parametrize("omakase_request", [False, True])
def test_real_stages_deliver_25_pages_with_partial_and_restore(
    monkeypatch: pytest.MonkeyPatch,
    omakase_request: bool,
) -> None:
    from teamagent.skills.omiyage_report import input_check, omakase
    from tests.skills.omiyage_report.test_sales25_omakase import candidates

    store = ProposalJobStore(table_name="", memory={})
    client = Transport()
    approved = model()
    wire = Wire(client, lambda: approved, store=store)
    submitted = request()
    if omakase_request:
        monkeypatch.setattr(input_check, "bedrock_text_caller", lambda *a: candidates())
        submitted = request(competitors=[], keywords=["お任せ"], category="", sales25=None)
    accepted = build(store, sales25_stages=production_stages(wire)).run(submitted, ctx())
    saved = result(store, accepted.job_id)
    assert saved.delivery_status == "pending", saved.model_dump_json(indent=2)
    assert saved.status == "partial" and len(saved.checkpoints) == 12
    assert len([r for r in client.requests if r.operation.kind == "tiktok_acquire"]) == 6
    assert len([r for r in client.requests if r.operation.kind == "sales25_media"]) == 30
    assert saved.final_pptx_ref is not None
    deck = client.objects[saved.final_pptx_ref.key, saved.final_pptx_ref.version_id]
    presentation = Presentation(io.BytesIO(deck))
    assert len(presentation.slides) == 25
    with zipfile.ZipFile(io.BytesIO(deck)) as archive:
        text = "\n".join(
            node.text or ""
            for name in archive.namelist()
            if name.startswith("ppt/slides/slide") and name.endswith(".xml")
            for node in ElementTree.fromstring(archive.read(name)).iter(
                "{http://schemas.openxmlformats.org/drawingml/2006/main}t"
            )
        )
    assert "100本目標→12本取得" in text
    assert "数え方" in text
    if omakase_request:
        case = json.loads(store.get_job(accepted.job_id)["request_summary"])["case"]
        disclosure = omakase.note(case)
        assert disclosure in accepted.message
        with zipfile.ZipFile(io.BytesIO(deck)) as archive:
            assert disclosure in archive.read("ppt/slides/slide25.xml").decode()
            assert disclosure in archive.read("ppt/notesSlides/notesSlide25.xml").decode()
    # New parent and no local work directories: restore immutable final bytes only.
    slack = FakeSlack(store, accepted.job_id)
    parent = sales25.Parent(
        store, reader=lambda ref: client.objects[ref.key, ref.version_id], slack=slack
    )
    context = ctx()
    output = OmiyageReportStatusSkill(store=store, sales25_parent=parent).run(
        OmiyageReportStatusInput(job_id=accepted.job_id), context
    )
    monkeypatch.setattr(notify, "ProposalJobStore", lambda: store)
    assert notify.publish_notice(
        output.message,
        origin=context.metadata[ORIGIN_KEY],
        request_id="wire-fixture",
        job_id=accepted.job_id,
    )
    comment = slack.uploads[0]["initial_comment"]
    assert all(
        word in comment
        for word in ("partial", "100本目標→12本取得", "原音が未確認", "見た目（描画）は未確認")
    )
    assert slack.uploads[0]["data"] == deck
    assert result(store, accepted.job_id).delivery_status == "delivered"


@pytest.mark.parametrize(
    "failure,expected",
    [
        ("refused", "refused"),
        ("timeout", "timeout"),
        ("worker_timeout", "timeout"),
        ("failed", "incomplete"),
    ],
)
def test_real_failure_shapes_keep_all_searches_without_delivery(
    failure: str, expected: str
) -> None:
    store = ProposalJobStore(table_name="", memory={})
    client = Transport(failure)
    approved = model()
    accepted = build(
        store, sales25_stages=production_stages(Wire(client, lambda: approved, store=store))
    ).run(request(), ctx())
    saved = result(store, accepted.job_id)
    assert saved.checkpoints["search_2"].status == expected
    assert len(saved.checkpoints) == 6
    assert saved.delivery_status == "not_ready" and saved.final_pptx_ref is None
    assert len([r for r in client.requests if r.operation.kind == "tiktok_acquire"]) == 6
    assert not any(r.operation.kind == "sales25_pptx" for r in client.requests)
    # Completed companies may already have relevance evidence and early media.
    assert all(
        call.args[1].startswith("sales25-relevance-")
        for call in approved.generate_text.call_args_list
    )


def test_synonyms_persist_and_change_revision_identity() -> None:
    store = ProposalJobStore(table_name="", memory={})
    case, missing = sales25.preflight(
        store,
        request(
            brand="コープデリ",
            sales25=Sales25Intake(
                target_product="合成商品",
                claim_constraints="条件なし",
                brand_synonyms={"ブランドB": ["B系列"]},
            ),
        ),
        ctx(),
    )
    assert missing is None
    assert case["brand_synonyms"]["コープデリ"] == ["コープデリ", "コープ", "COOP", "生協"]
    assert case["brand_synonyms"]["ブランドB"] == ["ブランドB", "B系列"]


def test_mutated_persisted_evidence_is_rejected() -> None:
    from datetime import UTC, datetime

    client = Transport()
    wire = Wire(client)
    case = {
        "owner_id": "owner-fixture",
        "job_id": "omy-fixture",
        "started_at": datetime.now(UTC).isoformat(),
    }
    ref = wire._save_json(case, "evidence.json", {"actual": 12})
    obj = ref["object"]
    client.objects[obj["key"], obj["version_id"]] = b'{"actual":100}'
    with pytest.raises(ValueError, match="hash/size"):
        wire._json(case, ref)
    ref["owner_id"] = "another-owner"
    with pytest.raises(ValueError, match="owner mismatch"):
        wire._json(case, ref)


def test_restart_after_media_save_reuses_all_acquisition() -> None:
    class Restart(BaseException):
        pass

    class Interrupted(Wire):
        def run(self, stage: str, *args: Any) -> sales25.StepResult:
            completed = super().run(stage, *args)
            if stage == "media":
                raise Restart
            return completed

    store = ProposalJobStore(table_name="", memory={})
    client = Transport()
    approved = model()
    pending: list[Any] = []
    accepted = build(
        store,
        sales25_stages=production_stages(Interrupted(client, lambda: approved, store=store)),
        thread_launcher=lambda target, name: pending.append(target),
    ).run(request(), ctx())
    with pytest.raises(Restart):
        pending[0]()
    job_id = accepted.job_id
    row = store.get_job(job_id)
    assert row is not None
    assert "media" not in json.loads(row["checkpoint_json"])["steps"]
    assert len(client.requests) == 36
    parent = sales25.Parent(
        store,
        stages=production_stages(Wire(client, lambda: approved, store=store)),
        heartbeat_seconds=0,
    )
    parent.execute(job_id, row)
    assert result(store, job_id).delivery_status == "pending"
    assert len(client.requests) == 37  # Only the renderer was added after restart.
    assert (
        sum(
            call.args[1].startswith("sales25-relevance-")
            for call in approved.generate_text.call_args_list
        )
        == 6
    )


def test_model_limit_and_persisted_response_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime

    from teamagent.skills.omiyage_report.sales25_wire import _Model

    monkeypatch.setenv("OMIYAGE_SALES25_MODEL_CALLS_PER_STAGE", "1")
    approved = fake_model()
    approved.analyze_image_bytes.return_value = reply({"observation": "合成"})
    wire = Wire(Transport(), lambda: approved)
    case = {
        "owner_id": "owner-fixture",
        "job_id": "model-fixture",
        "started_at": datetime.now(UTC).isoformat(),
    }
    first = _Model(wire, case, "analysis")
    response = first.analyze_image_bytes(b"synthetic", "image/jpeg", "Observe", "one")
    with pytest.raises(TimeoutError, match="model call limit"):
        first.analyze_image_bytes(b"synthetic", "image/jpeg", "Observe", "two")
    resumed = _Model(wire, case, "analysis")
    assert resumed.analyze_image_bytes(b"synthetic", "image/jpeg", "Observe", "one") == response
    approved.analyze_image_bytes.assert_called_once()


def test_search_depth_setting_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    from teamagent.skills.omiyage_report import sales25

    monkeypatch.delenv("OMIYAGE_SALES25_SEARCH_DEPTH", raising=False)
    assert sales25.search_depth() == 100
    monkeypatch.setenv("OMIYAGE_SALES25_SEARCH_DEPTH", "50")
    assert sales25.search_depth() == 50
    for bad in ("5", "101"):
        monkeypatch.setenv("OMIYAGE_SALES25_SEARCH_DEPTH", bad)
        with pytest.raises(ValueError):
            sales25.search_depth()
