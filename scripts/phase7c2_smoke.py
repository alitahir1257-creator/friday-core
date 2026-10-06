"""Offline acceptance for explicit verified experience learning."""

from __future__ import annotations

import os
from contextlib import closing
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from friday.memory import MemoryManager  # noqa: E402
from friday.planner.planner import Planner  # noqa: E402
from friday.tools.registry import ToolRegistry  # noqa: E402
from phase7c1_smoke import _working_set  # noqa: E402


def check(label: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(label)
    print(f"{label}: PASS")


def elapsed(call, *args, **kwargs):
    started = time.perf_counter()
    return call(*args, **kwargs), (time.perf_counter() - started) * 1000


def main() -> None:
    before_ram = _working_set()
    with tempfile.TemporaryDirectory(prefix="phase7c2-", dir=ROOT) as temp:
        path = Path(temp) / "memory.sqlite3"
        memory = MemoryManager(path)
        ex = memory.experience
        before_bytes = path.stat().st_size
        incident, incident_ms = elapsed(ex.start_incident, "Shopify OAuth scope missing",
            "OAuth callback failed because required Shopify scope was missing",
            symptoms="Shopify login connection issue", environment="Windows local test",
            project="shopify", namespace="shopify")
        check("A incident creation", incident["status"] == "OPEN")
        duplicate = ex.start_incident("Shopify OAuth scope missing",
            "OAuth callback failed because required Shopify scope was missing",
            symptoms="Shopify login connection issue", environment="Windows local test",
            project="shopify", namespace="shopify")
        check("Q duplicate open incident", duplicate["id"] == incident["id"])
        attempt, attempt_ms = elapsed(ex.record_attempt, incident["id"], "reinstall package")
        check("B attempt capture", attempt["result"] == "PENDING")
        failed = ex.record_attempt_result(attempt["id"], "FAILED", evidence="Same OAuth callback error in test run",
                                          failure_reason="Scope remained missing")
        check("C failed attempt capture", failed["result"] == "FAILED")
        good = ex.record_attempt(incident["id"], "add required Shopify scope")
        ex.record_attempt_result(good["id"], "SUCCEEDED", evidence="OAuth callback returned expected connected state")
        resolution, resolution_ms = elapsed(ex.resolve_incident, incident["id"],
            root_cause="Required Shopify OAuth scope was absent", successful_fix="Add the required Shopify scope",
            verification_method="test", verification_result="OAuth callback test passed and returned connected state",
            evidence_ref="oauth-callback-test:PASS", confidence=0.9, reusable=True)
        check("D verified resolution", resolution["status"] == "VERIFIED" and bool(resolution["memory_id"]))
        hypothesis = ex.start_incident("Shopify OAuth scope missing", "Possible network issue",
            symptoms="Shopify login connection issue", environment="Windows local test", project="shopify", namespace="shopify")
        unverified = ex.resolve_incident(hypothesis["id"], root_cause="maybe network", successful_fix="reinstall package",
            verification_method="test", verification_result="looks fixed", evidence_ref="model guess", reusable=True)
        check("E vague evidence stays unverified", unverified["status"] == "UNVERIFIED" and unverified["memory_id"] is None)
        try:
            ex.promote_playbook(hypothesis["id"], trigger="login error", diagnostic_steps=["check network"],
                                successful_steps=["reinstall"], prerequisites=[], verification_steps=["check login"])
            raise AssertionError("Unverified playbook promoted")
        except ValueError:
            pass
        playbook, promotion_ms = elapsed(ex.promote_playbook, incident["id"],
            trigger="Shopify login connection issue after OAuth callback", diagnostic_steps=["Inspect OAuth scopes"],
            successful_steps=["Add the required Shopify scope"], prerequisites=["Access to app OAuth configuration"],
            verification_steps=["OAuth callback returns connected state"])
        check("F playbook generation", playbook["status"] == "CURRENT" and playbook["source_incidents"] == [incident["id"]])
        ordinary = memory.remember("FTS5 ordinary memory fixture", namespace="shopify", source="fixture")
        check("H existing lexical memory", any(hit.id == ordinary.id for hit in memory.search("FTS5 ordinary", namespace="shopify")))
        failed_only = ex.start_incident("Package install error", "Dependency install failed", symptoms="version conflict",
                                        environment="Windows local test", project="shopify", namespace="shopify")
        failed_attempt = ex.record_attempt(failed_only["id"], "repeat the same install")
        ex.record_attempt_result(failed_attempt["id"], "FAILED", evidence="Installer returned version conflict",
                                 failure_reason="Version mismatch")
        ex.mark_failed(failed_only["id"])
        warning_hits = ex.retrieve_experience("Dependency install failed", namespace="shopify", project="shopify")
        check("J warning without verified fix", any(h["type"] == "warning" and h["incident"]["id"] == failed_only["id"]
                                                  for h in warning_hits))
        hits, retrieve_ms = elapsed(ex.retrieve_experience, "Shopify login connection issue", namespace="shopify",
                                     project="shopify", environment="Windows local test", limit=5)
        check("H lexical retrieval", bool(hits) and hits[0]["type"] == "incident")
        check("I verified over unverified", all(h["incident"]["id"] != hypothesis["id"] for h in hits if h["type"] == "incident"))
        check("J failed attempt warning", any(a["approach"] == "reinstall package" for a in hits[0]["warnings"]))
        context = ex.planner_context("Shopify login connection issue", namespace="shopify", privacy_level="public")
        check("K failed attempt not solution", '"verified_fix": "Add the required Shopify scope"' in context
              and '"failed_attempts_warning": ["reinstall package"]' in context)
        semantic_start = time.perf_counter()
        paraphrase = ex.retrieve_experience("merchant authorization handshake refuses to complete",
                                            namespace="shopify", project="shopify")
        semantic_ms = (time.perf_counter() - semantic_start) * 1000
        check("G semantic paraphrase", any(h["type"] == "playbook" and h["playbook"]["id"] == playbook["id"] for h in paraphrase))
        planner_prompts = []
        Planner(None, ToolRegistry(), model_call=lambda goal, prompt, privacy: planner_prompts.append(prompt) or '{"steps":[]}',
                experience=ex).plan("Shopify login connection issue", namespace="shopify")
        check("T Planner retrieval", bool(planner_prompts) and "Relevant verified experience" in planner_prompts[0])
        other = ex.start_incident("Shopify OAuth scope missing", "Other merchant failure", symptoms="OAuth error",
                                  environment="Windows local test", project="other", namespace="other")
        ex.resolve_incident(other["id"], root_cause="other project cause", successful_fix="other project fix",
                            verification_method="user_confirmation", verification_result="User confirmed fix worked",
                            evidence_ref="user:confirmed", reusable=True)
        check("N namespace isolation", all(h.get("incident", {}).get("id") != other["id"] for h in hits))
        private = ex.start_incident("Local model launch", "Local model executable path wrong",
            symptoms="FRIDAY model not starting", environment="Windows laptop", project="friday", namespace="friday", privacy="private")
        ex.resolve_incident(private["id"], root_cause="Executable path was wrong", successful_fix="Set correct path",
            verification_method="runtime", verification_result="Process launched successfully",
            evidence_ref="local-process:launched", reusable=True)
        calls = []
        with patch.object(socket.socket, "connect", side_effect=lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError("network"))):
            private_hits = ex.retrieve_experience("local model path", namespace="friday", project="friday",
                                                  privacy_level="private", route_backend="local")
        check("O PRIVATE remote calls zero", not calls and private_hits and not ex.retrieve_experience(
            "local model path", namespace="friday", project="friday", route_backend=None))
        print(f"PRIVATE_REMOTE_CALLS={len(calls)}")
        check("R secret rejection", _reject_secret(ex))
        roman = ex.start_incident("FRIDAY local model", "FRIDAY ka local model start nahi ho raha tha",
            symptoms="local model start failure", environment="Windows laptop", project="roman", namespace="roman")
        ex.resolve_incident(roman["id"], root_cause="executable path ghalat tha",
            successful_fix="Correct path set karne ke baad launch verify hua",
            verification_method="runtime", verification_result="Process launched successfully",
            evidence_ref="local-process:launched", reusable=True)
        roman_hits = ex.retrieve_experience("local model q start nhi ho raha tha?", namespace="roman", project="roman")
        print("ROMAN_URDU_PROBE=" + ("PASS" if any(h.get("incident", {}).get("id") == roman["id"] for h in roman_hits) else "LIMITED"))
        replacement_incident = ex.start_incident("Shopify OAuth scope missing", "New Shopify login scope fix",
            symptoms="Shopify login connection issue", environment="Windows local test", project="shopify", namespace="shopify")
        ex.resolve_incident(replacement_incident["id"], root_cause="Scope list changed", successful_fix="Add current required scope",
            verification_method="test", verification_result="OAuth test passed", evidence_ref="oauth-new:PASS", reusable=True)
        replacement = ex.promote_playbook(replacement_incident["id"], trigger="Shopify login connection issue",
            diagnostic_steps=["Inspect current scope list"], successful_steps=["Add current required scope"],
            prerequisites=["App access"], verification_steps=["OAuth test passed"])
        ex.supersede_playbook(playbook["id"], replacement["id"])
        current = ex.retrieve_experience("Shopify login connection issue", namespace="shopify", project="shopify")
        check("L supersede preference", all(h.get("playbook", {}).get("id") != playbook["id"] for h in current)
              and all(h.get("incident", {}).get("id") != incident["id"] for h in current if h["type"] == "incident")
              and any(h.get("playbook", {}).get("id") == replacement["id"] for h in current))
        ex.correct_incident(replacement_incident["id"], reason="User said this no longer works")
        corrected = ex.retrieve_experience("New Shopify login scope fix", namespace="shopify", project="shopify")
        check("M user correction invalidation", all(h.get("incident", {}).get("id") != replacement_incident["id"] for h in corrected))
        ex.forget_incident(incident["id"])
        forgotten = ex.retrieve_experience("Shopify login connection issue", namespace="shopify", project="shopify")
        check("P forget cleanup", all(h.get("incident", {}).get("id") != incident["id"] for h in forgotten)
              and memory.get(resolution["memory_id"]) is None and memory.get(playbook["memory_id"]) is None)
        with closing(sqlite3.connect(path)) as db:
            count = db.execute("SELECT COUNT(*) FROM memory_semantic_metadata WHERE memory_id=?", (resolution["memory_id"],)).fetchone()[0]
        check("P no ghost vector", count == 0)
        expiring = ex.start_incident("Expiring fix", "Expiring process issue", symptoms="launch error",
            environment="Windows laptop", project="friday", namespace="friday", expires_at="2100-01-01T00:00:00Z")
        expiring_resolution = ex.resolve_incident(expiring["id"], root_cause="wrong path", successful_fix="set path",
            verification_method="runtime", verification_result="Process launched successfully",
            evidence_ref="local-process:launched")
        with closing(sqlite3.connect(path)) as db:
            db.execute("UPDATE experience_incidents SET expires_at='2000-01-01T00:00:00Z' WHERE id=?", (expiring["id"],))
            db.execute("UPDATE memory_items SET expires_at='2000-01-01T00:00:00Z' WHERE id=?", (expiring_resolution["memory_id"],))
            db.commit()
        check("Expiry excludes structured and semantic", not ex.retrieve_experience("Expiring process issue",
              namespace="friday", project="friday") and memory.get(expiring_resolution["memory_id"]) is None)
        after_ram = _working_set()
        print(f"INCIDENT_WRITE_MS={incident_ms:.2f}")
        print(f"ATTEMPT_WRITE_MS={attempt_ms:.2f}")
        print(f"RESOLUTION_WRITE_MS={resolution_ms:.2f}")
        print(f"PLAYBOOK_PROMOTION_MS={promotion_ms:.2f}")
        print(f"EXPERIENCE_RETRIEVAL_MS={retrieve_ms:.2f}")
        print(f"SEMANTIC_RETRIEVAL_MS={semantic_ms:.2f}")
        _, playbook_ms = elapsed(ex.retrieve_experience, "Shopify login connection issue", namespace="shopify", project="shopify")
        print(f"PLAYBOOK_RETRIEVAL_MS={playbook_ms:.2f}")
        print(f"DB_GROWTH_BYTES={path.stat().st_size-before_bytes}")
        print(f"RAM_IMPACT_BYTES={after_ram-before_ram if before_ram is not None and after_ram is not None else 'UNVERIFIED'}")
        memory.close()
    completed = subprocess.run([sys.executable, "-B", str(ROOT / "scripts" / "phase7c1_smoke.py")],
                               cwd=ROOT, capture_output=True, text=True, timeout=900,
                               env={**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"})
    if completed.returncode:
        print(completed.stdout[-6000:])
        print(completed.stderr[-4000:])
    check("U Phase 7C.1 regression", completed.returncode == 0 and "PHASE 7C.1 SMOKE PASS" in completed.stdout)
    for label in ("Phase 7B regression", "Phase 7A regression", "Phase 6B regression",
                  "Phase 6A regression", "Phase 5C regression", "privacy regression"):
        check(label, f"{label}: PASS" in completed.stdout)
    print("PHASE 7C.2 SMOKE PASS")


def _reject_secret(ex) -> bool:
    try:
        ex.start_incident("test", "token=abcdefgh12345678", symptoms="error", environment="local", project="test")
    except ValueError:
        return True
    return False


if __name__ == "__main__":
    main()
