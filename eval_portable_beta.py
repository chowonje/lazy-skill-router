from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import platform
import re
import sys
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from lazy_skill_router_capability_index import (
    CAPABILITY_INDEX_SCHEMA,
    CAPABILITY_INDEX_SCHEMA_V1,
    FEATURE_EXTRACTOR_V1,
    build_capability_index,
)
from lazy_skill_router_common import confined_read_regular_snapshot, write_json_atomic
from lazy_skill_router_inventory import InventorySnapshot, inventory_revision
from lazy_skill_router_retrieval import (
    PRODUCT_PREVIEW_ALGORITHM,
    RETRIEVAL_IMPLEMENTATION_REVISION,
    SUPPORTED_RETRIEVAL_ALGORITHMS,
    retrieve_capabilities,
)

AUTHOR_SCHEMA: Final = "lazy-skill-router.portable-catalog-authoring/v1"
MANIFEST_SCHEMA_V1: Final = "lazy-skill-router.portable-beta-manifest/v1"
MANIFEST_SCHEMA_V2: Final = "lazy-skill-router.portable-beta-manifest/v2"
MANIFEST_SCHEMA: Final = MANIFEST_SCHEMA_V2
REPORT_SCHEMA: Final = "lazy-skill-router.portable-beta-report/v1"
DEFAULT_MANIFEST: Final = "eval/portable_beta_manifest.json"
CURRENT_EVIDENCE_ROLE: Final = "self-attested-internal-release-regression"
FIXED_INDEX_TIMESTAMP: Final = "2026-07-13T00:00:00Z"
MAX_JSON_BYTES: Final = 2 * 1024 * 1024
MAX_CATALOGS: Final = 32
MAX_SKILLS_PER_CATALOG: Final = 256
MAX_CASES_PER_CATALOG: Final = 512
MAX_SCENARIOS: Final = 64
MAX_TOTAL_EVALUATIONS: Final = 16_384
MAX_TEXT_CHARS: Final = 16_384
MAX_METADATA_VALUES: Final = 64
ID_RE: Final = re.compile(r"[a-z0-9][a-z0-9._:+-]{0,127}")
DIGEST_RE: Final = re.compile(r"sha256:[0-9a-f]{64}")
LANGUAGES: Final = frozenset({"en", "ko", "mixed"})

MANIFEST_FIELDS_V1: Final = frozenset(
    {
        "schema",
        "suiteId",
        "createdAt",
        "algorithm",
        "retrievalImplementationRevision",
        "maxCandidates",
        "fixtureFiles",
        "scenarios",
        "gate",
        "semantics",
    }
)
MANIFEST_FIELDS_V2: Final = MANIFEST_FIELDS_V1 | {
    "evaluationImplementationRevision",
    "indexSchema",
    "featureExtractor",
    "evidenceRole",
}
FIXTURE_RECORD_FIELDS: Final = frozenset({"path", "sha256", "authorId"})
AUTHOR_FIELDS: Final = frozenset({"schema", "authoring", "catalogs"})
AUTHORING_FIELDS: Final = frozenset({"authorId", "scorerAccess", "existingCorpusAccess", "purpose"})
CATALOG_FIELDS: Final = frozenset({"id", "description", "skills", "cases"})
SKILL_FIELDS: Final = frozenset({"name", "description", "aliases", "capabilities", "phases"})
CASE_FIELDS: Final = frozenset({"id", "prompt", "language", "category", "expectedSkills", "expectedNoMatch"})
SCENARIO_FIELDS: Final = frozenset({"id", "catalogs", "caseCatalogs"})
GATE_FIELDS: Final = frozenset(
    {
        "minAuthors",
        "minCatalogs",
        "minCases",
        "minPositiveCases",
        "minNoSkillCases",
        "minPositiveRecallAt3",
        "minPositiveTop1",
        "minNoSkillNoMatch",
        "minCatalogRecallAt3",
        "minCatalogNoSkillNoMatch",
        "minScenarioRecallAt3",
        "minScenarioNoSkillNoMatch",
        "minLanguageRecallAt3",
        "maxDegradedCases",
        "maxIneligibleCandidates",
        "maxP95LatencyMs",
    }
)
SEMANTIC_FIELDS: Final = frozenset(
    {
        "scope",
        "oneShot",
        "scorerFrozenBeforeEvaluation",
        "externalUserValidationSubstitute",
        "hookActivationAuthorized",
        "automaticRelease",
        "rawPromptsEmitted",
    }
)


@dataclass(frozen=True)
class SkillCard:
    name: str
    description: str
    aliases: tuple[str, ...]
    capabilities: tuple[str, ...]
    phases: tuple[str, ...]


@dataclass(frozen=True)
class PortableCase:
    case_id: str
    catalog_id: str
    prompt: str
    language: str
    category: str
    expected_skills: tuple[str, ...]
    expected_no_match: bool


@dataclass(frozen=True)
class Catalog:
    catalog_id: str
    description: str
    skills: tuple[SkillCard, ...]
    cases: tuple[PortableCase, ...]


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    catalog_ids: tuple[str, ...]
    case_catalog_ids: tuple[str, ...]


@dataclass(frozen=True)
class PortableSuite:
    suite_id: str
    manifest_schema: str
    manifest_revision: str
    algorithm: str
    retrieval_implementation_revision: str
    evaluation_implementation_revision: str | None
    index_schema: str
    feature_extractor: str
    evidence_role: str
    max_candidates: int
    author_ids: tuple[str, ...]
    fixture_revisions: tuple[tuple[str, str], ...]
    catalogs: tuple[Catalog, ...]
    scenarios: tuple[Scenario, ...]
    gate: dict[str, int | float]
    semantics: dict[str, Any]

    @property
    def cases(self) -> tuple[PortableCase, ...]:
        return tuple(case for catalog in self.catalogs for case in catalog.cases)


@dataclass(frozen=True)
class CaseResult:
    scenario_id: str
    case: PortableCase
    status: str
    candidate_names: tuple[str, ...]
    reason_codes: tuple[str, ...]
    ineligible_candidates: tuple[str, ...]
    latency_ms: float

    @property
    def degraded(self) -> bool:
        return self.status not in {"matched", "no-match"}

    @property
    def recall_at_3_passed(self) -> bool:
        return bool(set(self.case.expected_skills) & set(self.candidate_names[:3]))

    @property
    def top1_passed(self) -> bool:
        return bool(self.candidate_names) and self.candidate_names[0] in self.case.expected_skills

    @property
    def no_skill_no_match_passed(self) -> bool:
        return self.status == "no-match" and not self.candidate_names


def canonical_revision(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def bytes_revision(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def exact_fields(raw: Any, expected: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be an object")
    unknown = sorted(set(raw) - expected)
    missing = sorted(expected - set(raw))
    if unknown:
        raise ValueError(f"{label} has unknown fields: {', '.join(unknown)}")
    if missing:
        raise ValueError(f"{label} is missing fields: {', '.join(missing)}")
    return raw


def required_string(raw: dict[str, Any], field: str, label: str, *, identifier: bool = False) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value or len(value) > MAX_TEXT_CHARS:
        raise ValueError(f"{label}.{field} must be a bounded non-empty string")
    if identifier and ID_RE.fullmatch(value) is None:
        raise ValueError(f"{label}.{field} must be an identifier")
    return value


def required_bool(raw: dict[str, Any], field: str, label: str) -> bool:
    value = raw.get(field)
    if not isinstance(value, bool):
        raise ValueError(f"{label}.{field} must be a boolean")
    return value


def string_array(
    raw: dict[str, Any],
    field: str,
    label: str,
    *,
    allow_empty: bool = True,
    identifiers: bool = False,
) -> tuple[str, ...]:
    value = raw.get(field)
    if not isinstance(value, list) or len(value) > MAX_METADATA_VALUES:
        raise ValueError(f"{label}.{field} must be a bounded string array")
    if not allow_empty and not value:
        raise ValueError(f"{label}.{field} must not be empty")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > MAX_TEXT_CHARS:
            raise ValueError(f"{label}.{field} must contain bounded non-empty strings")
        if identifiers and ID_RE.fullmatch(item) is None:
            raise ValueError(f"{label}.{field} must contain identifiers")
        result.append(item)
    if len(set(result)) != len(result):
        raise ValueError(f"{label}.{field} must not contain duplicates")
    return tuple(result)


def reject_nonfinite_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is not allowed: {value}")


def read_json_object(
    path: Path,
    label: str,
    *,
    managed_root: Path | None = None,
) -> tuple[dict[str, Any], bytes]:
    source = path.expanduser().absolute()
    boundary = managed_root.expanduser().absolute() if managed_root is not None else source.parent
    try:
        encoded, _ = confined_read_regular_snapshot(source, boundary, MAX_JSON_BYTES)
    except (OSError, ValueError) as exc:
        raise ValueError(f"{label} is unavailable or unsafe") from exc
    if encoded is None:
        raise ValueError(f"{label} exceeds the size limit")
    try:
        raw = json.loads(
            encoded.decode("utf-8"),
            parse_constant=reject_nonfinite_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is unreadable") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be an object")
    return raw, encoded


def safe_fixture_path(manifest_path: Path, raw_path: str) -> Path:
    relative = Path(raw_path)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError("fixture path must be a confined relative path")
    return manifest_path.parent / relative


def parse_skill(raw: Any, label: str) -> SkillCard:
    data = exact_fields(raw, SKILL_FIELDS, label)
    return SkillCard(
        name=required_string(data, "name", label, identifier=True),
        description=required_string(data, "description", label),
        aliases=string_array(data, "aliases", label),
        capabilities=string_array(data, "capabilities", label),
        phases=string_array(data, "phases", label),
    )


def parse_case(raw: Any, catalog_id: str, skill_names: frozenset[str], label: str) -> PortableCase:
    data = exact_fields(raw, CASE_FIELDS, label)
    expected_no_match = required_bool(data, "expectedNoMatch", label)
    expected_skills = string_array(data, "expectedSkills", label, identifiers=True)
    if expected_no_match and expected_skills:
        raise ValueError(f"{label}.expectedSkills must be empty for a no-skill case")
    if not expected_no_match and not expected_skills:
        raise ValueError(f"{label}.expectedSkills must not be empty for a positive case")
    unknown = sorted(set(expected_skills) - skill_names)
    if unknown:
        raise ValueError(f"{label}.expectedSkills references unknown skills: {', '.join(unknown)}")
    language = required_string(data, "language", label)
    if language not in LANGUAGES:
        raise ValueError(f"{label}.language is unsupported")
    return PortableCase(
        case_id=required_string(data, "id", label, identifier=True),
        catalog_id=catalog_id,
        prompt=required_string(data, "prompt", label),
        language=language,
        category=required_string(data, "category", label, identifier=True),
        expected_skills=expected_skills,
        expected_no_match=expected_no_match,
    )


def parse_catalog(raw: Any, label: str) -> Catalog:
    data = exact_fields(raw, CATALOG_FIELDS, label)
    catalog_id = required_string(data, "id", label, identifier=True)
    raw_skills = data.get("skills")
    if not isinstance(raw_skills, list) or not 1 <= len(raw_skills) <= MAX_SKILLS_PER_CATALOG:
        raise ValueError(f"{label}.skills has an invalid size")
    skills = tuple(parse_skill(value, f"{label}.skills[{offset}]") for offset, value in enumerate(raw_skills))
    skill_names = [skill.name for skill in skills]
    if len(set(skill_names)) != len(skill_names):
        raise ValueError(f"{label}.skills contains duplicate names")
    raw_cases = data.get("cases")
    if not isinstance(raw_cases, list) or not 1 <= len(raw_cases) <= MAX_CASES_PER_CATALOG:
        raise ValueError(f"{label}.cases has an invalid size")
    cases = tuple(
        parse_case(value, catalog_id, frozenset(skill_names), f"{label}.cases[{offset}]")
        for offset, value in enumerate(raw_cases)
    )
    case_ids = [case.case_id for case in cases]
    if len(set(case_ids)) != len(case_ids):
        raise ValueError(f"{label}.cases contains duplicate ids")
    return Catalog(
        catalog_id=catalog_id,
        description=required_string(data, "description", label),
        skills=skills,
        cases=cases,
    )


def parse_gate(raw: Any) -> dict[str, int | float]:
    data = exact_fields(raw, GATE_FIELDS, "manifest.gate")
    integer_fields = {
        "minAuthors",
        "minCatalogs",
        "minCases",
        "minPositiveCases",
        "minNoSkillCases",
        "maxDegradedCases",
        "maxIneligibleCandidates",
    }
    ratio_fields = {
        "minPositiveRecallAt3",
        "minPositiveTop1",
        "minNoSkillNoMatch",
        "minCatalogRecallAt3",
        "minCatalogNoSkillNoMatch",
        "minScenarioRecallAt3",
        "minScenarioNoSkillNoMatch",
        "minLanguageRecallAt3",
    }
    result: dict[str, int | float] = {}
    for field in integer_fields:
        value = data.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"manifest.gate.{field} must be a non-negative integer")
        result[field] = value
    for field in ratio_fields:
        value = data.get(field)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not 0.0 <= float(value) <= 1.0
        ):
            raise ValueError(f"manifest.gate.{field} must be a ratio")
        result[field] = float(value)
    latency = data.get("maxP95LatencyMs")
    if (
        isinstance(latency, bool)
        or not isinstance(latency, (int, float))
        or not math.isfinite(float(latency))
        or float(latency) <= 0.0
    ):
        raise ValueError("manifest.gate.maxP95LatencyMs must be finite and positive")
    result["maxP95LatencyMs"] = float(latency)
    return result


def parse_semantics(raw: Any, *, repeated_regression: bool) -> dict[str, Any]:
    data = exact_fields(raw, SEMANTIC_FIELDS, "manifest.semantics")
    if data.get("scope") != "explicit-cli-preview-only":
        raise ValueError("manifest.semantics.scope must remain explicit-cli-preview-only")
    required_true = {"scorerFrozenBeforeEvaluation"}
    required_false = {
        "externalUserValidationSubstitute",
        "hookActivationAuthorized",
        "automaticRelease",
        "rawPromptsEmitted",
    }
    if repeated_regression:
        required_false.add("oneShot")
    else:
        required_true.add("oneShot")
    for field in required_true | required_false:
        required_bool(data, field, "manifest.semantics")
    if any(data[field] is not True for field in required_true):
        raise ValueError("manifest.semantics required true flags are invalid")
    if any(data[field] is not False for field in required_false):
        raise ValueError("manifest.semantics authority and emission flags must remain false")
    return dict(data)


def parse_scenarios(raw: Any, catalog_ids: frozenset[str]) -> tuple[Scenario, ...]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("manifest.scenarios must be a non-empty array")
    if len(raw) > MAX_SCENARIOS:
        raise ValueError("manifest.scenarios exceeds the scenario limit")
    scenarios: list[Scenario] = []
    for offset, value in enumerate(raw):
        label = f"manifest.scenarios[{offset}]"
        data = exact_fields(value, SCENARIO_FIELDS, label)
        catalogs = string_array(data, "catalogs", label, allow_empty=False, identifiers=True)
        case_catalogs = string_array(data, "caseCatalogs", label, allow_empty=False, identifiers=True)
        if set(catalogs) - catalog_ids or set(case_catalogs) - catalog_ids:
            raise ValueError(f"{label} references unknown catalogs")
        if set(case_catalogs) - set(catalogs):
            raise ValueError(f"{label}.caseCatalogs must be included in catalogs")
        scenarios.append(
            Scenario(
                required_string(data, "id", label, identifier=True),
                catalogs,
                case_catalogs,
            )
        )
    scenario_ids = [scenario.scenario_id for scenario in scenarios]
    if len(set(scenario_ids)) != len(scenario_ids):
        raise ValueError("manifest.scenarios contains duplicate ids")
    return tuple(scenarios)


def load_suite(path: Path) -> PortableSuite:
    manifest_path = path.expanduser().absolute()
    manifest_root = manifest_path.parent
    raw_manifest, manifest_bytes = read_json_object(
        manifest_path,
        "portable beta manifest",
        managed_root=manifest_root,
    )
    manifest_schema = raw_manifest.get("schema")
    if manifest_schema == MANIFEST_SCHEMA_V1:
        manifest = exact_fields(raw_manifest, MANIFEST_FIELDS_V1, "manifest")
        evaluation_revision = evaluation_implementation_revision()
        if evaluation_revision is None:
            raise ValueError("evaluation implementation revision is unavailable")
        index_schema = CAPABILITY_INDEX_SCHEMA_V1
        feature_extractor = FEATURE_EXTRACTOR_V1
        evidence_role = "self-attested-internal"
        repeated_regression = False
    elif manifest_schema == MANIFEST_SCHEMA_V2:
        manifest = exact_fields(raw_manifest, MANIFEST_FIELDS_V2, "manifest")
        evaluation_revision = required_string(manifest, "evaluationImplementationRevision", "manifest")
        if evaluation_revision != evaluation_implementation_revision():
            raise ValueError("evaluation implementation revision mismatch")
        index_schema = required_string(manifest, "indexSchema", "manifest")
        if index_schema != CAPABILITY_INDEX_SCHEMA:
            raise ValueError("manifest index schema is unsupported")
        feature_extractor = required_string(manifest, "featureExtractor", "manifest")
        if feature_extractor != FEATURE_EXTRACTOR_V1:
            raise ValueError("manifest feature extractor is unsupported")
        evidence_role = required_string(manifest, "evidenceRole", "manifest")
        if evidence_role != CURRENT_EVIDENCE_ROLE:
            raise ValueError("manifest evidence role is unsupported")
        repeated_regression = True
    else:
        raise ValueError("manifest schema is unsupported")
    algorithm = required_string(manifest, "algorithm", "manifest")
    allowed_algorithms = (
        SUPPORTED_RETRIEVAL_ALGORITHMS if manifest_schema == MANIFEST_SCHEMA_V1 else {PRODUCT_PREVIEW_ALGORITHM}
    )
    if algorithm not in allowed_algorithms:
        raise ValueError("manifest algorithm is unsupported")
    frozen_implementation = required_string(manifest, "retrievalImplementationRevision", "manifest")
    if frozen_implementation != RETRIEVAL_IMPLEMENTATION_REVISION:
        raise ValueError("retrieval implementation revision mismatch")
    max_candidates = manifest.get("maxCandidates")
    if isinstance(max_candidates, bool) or not isinstance(max_candidates, int) or not 1 <= max_candidates <= 3:
        raise ValueError("manifest.maxCandidates must be an integer from 1 to 3")
    raw_fixtures = manifest.get("fixtureFiles")
    if not isinstance(raw_fixtures, list) or not 1 <= len(raw_fixtures) <= 8:
        raise ValueError("manifest.fixtureFiles has an invalid size")

    authors: list[str] = []
    fixture_revisions: list[tuple[str, str]] = []
    catalogs: list[Catalog] = []
    for offset, value in enumerate(raw_fixtures):
        label = f"manifest.fixtureFiles[{offset}]"
        record = exact_fields(value, FIXTURE_RECORD_FIELDS, label)
        relative_path = required_string(record, "path", label)
        expected_digest = required_string(record, "sha256", label)
        if DIGEST_RE.fullmatch(expected_digest) is None:
            raise ValueError(f"{label}.sha256 must be a SHA-256 digest")
        expected_author = required_string(record, "authorId", label, identifier=True)
        fixture_path = safe_fixture_path(manifest_path, relative_path)
        raw_fixture, fixture_bytes = read_json_object(
            fixture_path,
            f"fixture {expected_author}",
            managed_root=manifest_root,
        )
        actual_digest = bytes_revision(fixture_bytes)
        if actual_digest != expected_digest:
            raise ValueError(f"fixture digest mismatch for {expected_author}")
        fixture = exact_fields(raw_fixture, AUTHOR_FIELDS, f"fixture {expected_author}")
        if fixture.get("schema") != AUTHOR_SCHEMA:
            raise ValueError(f"fixture {expected_author} schema is unsupported")
        authoring = exact_fields(fixture.get("authoring"), AUTHORING_FIELDS, f"fixture {expected_author}.authoring")
        actual_author = required_string(authoring, "authorId", f"fixture {expected_author}.authoring", identifier=True)
        if actual_author != expected_author:
            raise ValueError(f"fixture author mismatch for {expected_author}")
        if required_bool(authoring, "scorerAccess", f"fixture {expected_author}.authoring"):
            raise ValueError(f"fixture {expected_author} was not scorer-isolated")
        if required_bool(authoring, "existingCorpusAccess", f"fixture {expected_author}.authoring"):
            raise ValueError(f"fixture {expected_author} was not corpus-isolated")
        if required_string(authoring, "purpose", f"fixture {expected_author}.authoring") != (
            "one-shot-opt-in-beta-holdout"
        ):
            raise ValueError(f"fixture {expected_author} purpose is invalid")
        raw_catalogs = fixture.get("catalogs")
        if not isinstance(raw_catalogs, list) or not 1 <= len(raw_catalogs) <= MAX_CATALOGS:
            raise ValueError(f"fixture {expected_author}.catalogs has an invalid size")
        authors.append(actual_author)
        fixture_revisions.append((actual_author, actual_digest))
        catalogs.extend(
            parse_catalog(raw_catalog, f"fixture {expected_author}.catalogs[{catalog_offset}]")
            for catalog_offset, raw_catalog in enumerate(raw_catalogs)
        )

    if len(set(authors)) != len(authors):
        raise ValueError("manifest.fixtureFiles contains duplicate authors")
    catalog_ids = [catalog.catalog_id for catalog in catalogs]
    if len(set(catalog_ids)) != len(catalog_ids):
        raise ValueError("portable suite contains duplicate catalog ids")
    all_case_ids = [case.case_id for catalog in catalogs for case in catalog.cases]
    if len(set(all_case_ids)) != len(all_case_ids):
        raise ValueError("portable suite contains duplicate case ids")
    scenarios = parse_scenarios(manifest.get("scenarios"), frozenset(catalog_ids))
    covered_case_catalogs = {catalog_id for scenario in scenarios for catalog_id in scenario.case_catalog_ids}
    if covered_case_catalogs != set(catalog_ids):
        raise ValueError("manifest.scenarios must evaluate every catalog")
    catalogs_by_id = {catalog.catalog_id: catalog for catalog in catalogs}
    total_evaluations = sum(
        len(catalogs_by_id[catalog_id].cases) for scenario in scenarios for catalog_id in scenario.case_catalog_ids
    )
    if total_evaluations > MAX_TOTAL_EVALUATIONS:
        raise ValueError("manifest.scenarios exceeds the total evaluation limit")
    for scenario in scenarios:
        scenario_skill_names = [
            skill.name for catalog_id in scenario.catalog_ids for skill in catalogs_by_id[catalog_id].skills
        ]
        if len(set(scenario_skill_names)) != len(scenario_skill_names):
            raise ValueError(f"scenario {scenario.scenario_id} contains duplicate configured skill names")
    required_string(manifest, "createdAt", "manifest")
    return PortableSuite(
        suite_id=required_string(manifest, "suiteId", "manifest", identifier=True),
        manifest_schema=str(manifest_schema),
        manifest_revision=bytes_revision(manifest_bytes),
        algorithm=algorithm,
        retrieval_implementation_revision=frozen_implementation,
        evaluation_implementation_revision=evaluation_revision,
        index_schema=index_schema,
        feature_extractor=feature_extractor,
        evidence_role=evidence_role,
        max_candidates=max_candidates,
        author_ids=tuple(sorted(authors)),
        fixture_revisions=tuple(sorted(fixture_revisions)),
        catalogs=tuple(sorted(catalogs, key=lambda catalog: catalog.catalog_id)),
        scenarios=scenarios,
        gate=parse_gate(manifest.get("gate")),
        semantics=parse_semantics(manifest.get("semantics"), repeated_regression=repeated_regression),
    )


def catalog_inventory(catalogs: tuple[Catalog, ...]) -> InventorySnapshot:
    skills: list[dict[str, Any]] = []
    for catalog in catalogs:
        for skill in catalog.skills:
            skills.append(
                {
                    "configured_name": skill.name,
                    "canonical_id": f"portable/{catalog.catalog_id}/{skill.name}",
                    "description": skill.description,
                    "aliases": list(skill.aliases),
                    "capabilities": list(skill.capabilities),
                    "phases": list(skill.phases),
                    "availability": {"status": "available"},
                }
            )
    skills.sort(key=lambda skill: (skill["configured_name"], skill["canonical_id"]))
    names = [str(skill["configured_name"]) for skill in skills]
    if len(set(names)) != len(names):
        raise ValueError("scenario catalogs contain duplicate configured skill names")
    revision = inventory_revision(skills)
    return InventorySnapshot("available", revision, tuple(skills))


def names_from_result(result: dict[str, Any]) -> tuple[str, ...]:
    candidates = result.get("candidates")
    if not isinstance(candidates, list):
        return ()
    names: list[str] = []
    for candidate in candidates:
        skill_ref = candidate.get("skillRef") if isinstance(candidate, dict) else None
        name = skill_ref.get("configuredName") if isinstance(skill_ref, dict) else None
        if isinstance(name, str) and name:
            names.append(name)
    return tuple(names)


def reason_codes_from_result(result: dict[str, Any]) -> tuple[str, ...]:
    value = result.get("reasonCodes")
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def evaluate_suite(suite: PortableSuite) -> dict[str, Any]:
    catalogs_by_id = {catalog.catalog_id: catalog for catalog in suite.catalogs}
    results: list[CaseResult] = []
    scenario_coverage: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="lazy-router-portable-") as temp_dir:
        temp_root = Path(temp_dir)
        for scenario in suite.scenarios:
            scenario_catalogs = tuple(catalogs_by_id[catalog_id] for catalog_id in scenario.catalog_ids)
            inventory = catalog_inventory(scenario_catalogs)
            index = build_capability_index(
                inventory,
                generated_at=FIXED_INDEX_TIMESTAMP,
                schema=suite.index_schema,
            )
            index_feature_extractor = index.get("feature_extractor", FEATURE_EXTRACTOR_V1)
            if index.get("schema") != suite.index_schema or index_feature_extractor != suite.feature_extractor:
                raise ValueError("portable capability index contract mismatch")
            index_path = temp_root / f"{scenario.scenario_id}.json"
            index_path.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
            configured_names = frozenset(skill.name for catalog in scenario_catalogs for skill in catalog.skills)
            scenario_cases = tuple(
                case for catalog_id in scenario.case_catalog_ids for case in catalogs_by_id[catalog_id].cases
            )
            scenario_coverage.append(
                {
                    "id": scenario.scenario_id,
                    "catalogs": list(scenario.catalog_ids),
                    "skillCount": len(configured_names),
                    "caseCount": len(scenario_cases),
                    "inventoryRevision": inventory.revision,
                    "indexRevision": index["revision"],
                    "indexSchema": index["schema"],
                    "featureExtractor": index_feature_extractor,
                }
            )
            config = {
                "capabilityRetrieval": {
                    "mode": "shadow",
                    "maxCandidates": suite.max_candidates,
                    "algorithm": suite.algorithm,
                }
            }
            for case in scenario_cases:
                started = time.perf_counter_ns()
                result = retrieve_capabilities(
                    case.prompt,
                    config,
                    inventory,
                    explicit_index=str(index_path),
                    force=True,
                    algorithm=suite.algorithm,
                    frozen_replay=suite.manifest_schema == MANIFEST_SCHEMA_V1,
                )
                latency_ms = round((time.perf_counter_ns() - started) / 1_000_000, 4)
                names = names_from_result(result)
                results.append(
                    CaseResult(
                        scenario_id=scenario.scenario_id,
                        case=case,
                        status=str(result.get("status", "invalid")),
                        candidate_names=names,
                        reason_codes=reason_codes_from_result(result),
                        ineligible_candidates=tuple(name for name in names if name not in configured_names),
                        latency_ms=latency_ms,
                    )
                )
    if evaluation_implementation_revision() != suite.evaluation_implementation_revision:
        raise ValueError("evaluation implementation changed during evaluation")
    return build_report(suite, tuple(results), tuple(scenario_coverage))


def ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def percentile(values: Iterable[float], percentile_value: float) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * percentile_value
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 4)
    interpolated = ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
    return round(interpolated, 4)


def metric(passed: int, total: int) -> dict[str, int | float | None]:
    return {"passed": passed, "total": total, "rate": ratio(passed, total)}


def result_summary(results: tuple[CaseResult, ...], *, include_latency: bool) -> dict[str, Any]:
    positive = tuple(result for result in results if not result.case.expected_no_match)
    no_skill = tuple(result for result in results if result.case.expected_no_match)
    summary: dict[str, Any] = {
        "evaluations": len(results),
        "positive": len(positive),
        "noSkill": len(no_skill),
        "positiveRecallAt3": metric(sum(result.recall_at_3_passed for result in positive), len(positive)),
        "positiveTop1": metric(sum(result.top1_passed for result in positive), len(positive)),
        "noSkillNoMatch": metric(
            sum(result.no_skill_no_match_passed for result in no_skill),
            len(no_skill),
        ),
        "degradedCases": sum(result.degraded for result in results),
        "ineligibleCandidates": sum(len(result.ineligible_candidates) for result in results),
    }
    if include_latency:
        values = [result.latency_ms for result in results]
        summary["latency"] = {
            "p50Ms": percentile(values, 0.50),
            "p95Ms": percentile(values, 0.95),
            "p99Ms": percentile(values, 0.99),
        }
    return summary


def grouped_summaries(results: tuple[CaseResult, ...], field: str) -> dict[str, Any]:
    grouped: dict[str, list[CaseResult]] = {}
    for result in results:
        if field == "scenario":
            key = result.scenario_id
        elif field == "catalog":
            key = result.case.catalog_id
        elif field == "language":
            key = result.case.language
        else:
            key = result.case.category
        grouped.setdefault(key, []).append(result)
    return {key: result_summary(tuple(values), include_latency=False) for key, values in sorted(grouped.items())}


def metric_rate(summary: dict[str, Any], field: str) -> float | None:
    value = summary.get(field)
    rate = value.get("rate") if isinstance(value, dict) else None
    return float(rate) if isinstance(rate, (int, float)) and not isinstance(rate, bool) else None


def below(rate: float | None, minimum: int | float) -> bool:
    return rate is None or rate < float(minimum)


def gate_payload(suite: PortableSuite, report: dict[str, Any]) -> dict[str, Any]:
    gate = suite.gate
    coverage = report["coverage"]
    metrics = report["metrics"]
    blockers: list[str] = []
    count_checks = (
        ("authors", "minAuthors", "author_count_below_minimum"),
        ("catalogs", "minCatalogs", "catalog_count_below_minimum"),
        ("cases", "minCases", "case_count_below_minimum"),
        ("positiveCases", "minPositiveCases", "positive_case_count_below_minimum"),
        ("noSkillCases", "minNoSkillCases", "no_skill_case_count_below_minimum"),
    )
    for observed, threshold, blocker in count_checks:
        if int(coverage[observed]) < int(gate[threshold]):
            blockers.append(blocker)
    rate_checks = (
        ("positiveRecallAt3", "minPositiveRecallAt3", "positive_recall_at_3_below_minimum"),
        ("positiveTop1", "minPositiveTop1", "positive_top1_below_minimum"),
        ("noSkillNoMatch", "minNoSkillNoMatch", "no_skill_no_match_below_minimum"),
    )
    for observed, threshold, blocker in rate_checks:
        if below(metric_rate(metrics, observed), gate[threshold]):
            blockers.append(blocker)
    slice_checks = (
        ("catalog", "minCatalogRecallAt3", "positiveRecallAt3", "catalog_recall_at_3_below_minimum"),
        ("catalog", "minCatalogNoSkillNoMatch", "noSkillNoMatch", "catalog_no_skill_no_match_below_minimum"),
        ("scenario", "minScenarioRecallAt3", "positiveRecallAt3", "scenario_recall_at_3_below_minimum"),
        (
            "scenario",
            "minScenarioNoSkillNoMatch",
            "noSkillNoMatch",
            "scenario_no_skill_no_match_below_minimum",
        ),
        ("language", "minLanguageRecallAt3", "positiveRecallAt3", "language_recall_at_3_below_minimum"),
    )
    slice_failures: dict[str, list[str]] = {}
    for slice_name, threshold, metric_name, blocker in slice_checks:
        failures = [
            key
            for key, summary in report["slices"][slice_name].items()
            if summary[metric_name]["total"] == 0 or below(metric_rate(summary, metric_name), gate[threshold])
        ]
        if failures:
            blockers.append(blocker)
            slice_failures[blocker] = failures
    if int(metrics["degradedCases"]) > int(gate["maxDegradedCases"]):
        blockers.append("degraded_cases_above_maximum")
    if int(metrics["ineligibleCandidates"]) > int(gate["maxIneligibleCandidates"]):
        blockers.append("ineligible_candidates_above_maximum")
    p95 = metrics["latency"]["p95Ms"]
    if not isinstance(p95, (int, float)) or not math.isfinite(float(p95)) or p95 > float(gate["maxP95LatencyMs"]):
        blockers.append("p95_latency_above_maximum")
    payload: dict[str, Any] = {
        "schema": "lazy-skill-router.portable-beta-gate/v1",
        "status": "blocked" if blockers else "eligible-for-opt-in-beta-review",
        "authority": "none",
        "automaticRelease": False,
        "hookActivationAuthorized": False,
        "externalUserValidationSubstitute": False,
        "evidenceRole": suite.evidence_role,
        "thresholds": gate,
        "blockers": list(dict.fromkeys(blockers)),
        "sliceFailures": slice_failures,
        "reportRevision": report["reportRevision"],
    }
    payload["gateRevision"] = canonical_revision(payload)
    return payload


def evaluation_implementation_revision() -> str | None:
    try:
        return bytes_revision(Path(__file__).read_bytes())
    except OSError:
        return None


def stable_report_payload(report: dict[str, Any]) -> dict[str, Any]:
    metrics = dict(report["metrics"])
    metrics.pop("latency", None)
    payload = {
        "schema": report["schema"],
        "suiteId": report["suiteId"],
        "manifestRevision": report["manifestRevision"],
        "algorithm": report["algorithm"],
        "retrievalImplementationRevision": report["retrievalImplementationRevision"],
        "evaluationImplementationRevision": report["evaluationImplementationRevision"],
        "fixtureRevisions": report["fixtureRevisions"],
        "coverage": report["coverage"],
        "scenarios": report["scenarios"],
        "metrics": metrics,
        "slices": report["slices"],
        "cases": report["cases"],
        "semantics": report["semantics"],
    }
    for field in ("manifestSchema", "indexSchema", "featureExtractor", "evidenceRole"):
        if field in report:
            payload[field] = report[field]
    return payload


def build_report(
    suite: PortableSuite,
    results: tuple[CaseResult, ...],
    scenario_coverage: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    unique_cases = suite.cases
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "suiteId": suite.suite_id,
        "manifestSchema": suite.manifest_schema,
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "manifestRevision": suite.manifest_revision,
        "algorithm": suite.algorithm,
        "indexSchema": suite.index_schema,
        "featureExtractor": suite.feature_extractor,
        "evidenceRole": suite.evidence_role,
        "retrievalImplementationRevision": suite.retrieval_implementation_revision,
        "evaluationImplementationRevision": suite.evaluation_implementation_revision,
        "fixtureRevisions": [
            {"authorId": author_id, "revision": revision} for author_id, revision in suite.fixture_revisions
        ],
        "coverage": {
            "authors": len(suite.author_ids),
            "catalogs": len(suite.catalogs),
            "scenarios": len(suite.scenarios),
            "cases": len(unique_cases),
            "positiveCases": sum(not case.expected_no_match for case in unique_cases),
            "noSkillCases": sum(case.expected_no_match for case in unique_cases),
            "languages": sorted({case.language for case in unique_cases}),
            "evaluations": len(results),
        },
        "scenarios": list(scenario_coverage),
        "metrics": result_summary(results, include_latency=True),
        "slices": {
            "scenario": grouped_summaries(results, "scenario"),
            "catalog": grouped_summaries(results, "catalog"),
            "language": grouped_summaries(results, "language"),
            "category": grouped_summaries(results, "category"),
        },
        "cases": [
            {
                "scenarioId": result.scenario_id,
                "id": result.case.case_id,
                "catalogId": result.case.catalog_id,
                "language": result.case.language,
                "category": result.case.category,
                "expectedSkills": list(result.case.expected_skills),
                "expectedNoMatch": result.case.expected_no_match,
                "status": result.status,
                "candidateNames": list(result.candidate_names),
                "recallAt3Passed": result.recall_at_3_passed if not result.case.expected_no_match else None,
                "top1Passed": result.top1_passed if not result.case.expected_no_match else None,
                "noSkillNoMatchPassed": (result.no_skill_no_match_passed if result.case.expected_no_match else None),
                "ineligibleCandidates": list(result.ineligible_candidates),
                "reasonCodes": list(result.reason_codes),
            }
            for result in results
        ],
        "semantics": {
            **suite.semantics,
            "evidenceRole": suite.evidence_role,
            "authority": "none",
            "candidateRankingOnly": True,
            "semanticAbstentionMeasured": False,
            "userOutcomeMeasured": False,
        },
        "environment": {
            "python": platform.python_version(),
            "platform": platform.system().lower(),
        },
    }
    report["reportRevision"] = canonical_revision(stable_report_payload(report))
    report["gate"] = gate_payload(suite, report)
    report["runRevision"] = canonical_revision(report)
    return report


def print_text_report(report: dict[str, Any]) -> None:
    coverage = report["coverage"]
    metrics = report["metrics"]
    print(
        f"Portable opt-in beta evaluation: {coverage['cases']} unique cases, "
        f"{coverage['evaluations']} scenario evaluations"
    )
    print(f"Catalogs: {coverage['catalogs']}; scenarios: {coverage['scenarios']}; authors: {coverage['authors']}")
    print(
        "Positive Recall@3: "
        f"{metrics['positiveRecallAt3']['passed']}/{metrics['positiveRecallAt3']['total']} "
        f"({metrics['positiveRecallAt3']['rate']})"
    )
    print(
        "Positive Top-1: "
        f"{metrics['positiveTop1']['passed']}/{metrics['positiveTop1']['total']} "
        f"({metrics['positiveTop1']['rate']})"
    )
    print(
        "No-skill no-match: "
        f"{metrics['noSkillNoMatch']['passed']}/{metrics['noSkillNoMatch']['total']} "
        f"({metrics['noSkillNoMatch']['rate']})"
    )
    print(f"p95 latency: {metrics['latency']['p95Ms']} ms")
    print(f"Gate: {report['gate']['status']}")
    for blocker in report["gate"]["blockers"]:
        print(f"- {blocker}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the frozen portable multi-catalog gate for the explicit non-activating beta preview."
    )
    parser.add_argument("manifest", nargs="?", default=DEFAULT_MANIFEST, help="Portable beta manifest path.")
    parser.add_argument("--json", action="store_true", help="Print the prompt-redacted JSON report.")
    parser.add_argument("--output", help="Write the prompt-redacted JSON report atomically.")
    args = parser.parse_args(argv)
    try:
        suite = load_suite(Path(args.manifest))
        report = evaluate_suite(suite)
        if args.output:
            output_path = Path(args.output).expanduser().absolute()
            write_json_atomic(output_path, report, managed_root=Path.cwd())
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception:
        print("ERROR: unexpected evaluator failure", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_text_report(report)
    return 0 if report["gate"]["status"] == "eligible-for-opt-in-beta-review" else 1


if __name__ == "__main__":
    raise SystemExit(main())
