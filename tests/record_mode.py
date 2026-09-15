#!/usr/bin/env python3
"""Record-mode storage, identity, Git retention, and fail-open integration checks."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

from tauceti_worker.recording import (  # noqa: E402
    BlobStore,
    GitObjectStore,
    RecordConfig,
    RecordingError,
    RoadmapContextMaterializer,
    TaskRecorder,
    capture_id,
    resolve_record_dir,
    validate_record_store,
)

fails = 0


def check(name: str, condition: bool) -> None:
    global fails
    fails += not condition
    print(f"[{'OK ' if condition else 'BAD'}] {name}")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def make_repo(root: Path, name: str, *, dependencies: bool = False) -> tuple[Path, str]:
    repo = root / name
    git(root, "init", "-q", "-b", "main", str(repo))
    (repo / "README.md").write_text(f"# {name}\n")
    if dependencies:
        (repo / "lean-toolchain").write_text("leanprover/lean4:v4.24.0\n")
        (repo / "lake-manifest.json").write_text(
            json.dumps({"packages": [{"name": "mathlib", "rev": "a" * 40}]})
        )
    git(repo, "add", ".")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "initial")
    return repo, git(repo, "rev-parse", "HEAD")


def shallow_clone(source: Path, destination: Path) -> Path:
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", source.as_uri(), str(destination)],
        check=True,
    )
    return destination


def materialize(store: Path, retained_ref: str, destination: Path) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    git(destination.parent, "init", "-q", str(destination))
    git(destination, "fetch", "-q", "--no-tags", str(store), retained_ref)
    git(destination, "checkout", "-q", "--detach", "FETCH_HEAD")
    return git(destination, "rev-parse", "HEAD")


class FakeGitHub:
    repo = "TauCetiProject/TauCeti"

    def __init__(self, head: str, base: str):
        self.head = head
        self.base = base
        self.raced = False

    def pr_view(self, pr, fields):
        return {
            "number": pr,
            "title": "Fix the reviewed theorem",
            "body": "A real task",
            "url": f"https://example.invalid/pull/{pr}",
            "author": {"login": "alice"},
            "baseRefName": "main",
            "baseRefOid": self.base,
            "headRefName": "feature",
            "headRefOid": "f" * 40 if self.raced else self.head,
            "labels": [{"name": "reviewed"}],
            "state": "OPEN",
        }

    def issue_comments(self, pr):
        return [
            {
                "id": 1,
                "body": '<!--tauceti-scoreboard-->\n<!--tauceti-meta:v1 {"states":{"api":"blocking"}}-->',
                "user": {"login": "review-bot"},
            }
        ]

    def review_comments(self, pr):
        return [
            {
                "id": 2,
                "body": "Please repair this. <!--tauceti-rubric:api-->",
                "path": "TauCeti/Foo.lean",
                "line": 12,
                "user": {"login": "review-bot"},
            },
            {"id": 3, "body": "Acknowledged", "in_reply_to_id": 2, "user": {"login": "alice"}},
        ]

    def _gh(self, args):
        return subprocess.CompletedProcess(args, 0, "[]", "")

    def api_jq(self, path, jq):
        return self.base

    def pr_list(self, fields, *, author=None, state="open"):
        return [
            {
                "number": 9 if state == "open" else 8,
                "title": f"{state} context",
                "body": "bounded roadmap context",
                "url": "https://example.invalid/pr",
                "author": {"login": "bob"},
                "headRefOid": self.head,
                "baseRefName": "main",
                "labels": [{"name": "roadmap/Algebra"}],
                **({"mergedAt": "2026-08-20T00:00:00Z"} if state == "merged" else {}),
            }
        ]

    def issue_list(self, repo, *, labels=None, fields, state="open", limit=200):
        return []


with tempfile.TemporaryDirectory(prefix="record-mode-") as raw:
    tmp = Path(raw)
    store_root = tmp / "records"

    # Canonical identity is independent of operational provenance by construction.
    identity = {"schema": "v1", "phase": "fix", "head": "a" * 40}
    cid1, digest1 = capture_id(identity)
    cid2, digest2 = capture_id(dict(reversed(list(identity.items()))))
    check("canonical ID ignores object insertion order", (cid1, digest1) == (cid2, digest2))
    check("capture ID has the specified shape", len(cid1) == 27 and cid1.startswith("tc-"))

    blobs = BlobStore(store_root)
    first = blobs.put_bytes(b"same bytes", "text/plain")
    second = blobs.put_bytes(b"same bytes", "text/plain")
    check("content-addressed blobs deduplicate", first == second and blobs.verify(first))

    origin, base = make_repo(tmp, "TauCeti", dependencies=True)
    git(origin, "switch", "-qc", "feature")
    (origin / "feature.txt").write_text("captured\n")
    git(origin, "add", "feature.txt")
    git(origin, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "feature")
    head = git(origin, "rev-parse", "HEAD")
    git(origin, "update-ref", "refs/pull/7/head", head)

    objects = GitObjectStore(store_root, "TauCeti")
    retained = "refs/tauceti-record/tc-test/head"
    objects.retain(origin, head, retained, fetch_ref="refs/pull/7/head")
    git(origin, "update-ref", "-d", "refs/pull/7/head")
    git(origin, "switch", "-q", "main")
    git(origin, "branch", "-D", "feature")
    check("retained ref survives source-branch deletion", objects.verify(head, retained))
    check("retained object reconstructs captured files", objects.read_file(head, "feature.txt") == b"captured\n")

    damaged_origin, _ = make_repo(tmp, "DamagedHistory")
    (damaged_origin / "second.txt").write_text("second\n")
    git(damaged_origin, "add", ".")
    git(damaged_origin, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "second")
    damaged_head = git(damaged_origin, "rev-parse", "HEAD")
    damaged_parent = git(damaged_origin, "rev-parse", "HEAD^")
    damaged_store = GitObjectStore(store_root, "DamagedHistory")
    damaged_ref = "refs/tauceti-record/tc-damaged/main"
    damaged_store.retain(damaged_origin, damaged_head, damaged_ref)
    parent_object = damaged_store.path / "objects" / damaged_parent[:2] / damaged_parent[2:]
    parent_object.unlink()
    check("retained ref verification rejects missing reachable history", not damaged_store.verify(damaged_head, damaged_ref))

    prompt = tmp / "fix.md"
    prompt.write_text("Fix PR __PR__ as __AGENT__; wrappers: __BIN__.\n")
    gh = FakeGitHub(head, base)

    class FixtureRecorder(TaskRecorder):
        def _repo_remote(self, repo):
            return str(self.remotes.get(repo, origin))

    FixtureRecorder.remotes = {}

    recorder = FixtureRecorder(RecordConfig(store_root), gh, worker_root=REPO)
    check("a newly initialized empty store validates", validate_record_store(store_root) == [])
    kwargs = {
        "pr": 7,
        "expected_head": head,
        "worker_name": "worker1",
        "template_path": prompt,
        "rendered_base": "Fix PR 7 as TauCetiWorker; wrappers: scripts.\n",
        "logical_inputs": {"PR": 7, "AGENT": "TauCetiWorker", "BIN": "scripts"},
        "survey_metadata": {"reason": "blocking review"},
    }
    cid = recorder.capture_fix(**kwargs)
    capture = store_root / "captures" / cid
    check("fix capture is atomically complete", (capture / "capture.json").is_file() and (capture / "COMPLETE").is_file())
    manifest = json.loads((capture / "capture.json").read_text())
    check("capture declares materialized benchmark fidelity", manifest["fidelity"] == "materialized-context")
    check("dependency state preserves the resolved Mathlib revision", manifest["dependency_state"]["mathlib_rev"] == "a" * 40)
    check("prompt template and rendered base are independently retained", manifest["prompt"]["template"] != manifest["prompt"]["rendered_base"])

    # Timestamp and worker name are manifest provenance, not canonical identity.
    same = recorder.capture_fix(**{**kwargs, "worker_name": "worker2"})
    check("re-recording the same task reuses its capture ID", same == cid)

    roadmap_origin, _ = make_repo(tmp, "TauCetiRoadmap-origin")
    area_dir = roadmap_origin / "TauCetiRoadmap" / "Algebra"
    area_dir.mkdir(parents=True)
    (area_dir / "README.md").write_text("# Algebra roadmap\n")
    git(roadmap_origin, "add", ".")
    git(roadmap_origin, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "area")
    roadmap = shallow_clone(roadmap_origin, tmp / "TauCetiRoadmap")
    review_origin, _ = make_repo(tmp, "TauCetiReview-origin")
    (review_origin / "rubrics").mkdir()
    rubric_origin = review_origin / "rubrics" / "rubrics.md"
    rubric_origin.write_text("# Review rubric\n")
    git(review_origin, "add", ".")
    git(review_origin, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "rubric")
    review = shallow_clone(review_origin, tmp / "TauCetiReview")
    rubric = review / "rubrics" / "rubrics.md"
    recorder.remotes.update(
        {
            "TauCetiProject/TauCetiRoadmap": roadmap_origin,
            "TauCetiProject/TauCetiReview": review_origin,
        }
    )
    check(
        "roadmap and review task checkouts are genuinely shallow",
        git(roadmap, "rev-parse", "--is-shallow-repository") == "true"
        and git(review, "rev-parse", "--is-shallow-repository") == "true",
    )
    roadmap_prompt = tmp / "roadmap.md"
    roadmap_prompt.write_text("Implement __ONLY__; skip __SKIP__; context __ROADMAP_DIR__; __SOURCE_GUIDANCE__\n")
    roadmap_inputs = {
        "ONLY": "Algebra",
        "SKIP": "none",
        "ROADMAP_DIR": "context/roadmap/TauCetiRoadmap",
        "SOURCE_GUIDANCE": "",
    }
    roadmap_cid = recorder.capture_roadmap(
        area="Algebra",
        worker_name="worker1",
        template_path=roadmap_prompt,
        rendered_base="Implement Algebra; skip none; context context/roadmap/TauCetiRoadmap; \n",
        logical_inputs=roadmap_inputs,
        skip=[],
        claimed="none",
        survey_metadata={"candidate_reason": "Algebra"},
        roadmap_dir=roadmap,
        review_dir=review,
        rubric_bundle=rubric,
    )
    roadmap_manifest = json.loads(
        (store_root / "captures" / roadmap_cid / "capture.json").read_text(encoding="utf-8")
    )
    check("roadmap opportunity capture is complete", roadmap_manifest["capture_type"] == "roadmap-opportunity-raw")
    check(
        "roadmap capture retains all three required repositories",
        set(roadmap_manifest["repositories"]) == {"tauceti", "roadmap", "review"},
    )
    store_names = {"tauceti": "TauCeti.git", "roadmap": "TauCetiRoadmap.git", "review": "TauCetiReview.git"}
    imported = {
        name: materialize(
            store_root / "git" / store_names[name],
            repository["retained_ref"],
            tmp / "imports" / name,
        )
        for name, repository in roadmap_manifest["repositories"].items()
    }
    check(
        "fresh importer fetch and checkout succeeds for every roadmap repository",
        imported == {name: repository["sha"] for name, repository in roadmap_manifest["repositories"].items()},
    )
    check("store validator admits both complete captures", validate_record_store(store_root) == sorted([cid, roadmap_cid]))

    bounded_root = tmp / "bounded-roadmaps"
    bounded_dir = bounded_root / "TauCetiRoadmap"
    bounded_dir.mkdir(parents=True)
    for index in range(6):
        (bounded_dir / f"roadmap-{index}.md").write_text("x" * 900_000)
    bounded = RoadmapContextMaterializer(gh).materialize(
        area="Algebra",
        skip=[],
        claimed="none",
        survey_metadata={},
        roadmap_dir=bounded_root,
        rubric_bundle=None,
        source=None,
    )
    check("roadmap context above the old 5 MB bound is accepted", len(bounded["roadmap_files"]) == 6)
    for index in range(6, 18):
        (bounded_dir / f"roadmap-{index}.md").write_text("x" * 900_000)
    try:
        RoadmapContextMaterializer(gh).materialize(
            area="Algebra",
            skip=[],
            claimed="none",
            survey_metadata={},
            roadmap_dir=bounded_root,
            rubric_bundle=None,
            source=None,
        )
        check("roadmap context above the 16 MB bound is rejected", False)
    except RecordingError as exc:
        check(
            "roadmap context above the 16 MB bound is rejected",
            exc.code == "context_normalization_failed" and "16000000-byte aggregate limit" in str(exc),
        )

    class FailedGitHub(FakeGitHub):
        last_error = 'Unknown JSON field: "baseRefOid"'

        def pr_view(self, pr, fields):
            return None

    try:
        from tauceti_worker.recording import FixContextMaterializer

        FixContextMaterializer(FailedGitHub(head, base)).materialize(7, {})
        check("GitHub materialization exposes the underlying gh diagnostic", False)
    except RecordingError as exc:
        check(
            "GitHub materialization exposes the underlying gh diagnostic",
            exc.code == "github_read_failed" and "baseRefOid" in str(exc),
        )

    gh.raced = True
    before = set((store_root / "captures").iterdir())
    result = recorder.maybe_capture_fix(**kwargs)
    after = set((store_root / "captures").iterdir())
    check("a raced input fails open", result is None)
    check("a raced input leaves no apparently complete capture", before == after)
    error = json.loads((store_root / "errors" / "recording-errors.jsonl").read_text().splitlines()[-1])
    check("fail-open errors use stable codes", error["code"] == "input_raced")

    try:
        blobs.put_json({"github_token": "not-even-a-real-token"})
        check("secret fields are rejected", False)
    except RecordingError as exc:
        check("secret fields are rejected", exc.code == "secret_scan_failed")

    check(
        "CLI record directory overrides the environment",
        resolve_record_dir(tmp / "cli", {"TAUCETI_RECORD_DIR": str(tmp / "env")}) == (tmp / "cli").resolve(),
    )
    check(
        "environment enables recording when CLI is absent",
        resolve_record_dir(None, {"TAUCETI_RECORD_DIR": str(tmp / "env")}) == (tmp / "env").resolve(),
    )
    check("recording stays disabled when both are absent", resolve_record_dir(None, {}) is None)
    for blank in ("", " \t "):
        check(
            f"blank environment disables recording ({blank!r})",
            resolve_record_dir(None, {"TAUCETI_RECORD_DIR": blank}) is None,
        )
        check(
            f"CLI enables recording over a blank environment ({blank!r})",
            resolve_record_dir(tmp / "cli", {"TAUCETI_RECORD_DIR": blank}) == (tmp / "cli").resolve(),
        )
        try:
            resolve_record_dir(blank, {"TAUCETI_RECORD_DIR": str(tmp / "env")})
            check(f"explicit blank CLI directory is rejected ({blank!r})", False)
        except RecordingError as exc:
            check(
                f"explicit blank CLI directory is rejected ({blank!r})",
                exc.code == "storage_failed" and "must not be empty" in str(exc),
            )

    # The disabled dispatch seam must not even construct a recorder.
    import tauceti_worker.work_units as work_units

    original_recorder = work_units.TaskRecorder
    work_units.TaskRecorder = lambda *args, **values: (_ for _ in ()).throw(AssertionError("constructed"))
    try:
        disabled = work_units._task_recorder(
            types.SimpleNamespace(gh=None), types.SimpleNamespace(record_dir=None), "fix", 7
        )
        check("disabled mode constructs no recorder", disabled is None)
    finally:
        work_units.TaskRecorder = original_recorder

    from tauceti_worker.cli import build_parser
    from tauceti_worker.worker_manager import WorkerSpec

    parsed = build_parser().parse_args(["work", "--record-dir", str(tmp / "cli")])
    check("work CLI accepts --record-dir", parsed.record_dir == str(tmp / "cli"))
    parsed_records = build_parser().parse_args(["records", "validate", str(store_root)])
    check(
        "records validate CLI accepts a store path",
        parsed_records.records_cmd == "validate" and parsed_records.path == store_root,
    )
    spec = WorkerSpec(id="recorder", record_dir=str(tmp / "persistent"))
    check("persistent workers forward record_dir", spec.work_argv()[-2:] == ["--record-dir", str(tmp / "persistent")])

raise SystemExit(bool(fails))
