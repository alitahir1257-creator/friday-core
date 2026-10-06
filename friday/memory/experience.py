"""Explicit, evidence-gated troubleshooting history in the memory SQLite database."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

from friday.memory.models import MemoryPrivacy
from friday.memory.policy import bounded_score, contains_possible_secret, normalize_namespace, normalize_text
from friday.memory.store import normalize_timestamp, utc_now


_VERIFICATION = {"test", "build", "runtime", "deterministic_tool", "user_confirmation"}
_RESULTS = {"PENDING", "FAILED", "SUCCEEDED"}
_PRIVACY = {"public": 0, "private": 1, "local_only": 2}
_WORD = re.compile(r"[\w-]+", re.UNICODE)


def _text(value: str, name: str, *, required: bool = True, limit: int = 2000) -> str:
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValueError(f"{name} must be text of at most {limit} characters.")
    value = value.strip()
    if contains_possible_secret(value):
        raise ValueError("Experience contains a possible secret.")
    return value


def _steps(values: list[str] | tuple[str, ...], name: str, *, required: bool = True) -> list[str]:
    if not isinstance(values, (list, tuple)) or len(values) > 12 or (required and not values):
        raise ValueError(f"{name} must contain 1 to 12 steps.")
    return [_text(value, name, limit=500) for value in values]


def _hash(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _row(row: Any) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class ExperienceManager:
    """No automatic capture: trusted callers supply concrete verification evidence."""

    def __init__(self, memory: Any) -> None:
        self.memory = memory
        self.store = memory.store
        with self.store._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS experience_incidents (
                    id TEXT PRIMARY KEY, problem_family TEXT NOT NULL, summary TEXT NOT NULL,
                    symptoms TEXT NOT NULL, environment TEXT NOT NULL, project TEXT NOT NULL,
                    namespace TEXT NOT NULL, privacy TEXT NOT NULL CHECK(privacy IN ('public','private','local_only')),
                    signature TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('OPEN','IN_PROGRESS','RESOLVED','FAILED','UNVERIFIED','FORGOTTEN')),
                    created_at TEXT NOT NULL, resolved_at TEXT, expires_at TEXT
                );
                CREATE INDEX IF NOT EXISTS experience_incident_scope
                    ON experience_incidents(namespace, privacy, status, created_at DESC);
                CREATE TABLE IF NOT EXISTS experience_attempts (
                    id TEXT PRIMARY KEY, incident_id TEXT NOT NULL REFERENCES experience_incidents(id),
                    approach TEXT NOT NULL, result TEXT NOT NULL CHECK(result IN ('PENDING','FAILED','SUCCEEDED')),
                    failure_reason TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS experience_attempt_incident ON experience_attempts(incident_id);
                CREATE TABLE IF NOT EXISTS experience_resolutions (
                    id TEXT PRIMARY KEY, incident_id TEXT NOT NULL REFERENCES experience_incidents(id),
                    root_cause TEXT NOT NULL, successful_fix TEXT NOT NULL,
                    verification_method TEXT NOT NULL, verification_result TEXT NOT NULL,
                    evidence_ref TEXT NOT NULL, confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
                    reusable INTEGER NOT NULL CHECK(reusable IN (0,1)), content_hash TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('VERIFIED','UNVERIFIED','SUPERSEDED','INVALID')),
                    memory_id TEXT, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS experience_resolution_incident ON experience_resolutions(incident_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS experience_playbooks (
                    id TEXT PRIMARY KEY, problem_family TEXT NOT NULL, trigger_text TEXT NOT NULL,
                    environment TEXT NOT NULL, project TEXT NOT NULL, namespace TEXT NOT NULL,
                    privacy TEXT NOT NULL, root_cause TEXT NOT NULL, successful_fix TEXT NOT NULL,
                    diagnostic_steps TEXT NOT NULL, successful_steps TEXT NOT NULL,
                    known_bad_attempts TEXT NOT NULL, prerequisites TEXT NOT NULL,
                    verification_steps TEXT NOT NULL, confidence REAL NOT NULL,
                    content_hash TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('CURRENT','SUPERSEDED','INVALID','FORGOTTEN')),
                    replaces_id TEXT REFERENCES experience_playbooks(id), memory_id TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, expires_at TEXT
                );
                CREATE INDEX IF NOT EXISTS experience_playbook_scope ON experience_playbooks(namespace, privacy, status, updated_at DESC);
                CREATE TABLE IF NOT EXISTS experience_playbook_sources (
                    playbook_id TEXT NOT NULL REFERENCES experience_playbooks(id),
                    incident_id TEXT NOT NULL REFERENCES experience_incidents(id),
                    PRIMARY KEY(playbook_id, incident_id)
                );
                CREATE TABLE IF NOT EXISTS experience_corrections (
                    id TEXT PRIMARY KEY, incident_id TEXT NOT NULL REFERENCES experience_incidents(id),
                    reason TEXT NOT NULL, created_at TEXT NOT NULL
                );
            """)

    def _incident(self, incident_id: str, *, active: bool = True) -> dict[str, Any]:
        with self.store._connection() as db:
            row = _row(db.execute("SELECT * FROM experience_incidents WHERE id=?", (incident_id,)).fetchone())
        if row is None or (active and (row["status"] == "FORGOTTEN"
                           or (row["expires_at"] is not None and row["expires_at"] <= utc_now()))):
            raise KeyError("Incident was not found.")
        return row

    def start_incident(self, problem_family: str, summary: str, *, symptoms: str,
                       environment: str, project: str, namespace: str = "global",
                       privacy: str = "public", expires_at: str | None = None) -> dict[str, Any]:
        family = _text(problem_family, "problem_family", limit=200)
        summary = _text(summary, "summary")
        symptoms = _text(symptoms, "symptoms")
        environment = _text(environment, "environment", limit=500)
        project = _text(project, "project", limit=200)
        namespace = normalize_namespace(namespace)
        if privacy not in _PRIVACY:
            raise ValueError("Unsupported privacy.")
        expires_at = normalize_timestamp(expires_at)
        if expires_at is not None and expires_at <= utc_now():
            raise ValueError("Incident expiry must be in the future.")
        signature = _hash(*(normalize_text(value) for value in (family, summary, symptoms, environment, project, namespace, privacy)))
        with self.store._connection() as db:
            duplicate = db.execute(
                "SELECT * FROM experience_incidents WHERE signature=? AND status IN ('OPEN','IN_PROGRESS','UNVERIFIED') "
                "AND (expires_at IS NULL OR expires_at>?) ORDER BY created_at DESC LIMIT 1",
                (signature, utc_now()),
            ).fetchone()
            if duplicate:
                return dict(duplicate)
            incident_id, now = str(uuid.uuid4()), utc_now()
            db.execute(
                "INSERT INTO experience_incidents VALUES (?,?,?,?,?,?,?,?,?,'OPEN',?,NULL,?)",
                (incident_id, family, summary, symptoms, environment, project, namespace, privacy, signature, now, expires_at),
            )
        return self._incident(incident_id)

    def record_attempt(self, incident_id: str, approach: str) -> dict[str, Any]:
        incident = self._incident(incident_id)
        if incident["status"] in {"RESOLVED", "FAILED"}:
            raise ValueError("Incident is closed.")
        attempt_id, now = str(uuid.uuid4()), utc_now()
        with self.store._connection() as db:
            db.execute("INSERT INTO experience_attempts(id,incident_id,approach,result,created_at,updated_at) VALUES (?,?,?,'PENDING',?,?)",
                       (attempt_id, incident_id, _text(approach, "approach"), now, now))
            db.execute("UPDATE experience_incidents SET status='IN_PROGRESS' WHERE id=?", (incident_id,))
            return dict(db.execute("SELECT * FROM experience_attempts WHERE id=?", (attempt_id,)).fetchone())

    def record_attempt_result(self, attempt_id: str, result: str, *, evidence: str,
                              failure_reason: str = "") -> dict[str, Any]:
        if result not in {"FAILED", "SUCCEEDED"}:
            raise ValueError("Result must be FAILED or SUCCEEDED.")
        evidence = _text(evidence, "evidence", limit=1000)
        failure_reason = _text(failure_reason, "failure_reason", required=result == "FAILED", limit=500)
        with self.store._connection() as db:
            row = db.execute("SELECT a.*,i.status incident_status FROM experience_attempts a JOIN experience_incidents i ON i.id=a.incident_id WHERE a.id=?", (attempt_id,)).fetchone()
            if row is None or row["result"] != "PENDING" or row["incident_status"] == "FORGOTTEN":
                raise ValueError("Attempt is unavailable or already final.")
            db.execute("UPDATE experience_attempts SET result=?,failure_reason=?,evidence=?,updated_at=? WHERE id=?",
                       (result, failure_reason, evidence, utc_now(), attempt_id))
            return dict(db.execute("SELECT * FROM experience_attempts WHERE id=?", (attempt_id,)).fetchone())

    def resolve_incident(self, incident_id: str, *, root_cause: str, successful_fix: str,
                         verification_method: str = "", verification_result: str = "",
                         evidence_ref: str = "", confidence: float = 0.5,
                         reusable: bool = False) -> dict[str, Any]:
        incident = self._incident(incident_id)
        if incident["status"] in {"FAILED", "RESOLVED"}:
            raise ValueError("Closed incident requires correction or a new incident.")
        root_cause = _text(root_cause, "root_cause")
        successful_fix = _text(successful_fix, "successful_fix")
        verification_method = _text(verification_method, "verification_method", required=False, limit=100)
        verification_result = _text(verification_result, "verification_result", required=False, limit=500)
        evidence_ref = _text(evidence_ref, "evidence_ref", required=False, limit=500)
        confidence = bounded_score(confidence, "confidence")
        observed = normalize_text(verification_result)
        verified = (verification_method in _VERIFICATION and bool(evidence_ref)
                    and not re.search(r"\b(?:looks|seems|probably|might)\b", observed)
                    and bool(re.search(r"\b(?:pass|passed|success|successful|succeeded|expected|returned|launched|confirmed|worked)\b", observed)))
        status = "VERIFIED" if verified else "UNVERIFIED"
        resolution_id, now = str(uuid.uuid4()), utc_now()
        content_hash = _hash(normalize_text(root_cause), normalize_text(successful_fix))
        with self.store._connection() as db:
            db.execute("INSERT INTO experience_resolutions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (resolution_id, incident_id, root_cause, successful_fix, verification_method,
                        verification_result, evidence_ref, confidence, int(reusable), content_hash, status, None, now))
            db.execute("UPDATE experience_incidents SET status=?,resolved_at=? WHERE id=?",
                       ("RESOLVED" if verified else "UNVERIFIED", now if verified else None, incident_id))
        if verified:
            item = self.memory.remember(
                f"Incident {incident_id}: {incident['summary']}. Root cause: {root_cause}. Successful fix: {successful_fix}.",
                kind="EPISODE", namespace=incident["namespace"], privacy=incident["privacy"],
                source="verified_experience", confidence=confidence,
                expires_at=incident["expires_at"],
                metadata={"experience_type": "resolution", "experience_id": resolution_id},
            )
            with self.store._connection() as db:
                db.execute("UPDATE experience_resolutions SET memory_id=? WHERE id=?", (item.id, resolution_id))
        return self._resolution(resolution_id)

    def _resolution(self, resolution_id: str) -> dict[str, Any]:
        with self.store._connection() as db:
            return dict(db.execute("SELECT * FROM experience_resolutions WHERE id=?", (resolution_id,)).fetchone())

    def mark_failed(self, incident_id: str) -> None:
        incident = self._incident(incident_id)
        if incident["status"] == "RESOLVED":
            raise ValueError("Use correct_incident for a verified resolution.")
        with self.store._connection() as db:
            db.execute("UPDATE experience_incidents SET status='FAILED' WHERE id=?", (incident_id,))

    def promote_playbook(self, incident_id: str, *, trigger: str, diagnostic_steps: list[str],
                         successful_steps: list[str], prerequisites: list[str],
                         verification_steps: list[str]) -> dict[str, Any]:
        incident = self._incident(incident_id)
        with self.store._connection() as db:
            resolution = _row(db.execute("SELECT * FROM experience_resolutions WHERE incident_id=? AND status='VERIFIED' ORDER BY rowid DESC LIMIT 1", (incident_id,)).fetchone())
            bad = [row[0] for row in db.execute("SELECT approach FROM experience_attempts WHERE incident_id=? AND result='FAILED' ORDER BY created_at,id", (incident_id,))]
        if resolution is None or not resolution["reusable"] or incident["status"] != "RESOLVED":
            raise ValueError("Only a current, reusable, verified resolution can make a playbook.")
        trigger = _text(trigger, "trigger")
        diagnostics = _steps(diagnostic_steps, "diagnostic_steps")
        success = _steps(successful_steps, "successful_steps")
        prerequisites = _steps(prerequisites, "prerequisites", required=False)
        verification = _steps(verification_steps, "verification_steps")
        key = _hash(*(normalize_text(value) for value in (incident["problem_family"], trigger, incident["environment"],
                    incident["project"], incident["namespace"], incident["privacy"], resolution["root_cause"], resolution["successful_fix"])),
                    diagnostics, success, prerequisites, verification, incident["expires_at"])
        with self.store._connection() as db:
            existing = db.execute("SELECT * FROM experience_playbooks WHERE content_hash=? AND status='CURRENT'", (key,)).fetchone()
            if existing:
                db.execute("INSERT OR IGNORE INTO experience_playbook_sources VALUES (?,?)", (existing["id"], incident_id))
                merged = list(dict.fromkeys(json.loads(existing["known_bad_attempts"]) + bad))
                db.execute("UPDATE experience_playbooks SET known_bad_attempts=?,updated_at=? WHERE id=?",
                           (json.dumps(merged), utc_now(), existing["id"]))
                existing_id = existing["id"]
            else:
                existing_id = None
            if existing_id is None:
                playbook_id, now = str(uuid.uuid4()), utc_now()
                db.execute("INSERT INTO experience_playbooks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'CURRENT',NULL,NULL,?,?,?)",
                           (playbook_id, incident["problem_family"], trigger, incident["environment"], incident["project"],
                            incident["namespace"], incident["privacy"], resolution["root_cause"], resolution["successful_fix"],
                            json.dumps(diagnostics), json.dumps(success), json.dumps(bad), json.dumps(prerequisites),
                            json.dumps(verification), resolution["confidence"], key, now, now, incident["expires_at"]))
                db.execute("INSERT INTO experience_playbook_sources VALUES (?,?)", (playbook_id, incident_id))
        if existing_id is not None:
            return self._playbook(existing_id)
        item = self.memory.remember(
            f"Verified playbook {playbook_id}: {trigger}. Root cause: {resolution['root_cause']}. "
            f"Steps: {'; '.join(diagnostics + success)}. Verify: {'; '.join(verification)}.",
            kind="EPISODE", namespace=incident["namespace"], privacy=incident["privacy"],
            source="verified_experience", confidence=resolution["confidence"],
            expires_at=incident["expires_at"],
            metadata={"experience_type": "playbook", "experience_id": playbook_id},
        )
        with self.store._connection() as db:
            db.execute("UPDATE experience_playbooks SET memory_id=? WHERE id=?", (item.id, playbook_id))
        return self._playbook(playbook_id)

    def _playbook(self, playbook_id: str) -> dict[str, Any]:
        with self.store._connection() as db:
            row = db.execute("SELECT * FROM experience_playbooks WHERE id=?", (playbook_id,)).fetchone()
            if row is None:
                raise KeyError("Playbook was not found.")
            result = dict(row)
            result["source_incidents"] = [r[0] for r in db.execute("SELECT incident_id FROM experience_playbook_sources WHERE playbook_id=? ORDER BY incident_id", (playbook_id,))]
        for field in ("diagnostic_steps", "successful_steps", "known_bad_attempts", "prerequisites", "verification_steps"):
            result[field] = json.loads(result[field])
        return result

    def supersede_playbook(self, old_id: str, replacement_id: str) -> None:
        old, new = self._playbook(old_id), self._playbook(replacement_id)
        if (old_id == replacement_id or old["status"] != "CURRENT" or new["status"] != "CURRENT"
            or (old["namespace"], old["privacy"], old["problem_family"], old["project"], old["environment"])
            != (new["namespace"], new["privacy"], new["problem_family"], new["project"], new["environment"])):
            raise ValueError("Replacement must be a current playbook in the same scope and family.")
        with self.store._connection() as db:
            retired = [dict(row) for row in db.execute(
                "SELECT r.* FROM experience_resolutions r JOIN experience_playbook_sources s ON s.incident_id=r.incident_id "
                "WHERE s.playbook_id=? AND r.status='VERIFIED'", (old_id,))
                if (row["root_cause"], row["successful_fix"]) != (new["root_cause"], new["successful_fix"])]
            db.execute("UPDATE experience_playbooks SET status='SUPERSEDED',updated_at=? WHERE id=?", (utc_now(), old_id))
            db.execute("UPDATE experience_playbooks SET replaces_id=? WHERE id=?", (old_id, replacement_id))
            for resolution in retired:
                db.execute("UPDATE experience_resolutions SET status='SUPERSEDED' WHERE id=?", (resolution["id"],))
        for item in [old, *retired]:
            if item["memory_id"]:
                self.memory.forget(item["memory_id"])

    def correct_incident(self, incident_id: str, *, reason: str) -> None:
        _text(reason, "reason")
        if self._incident(incident_id, active=False)["status"] == "FORGOTTEN":
            raise ValueError("Incident is already forgotten.")
        with self.store._connection() as db:
            resolutions = [dict(row) for row in db.execute("SELECT * FROM experience_resolutions WHERE incident_id=? AND status='VERIFIED'", (incident_id,))]
            playbooks = [dict(row) for row in db.execute("SELECT p.* FROM experience_playbooks p JOIN experience_playbook_sources s ON s.playbook_id=p.id WHERE s.incident_id=? AND p.status='CURRENT'", (incident_id,))]
            db.execute("UPDATE experience_resolutions SET status='INVALID' WHERE incident_id=? AND status='VERIFIED'", (incident_id,))
            db.execute("UPDATE experience_incidents SET status='UNVERIFIED',resolved_at=NULL WHERE id=?", (incident_id,))
            db.execute("INSERT INTO experience_corrections VALUES (?,?,?,?)", (str(uuid.uuid4()), incident_id, reason, utc_now()))
            for playbook in playbooks:
                db.execute("UPDATE experience_playbooks SET status='INVALID',updated_at=? WHERE id=?", (utc_now(), playbook["id"]))
        for item in resolutions + playbooks:
            if item["memory_id"]:
                self.memory.forget(item["memory_id"])

    def forget_incident(self, incident_id: str) -> None:
        self.correct_incident(incident_id, reason="forgotten")
        with self.store._connection() as db:
            db.execute("UPDATE experience_incidents SET status='FORGOTTEN' WHERE id=?", (incident_id,))

    def retrieve_experience(self, query: str, *, namespace: str = "global", project: str = "",
                            environment: str = "", privacy_level: str = "public",
                            route_backend: str | None = None, include_unverified: bool = False,
                            limit: int = 5) -> list[dict[str, Any]]:
        query = _text(query, "query", limit=2000)
        namespace = normalize_namespace(namespace)
        project = _text(project, "project", required=False, limit=200)
        environment = _text(environment, "environment", required=False, limit=500)
        if privacy_level not in _PRIVACY or not isinstance(limit, int) or isinstance(limit, bool) or not 0 <= limit <= 20:
            raise ValueError("Invalid retrieval scope or limit.")
        if not limit:
            return []
        allowed = ("public",) if route_backend != "local" else tuple(p for p, rank in _PRIVACY.items() if rank <= _PRIVACY[privacy_level])
        marks = ",".join("?" for _ in allowed)
        with self.store._connection() as db:
            incidents = [dict(row) for row in db.execute(
                f"SELECT * FROM experience_incidents WHERE namespace=? AND privacy IN ({marks}) AND status!='FORGOTTEN' "
                "AND (expires_at IS NULL OR expires_at>?) ORDER BY created_at DESC LIMIT 500",
                (namespace, *allowed, utc_now()))]
            playbooks = [dict(row) for row in db.execute(
                f"SELECT * FROM experience_playbooks WHERE namespace=? AND privacy IN ({marks}) AND status='CURRENT' "
                "AND (expires_at IS NULL OR expires_at>?) ORDER BY updated_at DESC LIMIT 500",
                (namespace, *allowed, utc_now()))]
        semantic: dict[tuple[str, str], float] = {}
        try:
            for hit in self.memory.retrieve_relevant(query, namespace=namespace, include_global=False,
                    privacy_level=privacy_level, route_backend=route_backend, limit=30):
                item = self.memory.get(hit.id)
                if item and item.source == "verified_experience":
                    semantic[(item.metadata.get("experience_type"), item.metadata.get("experience_id"))] = hit.relevance
        except Exception:
            pass  # Lexical structured lookup remains available if the optional index is unavailable.
        words = set(_WORD.findall(normalize_text(query)))
        results: list[dict[str, Any]] = []
        for incident in incidents:
            if incident["privacy"] != "public" and incident["project"] != (project or namespace):
                continue
            with self.store._connection() as db:
                resolution = _row(db.execute("SELECT * FROM experience_resolutions WHERE incident_id=? ORDER BY rowid DESC LIMIT 1", (incident["id"],)).fetchone())
                attempts = [dict(row) for row in db.execute("SELECT * FROM experience_attempts WHERE incident_id=? ORDER BY created_at,id", (incident["id"],))]
            verified = resolution is not None and resolution["status"] == "VERIFIED" and incident["status"] == "RESOLVED"
            failed_attempts = [a for a in attempts if a["result"] == "FAILED"]
            if not verified and not include_unverified and not failed_attempts:
                continue
            searchable = " ".join((incident["problem_family"], incident["summary"], incident["symptoms"],
                                   resolution["root_cause"] if resolution else "", resolution["successful_fix"] if resolution else ""))
            lexical = len(words & set(_WORD.findall(normalize_text(searchable)))) / max(1, len(words))
            similarity = semantic.get(("resolution", resolution["id"]), 0) if resolution else 0
            if not lexical and not similarity:
                continue
            score = (2 if verified else 0.25 if failed_attempts else 0) + max(lexical, similarity) + (0.2 if project and incident["project"] == project else 0) + (0.2 if environment and incident["environment"] == environment else 0) + (resolution["confidence"] * 0.1 if verified else 0)
            results.append({"type": "incident" if verified else "warning" if failed_attempts else "unverified",
                            "score": round(score, 4), "incident": incident,
                            "resolution": resolution if verified or include_unverified else None, "warnings": failed_attempts})
        for playbook in playbooks:
            if playbook["privacy"] != "public" and playbook["project"] != (project or namespace):
                continue
            searchable = " ".join((playbook["problem_family"], playbook["trigger_text"], playbook["root_cause"], playbook["successful_fix"]))
            lexical = len(words & set(_WORD.findall(normalize_text(searchable)))) / max(1, len(words))
            similarity = semantic.get(("playbook", playbook["id"]), 0)
            if not lexical and not similarity:
                continue
            score = 1.5 + max(lexical, similarity) + (0.2 if project and playbook["project"] == project else 0) + (0.2 if environment and playbook["environment"] == environment else 0) + playbook["confidence"] * 0.1
            results.append({"type": "playbook", "score": round(score, 4), "playbook": self._playbook(playbook["id"])})
        results.sort(key=lambda entry: ((entry.get("incident") or entry.get("playbook"))["created_at"],
                                        (entry.get("incident") or entry.get("playbook"))["id"]), reverse=True)
        results.sort(key=lambda entry: -entry["score"])
        return results[:limit]

    def planner_context(self, query: str, *, namespace: str, privacy_level: str) -> str:
        hits = self.retrieve_experience(query, namespace=namespace, privacy_level=privacy_level,
                                        route_backend="local" if privacy_level != "public" else None, limit=3)
        lines = []
        for hit in hits:
            if hit["type"] == "incident":
                resolution = hit["resolution"]
                lines.append({"verified_fix": resolution["successful_fix"], "root_cause": resolution["root_cause"],
                              "verification": resolution["verification_result"],
                              "failed_attempts_warning": [a["approach"] for a in hit["warnings"]],
                              "environment": hit["incident"]["environment"]})
            elif hit["type"] == "warning":
                lines.append({"failed_attempts_warning": [a["approach"] for a in hit["warnings"]],
                              "environment": hit["incident"]["environment"]})
            elif hit["type"] == "playbook":
                playbook = hit["playbook"]
                lines.append({"verified_playbook": playbook["successful_steps"],
                              "prerequisites": playbook["prerequisites"],
                              "verification": playbook["verification_steps"],
                              "failed_attempts_warning": playbook["known_bad_attempts"]})
        return json.dumps(lines, ensure_ascii=False)[:3000]
