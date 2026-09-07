"""Pre-agent, treatment-neutral task capture for TauCetiBench.

The recorder deliberately lives outside the model/provider path.  It materializes a bounded
allow-list of GitHub context, stores non-Git inputs by content hash, and keeps source commits alive
with private refs in shared bare repositories.  A capture is importable only after ``COMPLETE`` is
written, and every public entry point is fail-open for the live worker.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import is_git_url, log, warn_red
from .constants import REVIEW, ROADMAP, TAUCETI
from .paths import HERE

CAPTURE_SCHEMA = "tauceti.record.capture/v1"
RECORDER_VERSION = "1"
FORMAT = {"schema": "tauceti.record.store/v1", "capture_schema": CAPTURE_SCHEMA}
OPEN_PR_LIMIT = 100
MERGED_PR_LIMIT = 50
ROADMAP_FILE_MAX_BYTES = 1_000_000
ROADMAP_TOTAL_MAX_BYTES = 16_000_000

_SECRET_KEYS = {
    "anthropic_api_key",
    "api_key",
    "authorization",
    "aws_access_key_id",
    "aws_secret_access_key",
    "codex_api_key",
    "cookie",
    "github_token",
    "gh_token",
    "kiro_api_key",
    "openai_api_key",
    "openrouter_api_key",
    "openrouter_management_key",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "token",
}
_SECRET_VALUE_RE = re.compile(
    r"(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|sk-ant-[A-Za-z0-9_-]{20,}|"
    r"sk-proj-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----)"
)
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")


class RecordingError(RuntimeError):
    """A stable recorder failure suitable for the JSONL operational log."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RecordConfig:
    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.expanduser().resolve())


def canonical_json(value: Any) -> bytes:
    """The sole canonical JSON encoding used for identities and content blobs."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def capture_id(identity: dict[str, Any]) -> tuple[str, str]:
    digest = sha256_bytes(canonical_json(identity))
    return f"tc-{digest[:24]}", digest


def _blob_ref(digest: str, size: int, media_type: str) -> dict[str, Any]:
    return {"algorithm": "sha256", "digest": digest, "size_bytes": size, "media_type": media_type}


def _utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def record_failure(config: RecordConfig, phase: str, candidate: Any, exc: Exception) -> None:
    """Append one allow-listed operational error and emit the matching structured warning."""
    code = exc.code if isinstance(exc, RecordingError) else "storage_failed"
    record = {
        "schema": "tauceti.record.error/v1",
        "occurred_at": _utc_now(),
        "phase": phase,
        "candidate": candidate,
        "code": code,
        "message": str(exc)[:2000],
    }
    try:
        errors = config.root / "errors"
        errors.mkdir(parents=True, exist_ok=True)
        fd = os.open(errors / "recording-errors.jsonl", os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            os.write(fd, canonical_json(record) + b"\n")
        finally:
            os.close(fd)
    except OSError:
        pass
    warn_red(f"recording warning: {canonical_json(record).decode('utf-8')}")


def _run(argv: list[str], *, cwd: Path | None = None, code: str = "storage_failed") -> str:
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    except OSError as exc:
        raise RecordingError(code, f"could not run {argv[0]}: {exc}") from exc
    if proc.returncode:
        detail = (proc.stderr or proc.stdout or "no diagnostic").strip()[-1000:]
        raise RecordingError(code, f"{' '.join(argv[:4])} failed: {detail}")
    return (proc.stdout or "").strip()


def _process_detail(proc: subprocess.CompletedProcess) -> str:
    """Bound a subprocess diagnostic before it reaches the operational error log."""
    return (proc.stderr or proc.stdout or "no diagnostic").strip()[-1000:]


def _github_read_error(github: Any, message: str) -> RecordingError:
    detail = str(getattr(github, "last_error", "") or "").strip()[-1000:]
    return RecordingError("github_read_failed", f"{message}: {detail}" if detail else message)


def _git_sha(repo: Path, ref: str = "HEAD", *, required: bool = True) -> str | None:
    try:
        out = _run(["git", "-C", str(repo), "rev-parse", f"{ref}^{{commit}}"], code="git_object_missing")
    except RecordingError:
        if not required:
            return None
        raise
    if not _SHA_RE.fullmatch(out):
        if required:
            raise RecordingError("git_object_missing", f"{repo}: {ref} did not resolve to a commit")
        return None
    return out.lower()


def _scan_for_secrets(value: Any, *, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _SECRET_KEYS:
                raise RecordingError("secret_scan_failed", f"secret-like field rejected at {path}.{key}")
            _scan_for_secrets(item, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _scan_for_secrets(item, path=f"{path}[{index}]")
    elif isinstance(value, str) and _SECRET_VALUE_RE.search(value):
        raise RecordingError("secret_scan_failed", f"secret-like value rejected at {path}")


class BlobStore:
    """Content-addressed non-Git storage shared by every capture."""

    def __init__(self, root: Path):
        self.root = root / "blobs" / "sha256"

    def path(self, digest: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise RecordingError("validation_failed", f"invalid SHA-256 digest: {digest!r}")
        return self.root / digest[:2] / digest[2:]

    def put_bytes(self, data: bytes, media_type: str = "application/octet-stream") -> dict[str, Any]:
        digest = sha256_bytes(data)
        target = self.path(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.read_bytes() != data:
                raise RecordingError("identity_collision", f"blob digest collision at {target}")
        else:
            fd, raw = tempfile.mkstemp(prefix=".blob-", dir=target.parent)
            tmp = Path(raw)
            try:
                with os.fdopen(fd, "wb") as dst:
                    dst.write(data)
                    dst.flush()
                    os.fsync(dst.fileno())
                try:
                    os.link(tmp, target)
                except FileExistsError:
                    if target.read_bytes() != data:
                        raise RecordingError("identity_collision", f"blob digest collision at {target}") from None
            finally:
                tmp.unlink(missing_ok=True)
        return _blob_ref(digest, len(data), media_type)

    def put_json(self, value: Any) -> dict[str, Any]:
        _scan_for_secrets(value)
        return self.put_bytes(canonical_json(value), "application/json")

    def verify(self, ref: dict[str, Any]) -> bool:
        path = self.path(str(ref.get("digest", "")))
        try:
            data = path.read_bytes()
        except OSError:
            return False
        return len(data) == ref.get("size_bytes") and sha256_bytes(data) == ref.get("digest")


class GitObjectStore:
    """One shared bare object database for a source repository."""

    def __init__(self, root: Path, name: str):
        if Path(name).is_absolute() or ".." in Path(name).parts:
            raise RecordingError("storage_failed", f"unsafe Git store name: {name!r}")
        suffix = name if name.endswith(".git") else f"{name}.git"
        self.path = root / "git" / suffix

    def ensure(self) -> None:
        if self.path.exists():
            if not (self.path / "HEAD").is_file():
                raise RecordingError("storage_failed", f"not a bare Git repository: {self.path}")
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _run(["git", "init", "--bare", str(self.path)], code="storage_failed")

    def _git(self, *args: str, code: str = "git_object_missing") -> str:
        self.ensure()
        return _run(["git", "--git-dir", str(self.path), *args], code=code)

    def has_commit(self, sha: str) -> bool:
        self.ensure()
        proc = subprocess.run(
            ["git", "--git-dir", str(self.path), "cat-file", "-e", f"{sha}^{{commit}}"],
            capture_output=True,
        )
        return proc.returncode == 0

    def has_connected_history(self, sha: str) -> bool:
        """Whether Git can traverse every object reachable from *sha*."""
        if not self.has_commit(sha):
            return False
        proc = subprocess.run(
            ["git", "--git-dir", str(self.path), "rev-list", "--objects", sha],
            capture_output=True,
        )
        return proc.returncode == 0

    def retain(self, remote: str | Path, sha: str, retained_ref: str, *, fetch_ref: str | None = None) -> None:
        """Fetch *sha* if needed, point *retained_ref* at it, and verify the exact target."""
        if not _SHA_RE.fullmatch(sha):
            raise RecordingError("git_object_missing", f"invalid commit SHA: {sha!r}")
        if not retained_ref.startswith("refs/tauceti-record/"):
            raise RecordingError("retained_ref_failed", f"invalid retained ref: {retained_ref}")
        self.ensure()
        if not self.has_commit(sha):
            staging = f"refs/tauceti-record-staging/{uuid.uuid4().hex}"
            refspec = f"+{fetch_ref}:{staging}" if fetch_ref else sha
            try:
                self._git("fetch", "--no-tags", "--force", str(remote), refspec, code="git_fetch_failed")
                if fetch_ref:
                    actual = self._git("rev-parse", f"{staging}^{{commit}}")
                    if actual.lower() != sha.lower():
                        raise RecordingError(
                            "input_raced", f"{fetch_ref} moved during archival: expected {sha}, got {actual}"
                        )
            finally:
                subprocess.run(
                    ["git", "--git-dir", str(self.path), "update-ref", "-d", staging], capture_output=True
                )
        if not self.has_commit(sha):
            raise RecordingError("git_object_missing", f"fetched object is not a commit: {sha}")
        self._git("update-ref", retained_ref, sha, code="retained_ref_failed")
        actual = self._git("rev-parse", f"{retained_ref}^{{commit}}", code="retained_ref_failed")
        if actual.lower() != sha.lower():
            raise RecordingError("retained_ref_failed", f"{retained_ref} points to {actual}, expected {sha}")

    def verify(self, sha: str, retained_ref: str) -> bool:
        try:
            return (
                self._git("rev-parse", f"{retained_ref}^{{commit}}").lower() == sha.lower()
                and self.has_connected_history(sha)
            )
        except RecordingError:
            return False

    def read_file(self, sha: str, path: str) -> bytes:
        self.ensure()
        proc = subprocess.run(
            ["git", "--git-dir", str(self.path), "show", f"{sha}:{path}"], capture_output=True
        )
        if proc.returncode:
            raise RecordingError("git_object_missing", f"{path} is unavailable at {sha}")
        return proc.stdout

    def merge_base(self, left: str, right: str) -> str:
        return self._git("merge-base", left, right)


@dataclass(frozen=True)
class PromptCapture:
    source_path: str
    template: dict[str, Any]
    rendered_base: dict[str, Any]
    logical_inputs: dict[str, Any]

    @classmethod
    def store(
        cls,
        blobs: BlobStore,
        template_path: Path,
        rendered_base: str,
        logical_inputs: dict[str, Any],
    ) -> PromptCapture:
        try:
            template = template_path.read_bytes()
        except OSError as exc:
            raise RecordingError("prompt_capture_failed", f"cannot read {template_path}: {exc}") from exc
        _scan_for_secrets(logical_inputs)
        _scan_for_secrets(rendered_base)
        try:
            source_path = str(template_path.relative_to(HERE))
        except ValueError:
            source_path = template_path.name
        return cls(
            source_path=source_path,
            template=blobs.put_bytes(template, "text/markdown; charset=utf-8"),
            rendered_base=blobs.put_bytes(rendered_base.encode("utf-8"), "text/markdown; charset=utf-8"),
            logical_inputs=blobs.put_json(logical_inputs),
        )

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class CaptureBuilder:
    """Atomically publish immutable manifests after all external objects verify."""

    def __init__(self, config: RecordConfig, blobs: BlobStore):
        self.config = config
        self.blobs = blobs

    def seal(self, identity: dict[str, Any], manifest: dict[str, Any]) -> str:
        cid, identity_digest = capture_id(identity)
        if manifest.get("capture_id") not in (None, cid):
            raise RecordingError("validation_failed", "manifest capture ID disagrees with canonical identity")
        manifest = {**manifest, "capture_id": cid}
        manifest.setdefault("integrity", {})["canonical_identity_sha256"] = identity_digest
        _scan_for_secrets(manifest)
        self._verify_blob_refs(manifest)

        captures = self.config.root / "captures"
        captures.mkdir(parents=True, exist_ok=True)
        target = captures / cid
        if target.exists():
            if self._existing_matches(target, identity_digest):
                return cid
            raise RecordingError("identity_collision", f"capture directory already differs: {target}")

        tmp = Path(tempfile.mkdtemp(prefix=f".{cid}-", dir=captures))
        try:
            payload = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            (tmp / "capture.json").write_text(payload, encoding="utf-8")
            (tmp / "COMPLETE").write_bytes(b"")
            try:
                tmp.rename(target)
            except FileExistsError:
                if self._existing_matches(target, identity_digest):
                    return cid
                raise RecordingError("identity_collision", f"capture concurrently differed: {target}") from None
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        return cid

    def _existing_matches(self, target: Path, identity_digest: str) -> bool:
        try:
            existing = json.loads((target / "capture.json").read_text(encoding="utf-8"))
            if not (target / "COMPLETE").is_file() or existing.get("capture_id") != target.name:
                return False
            if existing.get("integrity", {}).get("canonical_identity_sha256") != identity_digest:
                return False
            self._verify_blob_refs(existing)
            for name, repository in existing.get("repositories", {}).items():
                if name == "tauceti" and "pr_head_sha" in repository:
                    store = GitObjectStore(self.config.root, "TauCeti")
                    if not store.verify(repository["pr_head_sha"], repository["head_retained_ref"]):
                        return False
                    base = repository["base"]
                    if not store.verify(base["sha"], base["retained_ref"]):
                        return False
                    continue
                store_name = {
                    "tauceti": "TauCeti",
                    "roadmap": "TauCetiRoadmap",
                    "review": "TauCetiReview",
                }.get(name, repository.get("store"))
                if not store_name or not GitObjectStore(self.config.root, store_name).verify(
                    repository["sha"], repository["retained_ref"]
                ):
                    return False
            return True
        except (OSError, json.JSONDecodeError, AttributeError, KeyError, RecordingError):
            return False

    def _verify_blob_refs(self, value: Any) -> None:
        if isinstance(value, dict):
            if value.get("algorithm") == "sha256" and "digest" in value:
                if not self.blobs.verify(value):
                    raise RecordingError("validation_failed", f"missing or corrupt blob {value.get('digest')}")
                return
            for item in value.values():
                self._verify_blob_refs(item)
        elif isinstance(value, list):
            for item in value:
                self._verify_blob_refs(item)


def _user(value: Any) -> str | None:
    if isinstance(value, dict):
        return value.get("login") or value.get("name")
    return value if isinstance(value, str) else None


def _normalize_comment(raw: dict[str, Any]) -> dict[str, Any]:
    allowed = (
        "id",
        "body",
        "created_at",
        "updated_at",
        "path",
        "line",
        "original_line",
        "side",
        "start_line",
        "start_side",
        "in_reply_to_id",
        "commit_id",
        "original_commit_id",
        "author_association",
        "state",
        "submitted_at",
    )
    out = {key: raw[key] for key in allowed if raw.get(key) is not None}
    author = _user(raw.get("user") or raw.get("author"))
    if author:
        out["author"] = author
    return out


def _normalize_comments(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted((_normalize_comment(item) for item in values), key=lambda item: (item.get("id", 0), item.get("body", "")))


class FixContextMaterializer:
    """Fixed-schema GitHub context for ``fix-review-raw`` captures."""

    def __init__(self, github):
        self.github = github

    def _reviews(self, pr: int) -> list[dict[str, Any]]:
        runner = getattr(self.github, "_gh", None)
        if runner is None:
            return []
        proc = runner(["api", "--paginate", f"/repos/{self.github.repo}/pulls/{pr}/reviews?per_page=100"])
        if proc.returncode:
            raise RecordingError(
                "github_read_failed", f"could not read submitted reviews for PR #{pr}: {_process_detail(proc)}"
            )
        try:
            return json.loads(proc.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise RecordingError("context_normalization_failed", "submitted reviews were not JSON") from exc

    def _threads(self, pr: int, fallback: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Read review-thread resolution, which the REST comment endpoint does not expose."""
        runner = getattr(self.github, "_gh", None)
        if runner is not None:
            owner, _, name = self.github.repo.partition("/")
            query = (
                "query($owner:String!,$name:String!,$pr:Int!){repository(owner:$owner,name:$name){"
                "pullRequest(number:$pr){reviewThreads(first:100){totalCount nodes{isResolved isOutdated "
                "path line startLine diffSide comments(first:100){totalCount nodes{databaseId body createdAt "
                "updatedAt author{login}replyTo{databaseId}commit{oid}originalCommit{oid}}}}}}}}"
            )
            proc = runner(
                [
                    "api",
                    "graphql",
                    "-f",
                    f"query={query}",
                    "-F",
                    f"owner={owner}",
                    "-F",
                    f"name={name}",
                    "-F",
                    f"pr={pr}",
                ]
            )
            if proc.returncode:
                raise RecordingError(
                    "github_read_failed", f"could not read review-thread status for PR #{pr}: {_process_detail(proc)}"
                )
            if proc.returncode == 0:
                try:
                    payload = json.loads(proc.stdout)
                    threads = payload["data"]["repository"]["pullRequest"]["reviewThreads"]
                    if threads["totalCount"] > 100:
                        raise RecordingError("context_normalization_failed", "PR has more than 100 review threads")
                    normalized = []
                    for thread in threads["nodes"]:
                        comments = thread["comments"]
                        if comments["totalCount"] > 100:
                            raise RecordingError(
                                "context_normalization_failed", "a review thread has more than 100 comments"
                            )
                        items = []
                        for comment in comments["nodes"]:
                            items.append(
                                {
                                    "id": comment.get("databaseId"),
                                    "body": comment.get("body") or "",
                                    "author": _user(comment.get("author")),
                                    "created_at": comment.get("createdAt"),
                                    "updated_at": comment.get("updatedAt"),
                                    "in_reply_to_id": (comment.get("replyTo") or {}).get("databaseId"),
                                    "commit_id": (comment.get("commit") or {}).get("oid"),
                                    "original_commit_id": (comment.get("originalCommit") or {}).get("oid"),
                                }
                            )
                        normalized.append(
                            {
                                "is_resolved": bool(thread.get("isResolved")),
                                "is_outdated": bool(thread.get("isOutdated")),
                                "path": thread.get("path"),
                                "line": thread.get("line"),
                                "start_line": thread.get("startLine"),
                                "diff_side": thread.get("diffSide"),
                                "comments": items,
                            }
                        )
                    return normalized
                except (json.JSONDecodeError, KeyError, TypeError):
                    pass
        # Lightweight test clients and old gh installations still yield an honest fallback: status is
        # unknown rather than incorrectly inferring that a reply resolves a thread.
        roots = [item for item in fallback if item.get("in_reply_to_id") is None]
        return [
            {
                "is_resolved": None,
                "path": root.get("path"),
                "line": root.get("line"),
                "comments": [root, *[item for item in fallback if item.get("in_reply_to_id") == root.get("id")]],
            }
            for root in roots
        ]

    def materialize(self, pr: int, survey_metadata: dict[str, Any]) -> dict[str, Any]:
        fields = [
            "number",
            "title",
            "body",
            "url",
            "author",
            "baseRefName",
            "baseRefOid",
            "headRefName",
            "headRefOid",
            "labels",
            "state",
        ]
        raw_pr = self.github.pr_view(pr, fields)
        if raw_pr is None:
            raise _github_read_error(self.github, f"could not read PR metadata for PR #{pr}")
        issues = self.github.issue_comments(pr)
        if issues is None:
            raise _github_read_error(self.github, f"could not read issue comments for PR #{pr}")
        inline = self.github.review_comments(pr)
        if inline is None:
            raise _github_read_error(self.github, f"could not read review comments for PR #{pr}")
        normalized_pr = {
            "number": raw_pr.get("number", pr),
            "title": raw_pr.get("title") or "",
            "body": raw_pr.get("body") or "",
            "url": raw_pr.get("url") or f"https://github.com/{self.github.repo}/pull/{pr}",
            "author": _user(raw_pr.get("author")),
            "base_branch": raw_pr.get("baseRefName"),
            "base_sha": raw_pr.get("baseRefOid"),
            "head_branch": raw_pr.get("headRefName"),
            "head_sha": raw_pr.get("headRefOid"),
            "labels": sorted(
                item.get("name", "") if isinstance(item, dict) else str(item) for item in (raw_pr.get("labels") or [])
            ),
            "state": raw_pr.get("state"),
        }
        issue_comments = _normalize_comments(issues)
        review_comments = _normalize_comments(inline)
        review_threads = self._threads(pr, review_comments)
        reviews = _normalize_comments(self._reviews(pr))
        scoreboards = [item for item in issue_comments if "<!--tauceti-scoreboard-->" in item.get("body", "")]
        unresolved = [
            thread
            for thread in review_threads
            if thread.get("is_resolved") is not True
            and thread.get("comments")
            and (
                "tauceti-rubric:" in thread["comments"][0].get("body", "")
                or "request" in thread["comments"][0].get("body", "").lower()
            )
        ]
        return {
            "pr": normalized_pr,
            "issue_comments": issue_comments,
            "submitted_reviews": reviews,
            "review_threads": review_threads,
            "scoreboard": scoreboards[-1] if scoreboards else None,
            "unresolved_findings": {
                "algorithm": "tauceti.record.unresolved-findings/v1",
                "findings": unresolved,
            },
            "survey_metadata": survey_metadata,
        }


class RoadmapContextMaterializer:
    """Bounded, versioned opportunity context for roadmap captures."""

    def __init__(self, github):
        self.github = github

    def _prs(self, state: str, limit: int) -> list[dict[str, Any]]:
        fields = ["number", "title", "body", "url", "author", "headRefOid", "baseRefName", "labels", "mergedAt"]
        try:
            values = self.github.pr_list(fields, state=state)
        except Exception as exc:
            raise RecordingError("github_read_failed", f"could not read {state} PR snapshot: {exc}") from exc
        out = []
        for item in values[:limit]:
            out.append(
                {
                    "number": item.get("number"),
                    "title": item.get("title") or "",
                    "body": item.get("body") or "",
                    "url": item.get("url"),
                    "author": _user(item.get("author")),
                    "head_sha": item.get("headRefOid"),
                    "base_branch": item.get("baseRefName"),
                    "labels": sorted(
                        label.get("name", "") if isinstance(label, dict) else str(label)
                        for label in (item.get("labels") or [])
                    ),
                    **({"merged_at": item.get("mergedAt")} if item.get("mergedAt") else {}),
                }
            )
        return out

    def _intentions(self, area: str) -> list[dict[str, Any]]:
        labels = ["intention"] + ([f"roadmap/{area}"] if area not in ("", "any") else [])
        try:
            values = self.github.issue_list(
                ROADMAP,
                labels=labels,
                fields=["number", "url", "title", "body", "assignees", "labels", "state"],
                state="open",
                limit=200,
            )
        except Exception as exc:
            raise RecordingError("github_read_failed", f"could not read roadmap intentions: {exc}") from exc
        return [
            {
                "number": item.get("number"),
                "url": item.get("url"),
                "title": item.get("title") or "",
                "body": item.get("body") or "",
                "assignees": sorted(filter(None, (_user(user) for user in (item.get("assignees") or [])))),
                "labels": sorted(
                    label.get("name", "") if isinstance(label, dict) else str(label)
                    for label in (item.get("labels") or [])
                ),
                "state": item.get("state"),
            }
            for item in values
        ]

    def materialize(
        self,
        *,
        area: str,
        skip: list[str],
        claimed: str,
        survey_metadata: dict[str, Any],
        roadmap_dir: Path,
        rubric_bundle: Path | None,
        source: Any | None,
    ) -> dict[str, Any]:
        files: dict[str, str] = {}
        roadmaps = roadmap_dir / "TauCetiRoadmap"
        total = 0
        if roadmaps.is_dir():
            for path in sorted(roadmaps.rglob("*")):
                if not path.is_file() or path.suffix not in (".md", ".lean"):
                    continue
                size = path.stat().st_size
                if size > ROADMAP_FILE_MAX_BYTES:
                    raise RecordingError(
                        "context_normalization_failed",
                        f"roadmap file {path.relative_to(roadmap_dir)} is {size} bytes; "
                        f"the per-file limit is {ROADMAP_FILE_MAX_BYTES}",
                    )
                if total + size > ROADMAP_TOTAL_MAX_BYTES:
                    raise RecordingError(
                        "context_normalization_failed",
                        f"roadmap context is larger than the {ROADMAP_TOTAL_MAX_BYTES}-byte aggregate limit",
                    )
                files[str(path.relative_to(roadmap_dir))] = path.read_text(encoding="utf-8", errors="replace")
                total += size
        rubric = rubric_bundle.read_text(encoding="utf-8") if rubric_bundle and rubric_bundle.is_file() else None
        return {
            "schema": "tauceti.record.roadmap-context/v1",
            "designated_area": area,
            "skip_targets": sorted(skip),
            "claims_and_holds": claimed,
            "intentions": self._intentions(area),
            "roadmap_files": files,
            "open_prs": {"limit": OPEN_PR_LIMIT, "items": self._prs("open", OPEN_PR_LIMIT)},
            "recently_merged_prs": {"limit": MERGED_PR_LIMIT, "items": self._prs("merged", MERGED_PR_LIMIT)},
            "survey_metadata": survey_metadata,
            "rubric_bundle": rubric,
            **({"source_repository": source} if source else {}),
        }


class TaskRecorder:
    """High-level fail-open capture hooks used by fix and roadmap dispatch."""

    def __init__(self, config: RecordConfig, github, *, worker_root: Path = HERE):
        self.config = config
        self.github = github
        self.worker_root = worker_root
        self.blobs = BlobStore(config.root)
        self.builder = CaptureBuilder(config, self.blobs)
        self._initialize()

    def _initialize(self) -> None:
        self.config.root.mkdir(parents=True, exist_ok=True)
        expected = canonical_json(FORMAT)
        path = self.config.root / "format.json"
        if path.exists():
            try:
                if canonical_json(json.loads(path.read_text(encoding="utf-8"))) != expected:
                    raise RecordingError("validation_failed", f"unsupported record store format at {path}")
            except json.JSONDecodeError as exc:
                raise RecordingError("validation_failed", f"invalid record store format at {path}") from exc
        else:
            path.write_text(json.dumps(FORMAT, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (self.config.root / "captures").mkdir(parents=True, exist_ok=True)

    def _worker_sha(self) -> str:
        sha = _git_sha(self.worker_root, required=False)
        if sha is None:
            raise RecordingError("prompt_capture_failed", "TauCetiWorker commit SHA is unavailable")
        return sha

    def _store_context(self, value: dict[str, Any]) -> dict[str, Any]:
        refs: dict[str, Any] = {}
        for key, item in value.items():
            if item is not None:
                refs[key] = self.blobs.put_json(item)
        return refs

    def _dependency_state(self, store: GitObjectStore, sha: str) -> dict[str, Any]:
        toolchain = store.read_file(sha, "lean-toolchain")
        manifest = store.read_file(sha, "lake-manifest.json")
        try:
            parsed = json.loads(manifest)
        except json.JSONDecodeError as exc:
            raise RecordingError("context_normalization_failed", "lake-manifest.json is not valid JSON") from exc
        packages = parsed.get("packages") or []
        mathlib = next((p for p in packages if p.get("name") == "mathlib"), {})
        mathlib_rev = mathlib.get("rev") or mathlib.get("commit") or mathlib.get("gitRevision")
        return {
            "lean_toolchain": self.blobs.put_bytes(toolchain, "text/plain; charset=utf-8"),
            "lake_manifest": self.blobs.put_bytes(manifest, "application/json"),
            **({"mathlib_rev": mathlib_rev} if mathlib_rev else {}),
        }

    def _repo_remote(self, repo: str) -> str:
        return f"https://github.com/{repo}.git"

    def _archive_remote(self, name: str, checkout: Path) -> str | Path:
        """Return an ancestry-complete source for a repository snapshot.

        Roadmap, Review, and URL-backed supplementary checkouts are deliberately shallow in the
        live worker. Fetching from those checkouts copies a commit whose parents are absent into the
        record store, producing a bare repository that cannot serve the documented importer fetch.
        Archive known repositories, and any supplementary checkout with a network origin, from the
        authoritative remote instead. A local-only repository is acceptable only when it is complete.
        """
        known = {"roadmap": ROADMAP, "review": REVIEW}
        if name in known:
            return self._repo_remote(known[name])
        try:
            remote = _run(
                ["git", "-C", str(checkout), "config", "--get", "remote.origin.url"],
                code="unsupported_source",
            )
        except RecordingError:
            remote = ""
        if remote and is_git_url(remote) and not remote.lower().startswith("file:"):
            return remote
        shallow = _run(
            ["git", "-C", str(checkout), "rev-parse", "--is-shallow-repository"],
            code="unsupported_source",
        )
        if shallow == "true":
            raise RecordingError(
                "unsupported_source",
                f"{name} is shallow and has no ancestry-complete network origin",
            )
        return checkout

    def _source_description(self, source: str | None, source_dir: Path | None) -> dict[str, str] | None:
        if source is None:
            return None
        if is_git_url(source) and not source.lower().startswith("file:"):
            return {"kind": "git-url", "url": source}
        if source_dir is not None:
            try:
                remote = _run(
                    ["git", "-C", str(source_dir), "config", "--get", "remote.origin.url"],
                    code="unsupported_source",
                )
            except RecordingError:
                remote = ""
            if remote and is_git_url(remote) and not remote.lower().startswith("file:"):
                return {"kind": "git-url", "url": remote}
        return {"kind": "local-git", "name": Path(source).name}

    def _resolve_branch_sha(self, branch: str) -> str:
        value = self.github.api_jq(f"repos/{TAUCETI}/commits/{branch}", ".sha")
        if not value or not _SHA_RE.fullmatch(value):
            raise RecordingError("github_read_failed", f"could not resolve {TAUCETI}:{branch}")
        return value.lower()

    def maybe_capture_fix(self, **kwargs: Any) -> str | None:
        try:
            cid = self.capture_fix(**kwargs)
            log(f"  record fix: {cid}")
            return cid
        except Exception as exc:
            self._failure("fix", kwargs.get("pr"), exc)
            return None

    def capture_fix(
        self,
        *,
        pr: int,
        expected_head: str,
        worker_name: str,
        template_path: Path,
        rendered_base: str,
        logical_inputs: dict[str, Any],
        survey_metadata: dict[str, Any],
        auxiliary_repositories: dict[str, Path] | None = None,
    ) -> str:
        context = FixContextMaterializer(self.github).materialize(pr, survey_metadata)
        pr_doc = context["pr"]
        head = str(pr_doc.get("head_sha") or expected_head).lower()
        if head != expected_head.lower():
            raise RecordingError("input_raced", f"PR #{pr} head changed from {expected_head} to {head}")
        base_branch = str(pr_doc.get("base_branch") or "main")
        base_sha = str(pr_doc.get("base_sha") or self._resolve_branch_sha(base_branch)).lower()

        prompt = PromptCapture.store(self.blobs, template_path, rendered_base, logical_inputs)
        context_refs = self._store_context(context)
        worker_sha = self._worker_sha()
        auxiliary_shas = {
            name: sha
            for name, path in sorted((auxiliary_repositories or {}).items())
            if (sha := _git_sha(path, required=False)) is not None
        }
        identity: dict[str, Any] = {
            "schema": CAPTURE_SCHEMA,
            "phase": "fix",
            "capture_type": "fix-review-raw",
            "candidate": {"repository": TAUCETI, "pr": pr},
            "git": {"head": head, "base_branch": base_branch, "base": base_sha, **auxiliary_shas},
            "context": {key: ref["digest"] for key, ref in context_refs.items()},
            "prompt": {
                "template": prompt.template["digest"],
                "rendered_base": prompt.rendered_base["digest"],
                "logical_inputs": prompt.logical_inputs["digest"],
            },
        }
        cid, _ = capture_id(identity)
        git_store = GitObjectStore(self.config.root, "TauCeti")
        head_ref = f"refs/tauceti-record/{cid}/head"
        base_ref = f"refs/tauceti-record/{cid}/base"
        remote = self._repo_remote(TAUCETI)
        git_store.retain(remote, head, head_ref, fetch_ref=f"refs/pull/{pr}/head")
        git_store.retain(remote, base_sha, base_ref, fetch_ref=f"refs/heads/{base_branch}")
        merge_base = git_store.merge_base(head, base_sha)

        repositories: dict[str, Any] = {
            "tauceti": {
                "pr_head_sha": head,
                "head_retained_ref": head_ref,
                "base": {"branch": base_branch, "sha": base_sha, "retained_ref": base_ref},
                "merge_base_sha": merge_base,
            }
        }
        verified = [(git_store, head, head_ref), (git_store, base_sha, base_ref)]
        for name, sha in auxiliary_shas.items():
            path = (auxiliary_repositories or {})[name]
            store = GitObjectStore(self.config.root, "TauCetiRoadmap" if name == "roadmap" else "TauCetiReview")
            retained = f"refs/tauceti-record/{cid}/{name}"
            store.retain(self._archive_remote(name, path), sha, retained)
            repositories[name] = {"sha": sha, "retained_ref": retained}
            verified.append((store, sha, retained))

        dependency = self._dependency_state(git_store, head)
        check = self.github.pr_view(pr, ["headRefOid", "baseRefOid", "baseRefName"])
        if check is None:
            raise RecordingError("github_read_failed", f"could not recheck PR #{pr}")
        checked_base = str(check.get("baseRefOid") or self._resolve_branch_sha(base_branch)).lower()
        if (check.get("headRefOid") or "").lower() != head or checked_base != base_sha:
            raise RecordingError("input_raced", f"PR #{pr} changed while it was being captured")
        if not all(store.verify(sha, ref) for store, sha, ref in verified):
            raise RecordingError("validation_failed", "one or more retained Git refs failed verification")

        manifest = self._manifest_base(
            cid=cid,
            phase="fix",
            capture_type="fix-review-raw",
            worker_name=worker_name,
            worker_sha=worker_sha,
            candidate={"repository": TAUCETI, "pr": pr, "expected_head_sha": expected_head},
            repositories=repositories,
            dependency=dependency,
            context=context_refs,
            prompt=prompt,
        )
        return self.builder.seal(identity, manifest)

    def maybe_capture_roadmap(self, **kwargs: Any) -> str | None:
        try:
            cid = self.capture_roadmap(**kwargs)
            log(f"  record roadmap: {cid}")
            return cid
        except Exception as exc:
            self._failure("roadmap", kwargs.get("area"), exc)
            return None

    def capture_roadmap(
        self,
        *,
        area: str,
        worker_name: str,
        template_path: Path,
        rendered_base: str,
        logical_inputs: dict[str, Any],
        skip: list[str],
        claimed: str,
        survey_metadata: dict[str, Any],
        roadmap_dir: Path,
        review_dir: Path,
        rubric_bundle: Path | None,
        source: str | None = None,
        source_dir: Path | None = None,
    ) -> str:
        main_sha = self._resolve_branch_sha("main")
        roadmap_sha = _git_sha(roadmap_dir)
        review_sha = _git_sha(review_dir)
        try:
            source_sha = _git_sha(source_dir) if source_dir is not None else None
        except RecordingError as exc:
            raise RecordingError("unsupported_source", f"supplementary source could not be pinned: {source}") from exc
        if source is not None and source_sha is None:
            raise RecordingError("unsupported_source", f"supplementary source could not be pinned: {source}")
        source_description = self._source_description(source, source_dir)

        raw_context = RoadmapContextMaterializer(self.github).materialize(
            area=area,
            skip=skip,
            claimed=claimed,
            survey_metadata=survey_metadata,
            roadmap_dir=roadmap_dir,
            rubric_bundle=rubric_bundle,
            source=source_description,
        )
        prompt = PromptCapture.store(self.blobs, template_path, rendered_base, logical_inputs)
        context_refs = self._store_context({"roadmap": raw_context})
        worker_sha = self._worker_sha()
        git_identity = {"main": main_sha, "roadmap": roadmap_sha, "review": review_sha}
        if source_sha:
            git_identity["source"] = source_sha
        identity = {
            "schema": CAPTURE_SCHEMA,
            "phase": "roadmap",
            "capture_type": "roadmap-opportunity-raw",
            "area": area,
            "git": git_identity,
            "context": {key: ref["digest"] for key, ref in context_refs.items()},
            "prompt": {
                "template": prompt.template["digest"],
                "rendered_base": prompt.rendered_base["digest"],
                "logical_inputs": prompt.logical_inputs["digest"],
            },
        }
        cid, _ = capture_id(identity)
        repositories: dict[str, Any] = {}
        verified: list[tuple[GitObjectStore, str, str]] = []
        sources = (
            ("tauceti", GitObjectStore(self.config.root, "TauCeti"), self._repo_remote(TAUCETI), main_sha, "main"),
            (
                "roadmap",
                GitObjectStore(self.config.root, "TauCetiRoadmap"),
                self._archive_remote("roadmap", roadmap_dir),
                roadmap_sha,
                "roadmap",
            ),
            (
                "review",
                GitObjectStore(self.config.root, "TauCetiReview"),
                self._archive_remote("review", review_dir),
                review_sha,
                "review",
            ),
        )
        for key, store, remote, sha, ref_name in sources:
            retained = f"refs/tauceti-record/{cid}/{ref_name}"
            fetch_ref = "refs/heads/main" if key == "tauceti" else None
            store.retain(remote, sha, retained, fetch_ref=fetch_ref)
            repositories[key] = {"sha": sha, "retained_ref": retained}
            verified.append((store, sha, retained))
        if source_sha and source_dir is not None:
            source_name = sha256_bytes((source or str(source_dir)).encode())[:16]
            store = GitObjectStore(self.config.root, f"sources/{source_name}")
            retained = f"refs/tauceti-record/{cid}/source-{source_name}"
            store.retain(self._archive_remote("source", source_dir), source_sha, retained)
            repositories["source"] = {
                "identity": source_description,
                "sha": source_sha,
                "retained_ref": retained,
                "store": f"sources/{source_name}.git",
            }
            verified.append((store, source_sha, retained))

        tauceti_store = sources[0][1]
        dependency = self._dependency_state(tauceti_store, main_sha)
        if self._resolve_branch_sha("main") != main_sha:
            raise RecordingError("input_raced", "Tau Ceti main changed while the roadmap task was captured")
        if _git_sha(roadmap_dir) != roadmap_sha or _git_sha(review_dir) != review_sha:
            raise RecordingError("input_raced", "roadmap or review revision changed during capture")
        if source_dir is not None and _git_sha(source_dir) != source_sha:
            raise RecordingError("input_raced", "supplementary source changed during capture")
        if not all(store.verify(sha, ref) for store, sha, ref in verified):
            raise RecordingError("validation_failed", "one or more retained Git refs failed verification")

        manifest = self._manifest_base(
            cid=cid,
            phase="roadmap",
            capture_type="roadmap-opportunity-raw",
            worker_name=worker_name,
            worker_sha=worker_sha,
            candidate={"repository": TAUCETI, "roadmap_area": area},
            repositories=repositories,
            dependency=dependency,
            context=context_refs,
            prompt=prompt,
        )
        return self.builder.seal(identity, manifest)

    def _manifest_base(
        self,
        *,
        cid: str,
        phase: str,
        capture_type: str,
        worker_name: str,
        worker_sha: str,
        candidate: dict[str, Any],
        repositories: dict[str, Any],
        dependency: dict[str, Any],
        context: dict[str, Any],
        prompt: PromptCapture,
    ) -> dict[str, Any]:
        return {
            "schema": CAPTURE_SCHEMA,
            "capture_id": cid,
            "phase": phase,
            "capture_type": capture_type,
            "fidelity": "materialized-context",
            "purpose": "benchmark-corpus",
            "captured_at": _utc_now(),
            "provenance": {
                "worker_repository": "https://github.com/kim-em/TauCetiWorker",
                "worker_sha": worker_sha,
                "recorder_version": RECORDER_VERSION,
                "worker_name": worker_name,
            },
            "candidate": candidate,
            "repositories": repositories,
            "dependency_state": dependency,
            "context": context,
            "prompt": prompt.as_dict(),
            "integrity": {
                "git_objects_verified": True,
                "retained_refs_verified": True,
                "context_blobs_verified": True,
                "prompt_blobs_verified": True,
                "secret_scan_passed": True,
            },
        }

    def _failure(self, phase: str, candidate: Any, exc: Exception) -> None:
        record_failure(self.config, phase, candidate, exc)


def resolve_record_dir(cli_value: str | Path | None, env: dict[str, str] | None = None) -> Path | None:
    """Resolve CLI-over-environment record configuration without touching the filesystem."""
    environ = os.environ if env is None else env
    raw = cli_value if cli_value is not None else environ.get("TAUCETI_RECORD_DIR")
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        raise RecordingError("storage_failed", "record directory must not be empty")
    return Path(text).expanduser().resolve()


def _manifest_identity(manifest: dict[str, Any]) -> dict[str, Any]:
    repositories = manifest["repositories"]
    prompt = manifest["prompt"]
    prompt_identity = {key: prompt[key]["digest"] for key in ("template", "rendered_base", "logical_inputs")}
    context_identity = {key: ref["digest"] for key, ref in manifest["context"].items()}
    if manifest["phase"] == "fix" and manifest["capture_type"] == "fix-review-raw":
        tau = repositories["tauceti"]
        git_identity = {
            "head": tau["pr_head_sha"],
            "base_branch": tau["base"]["branch"],
            "base": tau["base"]["sha"],
        }
        for key in ("roadmap", "review"):
            if key in repositories:
                git_identity[key] = repositories[key]["sha"]
        return {
            "schema": CAPTURE_SCHEMA,
            "phase": "fix",
            "capture_type": "fix-review-raw",
            "candidate": {"repository": manifest["candidate"]["repository"], "pr": manifest["candidate"]["pr"]},
            "git": git_identity,
            "context": context_identity,
            "prompt": prompt_identity,
        }
    if manifest["phase"] == "roadmap" and manifest["capture_type"] == "roadmap-opportunity-raw":
        git_identity = {
            "main": repositories["tauceti"]["sha"],
            "roadmap": repositories["roadmap"]["sha"],
            "review": repositories["review"]["sha"],
        }
        if "source" in repositories:
            git_identity["source"] = repositories["source"]["sha"]
        return {
            "schema": CAPTURE_SCHEMA,
            "phase": "roadmap",
            "capture_type": "roadmap-opportunity-raw",
            "area": manifest["candidate"]["roadmap_area"],
            "git": git_identity,
            "context": context_identity,
            "prompt": prompt_identity,
        }
    raise RecordingError("validation_failed", "unsupported phase/capture_type pair")


def _manifest_repositories(root: Path, manifest: dict[str, Any]) -> list[tuple[str, GitObjectStore, str, str]]:
    found = []
    for key, repository in manifest["repositories"].items():
        if key == "tauceti" and "pr_head_sha" in repository:
            store = GitObjectStore(root, "TauCeti")
            found.append(("tauceti-head", store, repository["pr_head_sha"], repository["head_retained_ref"]))
            base = repository["base"]
            found.append(("tauceti-base", store, base["sha"], base["retained_ref"]))
            continue
        default = {"tauceti": "TauCeti", "roadmap": "TauCetiRoadmap", "review": "TauCetiReview"}.get(key)
        store_name = repository.get("store") or default
        if not isinstance(store_name, str) or Path(store_name).is_absolute() or ".." in Path(store_name).parts:
            raise RecordingError("validation_failed", f"unsafe Git store for {key}")
        found.append((key, GitObjectStore(root, store_name), repository["sha"], repository["retained_ref"]))
    return found


def validate_record_store(root: str | Path) -> list[str]:
    """Validate every complete capture, including a fresh fetch/checkout of each retained ref."""
    store_root = Path(root).expanduser().resolve()
    try:
        store_format = json.loads((store_root / "format.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecordingError("validation_failed", f"cannot read record-store format: {exc}") from exc
    if store_format != FORMAT:
        raise RecordingError("validation_failed", "unsupported record-store format")
    captures_root = store_root / "captures"
    blobs = BlobStore(store_root)
    validated: list[str] = []
    try:
        children = sorted(captures_root.iterdir())
    except OSError as exc:
        raise RecordingError("validation_failed", f"cannot enumerate captures: {exc}") from exc
    for capture_dir in children:
        if not capture_dir.is_dir() or not re.fullmatch(r"tc-[0-9a-f]{24}", capture_dir.name):
            continue
        try:
            complete = capture_dir / "COMPLETE"
            if not complete.is_file() or complete.stat().st_size != 0:
                continue
            manifest = json.loads((capture_dir / "capture.json").read_text(encoding="utf-8"))
            if manifest.get("schema") != CAPTURE_SCHEMA or manifest.get("capture_id") != capture_dir.name:
                raise RecordingError("validation_failed", f"{capture_dir.name}: manifest identity mismatch")
            if manifest.get("fidelity") != "materialized-context" or manifest.get("purpose") != "benchmark-corpus":
                raise RecordingError("validation_failed", f"{capture_dir.name}: unsupported fidelity or purpose")
            CaptureBuilder(RecordConfig(store_root), blobs)._verify_blob_refs(manifest)
            decoded_context = {
                key: json.loads(blobs.path(ref["digest"]).read_text(encoding="utf-8"))
                for key, ref in manifest.get("context", {}).items()
            }
            json.loads(blobs.path(manifest["prompt"]["logical_inputs"]["digest"]).read_text(encoding="utf-8"))
            if "roadmap" in decoded_context and decoded_context["roadmap"].get("schema") != (
                "tauceti.record.roadmap-context/v1"
            ):
                raise RecordingError("validation_failed", f"{capture_dir.name}: invalid roadmap-context schema")
            if "unresolved_findings" in decoded_context and decoded_context["unresolved_findings"].get(
                "algorithm"
            ) != "tauceti.record.unresolved-findings/v1":
                raise RecordingError("validation_failed", f"{capture_dir.name}: invalid unresolved-findings algorithm")
            identity = _manifest_identity(manifest)
            cid, digest = capture_id(identity)
            if cid != capture_dir.name or digest != manifest.get("integrity", {}).get("canonical_identity_sha256"):
                raise RecordingError("validation_failed", f"{capture_dir.name}: canonical identity mismatch")
            if not all(value is True for value in manifest["integrity"].values() if isinstance(value, bool)):
                raise RecordingError("validation_failed", f"{capture_dir.name}: capture-time integrity check failed")
            repositories = _manifest_repositories(store_root, manifest)
            for label, git_store, sha, retained_ref in repositories:
                if not git_store.verify(sha, retained_ref):
                    raise RecordingError("validation_failed", f"{capture_dir.name}: invalid Git history for {label}")
                with tempfile.TemporaryDirectory(prefix=f"{capture_dir.name}-{label}-") as raw:
                    task = Path(raw) / "repo"
                    _run(["git", "init", "-q", str(task)], code="validation_failed")
                    _run(
                        ["git", "-C", str(task), "fetch", "-q", "--no-tags", str(git_store.path), retained_ref],
                        code="validation_failed",
                    )
                    _run(["git", "-C", str(task), "checkout", "-q", "--detach", "FETCH_HEAD"], code="validation_failed")
                    if _git_sha(task) != sha.lower():
                        raise RecordingError("validation_failed", f"{capture_dir.name}: materialized {label} at wrong SHA")
            tau = manifest["repositories"]["tauceti"]
            tau_sha = tau.get("pr_head_sha") or tau.get("sha")
            tau_store = GitObjectStore(store_root, "TauCeti")
            if "pr_head_sha" in tau and tau_store.merge_base(tau["pr_head_sha"], tau["base"]["sha"]) != tau[
                "merge_base_sha"
            ]:
                raise RecordingError("validation_failed", f"{capture_dir.name}: merge base mismatch")
            for key, path in (("lean_toolchain", "lean-toolchain"), ("lake_manifest", "lake-manifest.json")):
                ref = manifest["dependency_state"][key]
                if tau_store.read_file(tau_sha, path) != blobs.path(ref["digest"]).read_bytes():
                    raise RecordingError("validation_failed", f"{capture_dir.name}: dependency blob differs from Git")
            validated.append(capture_dir.name)
        except (KeyError, TypeError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RecordingError("validation_failed", f"{capture_dir.name}: malformed capture: {exc}") from exc
    return validated
