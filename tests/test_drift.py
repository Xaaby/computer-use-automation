"""Tests for ARIA fingerprint drift detection and confidence scoring."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from capability.schema import ConfidenceScore
from evals.stability_runner import compute_confidence, promote_artifact
from replay.conditions import (
    compute_fingerprint,
    diff_fingerprints,
    fingerprint_matches,
)

ARIA_YAML_A = """
- role: textbox
  name: Member ID
- role: button
  name: Search
- role: heading
  name: Member Lookup
"""

ARIA_YAML_B = """
- role: textbox
  name: Member ID
- role: button
  name: Search
- role: button
  name: Clear
- role: heading
  name: Member Lookup
"""

PLAYWRIGHT_ARIA = """
- textbox "Member ID" [ref=e1]
- button "Search" [ref=e2]
- heading "Member Lookup" [ref=e3]
"""


def test_compute_fingerprint_stable():
    fp1 = compute_fingerprint(ARIA_YAML_A)
    fp2 = compute_fingerprint(ARIA_YAML_A)
    assert fp1.hash == fp2.hash


def test_compute_fingerprint_detects_change():
    fp_a = compute_fingerprint(ARIA_YAML_A)
    fp_b = compute_fingerprint(ARIA_YAML_B)
    assert fp_a.hash != fp_b.hash


def test_fingerprint_structure_content():
    fp = compute_fingerprint(ARIA_YAML_A)
    assert "textbox" in fp.structure
    assert "Member ID" in fp.structure["textbox"]
    assert "button" in fp.structure
    assert "Search" in fp.structure["button"]


def test_fingerprint_playwright_format():
    fp = compute_fingerprint(PLAYWRIGHT_ARIA)
    assert "textbox" in fp.structure
    assert "Member ID" in fp.structure["textbox"]
    assert "Search" in fp.structure["button"]


def test_fingerprint_matches_true():
    fp1 = compute_fingerprint(ARIA_YAML_A)
    fp2 = compute_fingerprint(ARIA_YAML_A)
    assert fingerprint_matches(fp1, fp2) is True


def test_fingerprint_matches_false():
    fp_a = compute_fingerprint(ARIA_YAML_A)
    fp_b = compute_fingerprint(ARIA_YAML_B)
    assert fingerprint_matches(fp_a, fp_b) is False


def test_diff_fingerprints_shows_added():
    fp_a = compute_fingerprint(ARIA_YAML_A)
    fp_b = compute_fingerprint(ARIA_YAML_B)
    diff = diff_fingerprints(fp_a, fp_b)
    assert "button" in diff["added"]
    assert "Clear" in diff["added"]["button"]


def test_confidence_stable():
    results = [{"status": "success", "duration_ms": 3000}] * 20
    conf = compute_confidence(results)
    assert conf.verdict == "STABLE"
    assert conf.score == 1.0
    assert conf.sample_size == 20


def test_confidence_flaky():
    results = (
        [{"status": "success", "duration_ms": 3000}] * 15
        + [{"status": "hard_failure", "duration_ms": 5000}] * 5
    )
    conf = compute_confidence(results)
    assert conf.verdict == "FLAKY"
    assert abs(conf.score - 0.75) < 0.01


def test_confidence_broken():
    results = (
        [{"status": "success", "duration_ms": 3000}] * 5
        + [{"status": "hard_failure", "duration_ms": 5000}] * 15
    )
    conf = compute_confidence(results)
    assert conf.verdict == "BROKEN"
    assert conf.score < 0.5


def test_promote_requires_stable(tmp_path: Path):
    artifact_path = tmp_path / "test.capability.json"
    artifact_path.write_text(
        json.dumps({"status": "draft", "provenance": {"status": "draft"}, "name": "test"}),
        encoding="utf-8",
    )
    flaky_conf = ConfidenceScore(
        score=0.75, sample_size=20, p50_ms=3000, p95_ms=5000, verdict="FLAKY"
    )
    with pytest.raises(ValueError, match="Cannot promote"):
        promote_artifact(artifact_path, flaky_conf)
    data = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert data["provenance"]["status"] == "draft"


def test_promote_stable_writes_approved(tmp_path: Path):
    artifact_path = tmp_path / "test.capability.json"
    artifact_path.write_text(
        json.dumps({"status": "draft", "provenance": {"status": "draft"}, "name": "test"}),
        encoding="utf-8",
    )
    stable_conf = ConfidenceScore(
        score=1.0, sample_size=20, p50_ms=3141, p95_ms=4587, verdict="STABLE"
    )
    result = promote_artifact(artifact_path, stable_conf)
    assert result["provenance"]["status"] == "approved"
    assert result["provenance"]["approved_by"] == "stability_runner"
    assert "approved_at" in result["provenance"]
    data = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert data["provenance"]["status"] == "approved"
