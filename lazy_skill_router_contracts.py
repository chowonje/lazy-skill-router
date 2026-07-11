from __future__ import annotations

from typing import Any
from urllib.parse import quote

from lazy_skill_router_activation import activation_ir_dict, decide_activation
from lazy_skill_router_core import answer_only_patterns, parse_routes
from lazy_skill_router_inventory import InventorySnapshot
from lazy_skill_router_scoring import RouteMatch, ranked_route_matches_v2, text_matches

ROUTE_RESULT_CONTRACT_VERSION = 2
DEFAULT_MAX_RECOMMENDATIONS = 3
DEFAULT_MIN_SCORE_MARGIN = 0.05
RECOMMENDATION_CONTRACT_NAME = "lazy-skill-router.skill-recommendation"
RECOMMENDATION_CONTRACT_VERSION = "1.0"
HOOK_IR_SCHEMA = "lazy-skill-router.hook-ir/v1"
MAX_HOOK_IR_SKILLS = 8


def configured_number(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(0.0, min(1.0, float(value)))


def selection_config(config: dict[str, Any]) -> dict[str, Any]:
    value = config.get("selection")
    return value if isinstance(value, dict) else {}


def max_recommendations(config: dict[str, Any]) -> int:
    value = selection_config(config).get("maxRecommendations", DEFAULT_MAX_RECOMMENDATIONS)
    if isinstance(value, bool) or not isinstance(value, int):
        return DEFAULT_MAX_RECOMMENDATIONS
    return max(1, min(DEFAULT_MAX_RECOMMENDATIONS, value))


def min_score_margin(config: dict[str, Any]) -> float:
    return configured_number(selection_config(config).get("minScoreMargin"), DEFAULT_MIN_SCORE_MARGIN)


def policy_version(config: dict[str, Any]) -> str:
    configured = config.get("policyVersion")
    if isinstance(configured, str) and configured:
        return configured
    legacy = config.get("version", 1)
    if isinstance(legacy, bool) or not isinstance(legacy, (str, int, float)):
        legacy = 1
    return f"route-v1:{legacy}"


def score_margin(matches: tuple[RouteMatch, ...], index: int) -> float | None:
    if index + 1 >= len(matches):
        return None
    return round(max(0.0, matches[index].score - matches[index + 1].score), 4)


def capability_requirements(match: RouteMatch) -> dict[str, list[str]]:
    requirements = match.route.capability_requirements
    return {
        "primary": list(requirements.primary),
        "supporting": list(requirements.supporting),
        "verification": list(requirements.verification),
    }


def recommendation(match: RouteMatch, matches: tuple[RouteMatch, ...], index: int) -> dict[str, Any]:
    route = match.route
    return {
        "route_id": route.name,
        "rank": index + 1,
        "intent": route.intent,
        "match_strength": round(match.confidence, 2),
        "score": round(match.score, 2),
        "score_margin": score_margin(matches, index),
        "matched_pattern_ids": list(match.matched_pattern_ids),
        "capability_requirements": capability_requirements(match),
        "legacy_skill_projection": {
            "primary": route.primary,
            "supporting": list(route.supporting),
            "verification": route.verification or None,
        },
    }


def contract_matches(
    prompt: str,
    config: dict[str, Any],
    inventory: InventorySnapshot | None = None,
) -> tuple[RouteMatch, ...]:
    routes = [route for route in parse_routes(config, inventory) if route.lifecycle_state == "active"]
    return ranked_route_matches_v2(prompt, routes, config)


def route_result_v2(
    prompt: str,
    config: dict[str, Any],
    inventory: InventorySnapshot | None = None,
) -> dict[str, Any]:
    matches = contract_matches(prompt, config, inventory)
    fallback_used = bool(matches and matches[0].route.fallback)
    top_margin = score_margin(matches, 0)
    ambiguous = bool(len(matches) > 1 and top_margin is not None and top_margin < min_score_margin(config))
    activation = decide_activation(
        prompt,
        matches,
        config,
        answer_only=text_matches(prompt, answer_only_patterns(config)),
    )
    if activation.disposition == "abstain":
        status = "abstained" if matches else "no-match"
        bounded = ()
    else:
        status = "ambiguous" if ambiguous else "matched" if matches else "no-match"
        bounded = matches[: max_recommendations(config)]

    return {
        "contract": "lazy-skill-router.route-result/v2",
        "contract_version": ROUTE_RESULT_CONTRACT_VERSION,
        "policy_version": policy_version(config),
        "status": status,
        "match_strength_semantics": "not_probability",
        "recommendations": [recommendation(match, matches, index) for index, match in enumerate(bounded)],
        "fallback_used": fallback_used,
        "fallback_reason": "no_matching_normal_route" if fallback_used else None,
        "ambiguous": ambiguous,
        "activation": activation_ir_dict(activation),
        "compatibility": {
            "legacy_route_v1_top1": True,
            "should_inject_means_context_delivery": True,
        },
    }


def configured_skill_ref(skill: str) -> dict[str, Any]:
    if ":" in skill:
        namespace, name = skill.split(":", 1)
        provider_type = "plugin"
        provider_id = namespace
    else:
        namespace = "default"
        name = skill
        provider_type = "configured"
        provider_id = "local"
    segments = (provider_type, provider_id, namespace, name)
    canonical_id = "/".join(quote(segment, safe="-._~") for segment in segments)
    return {
        "canonical_id": canonical_id,
        "provider": {"type": provider_type, "id": provider_id},
        "namespace": namespace,
        "name": name,
        "configured_name": skill,
        "identity_source": "configured-name-adapter",
    }


def inventory_skill_ref(skill: dict[str, Any]) -> dict[str, Any]:
    skill_ref = {
        key: skill.get(key)
        for key in (
            "canonical_id",
            "provider",
            "namespace",
            "name",
            "configured_name",
            "revision",
            "provenance_ref",
            "locator_ref",
            "aliases",
            "content_digest",
        )
    }
    skill_ref["identity_source"] = "generated-manifest"
    return skill_ref


def resolved_skill(skill: str, inventory: InventorySnapshot | None) -> tuple[dict[str, Any], dict[str, Any]]:
    if inventory is not None and inventory.state == "available":
        resolved = inventory.resolve(skill)
        if resolved is not None:
            availability = resolved.get("availability")
            if not isinstance(availability, dict):
                availability = {
                    "status": "unknown",
                    "reason_codes": ["inventory_availability_invalid"],
                    "authorization": False,
                }
            return inventory_skill_ref(resolved), availability
        reason = "inventory_identity_ambiguous" if inventory.match_count(skill) > 1 else "inventory_skill_missing"
        return configured_skill_ref(skill), {
            "status": "unknown",
            "reason_codes": [reason],
            "authorization": False,
        }
    reason = (
        "inventory_snapshot_invalid"
        if inventory is not None and inventory.state == "invalid"
        else ("inventory_snapshot_unavailable")
    )
    return configured_skill_ref(skill), {
        "status": "unknown",
        "reason_codes": [reason],
        "authorization": False,
    }


def skill_recommendations(match: RouteMatch, inventory: InventorySnapshot | None) -> list[dict[str, Any]]:
    configured_skills = [("primary", match.route.primary)]
    configured_skills.extend(("supporting", skill) for skill in match.route.supporting)
    if match.route.verification:
        configured_skills.append(("verification", match.route.verification))
    evidence = [f"signal:{pattern_id}" for pattern_id in match.matched_pattern_ids]
    recommendations = []
    for role, skill in configured_skills:
        skill_ref, availability = resolved_skill(skill, inventory)
        recommendations.append(
            {
                "role": role,
                "skill_ref": skill_ref,
                "availability": availability,
                "selection_evidence": evidence,
            }
        )
    return recommendations


def structured_recommendation_v1(
    prompt: str,
    config: dict[str, Any],
    inventory: InventorySnapshot | None = None,
) -> dict[str, Any]:
    route_result = route_result_v2(prompt, config, inventory)
    matches = (
        ()
        if route_result["activation"]["disposition"] == "abstain"
        else contract_matches(prompt, config, inventory)[: max_recommendations(config)]
    )
    route_recommendations = route_result["recommendations"]
    activation = route_result["activation"]
    recommendations = []
    for index, match in enumerate(matches):
        route_recommendation = route_recommendations[index]
        requirements = route_recommendation["capability_requirements"]
        unresolved = requirements["primary"] + requirements["supporting"] + requirements["verification"]
        recommendations.append(
            {
                "recommendation_id": f"rec-{index + 1}",
                "route_id": route_recommendation["route_id"],
                "route_rank": route_recommendation["rank"],
                "match": {
                    "match_strength": route_recommendation["match_strength"],
                    "score_margin": route_recommendation["score_margin"],
                    "evidence_ids": route_recommendation["matched_pattern_ids"],
                },
                "intent": route_recommendation["intent"],
                "plan_kind": "unresolved_capabilities",
                "phases": [],
                "skills": [
                    {
                        **skill,
                        "activation_state": (
                            "activated"
                            if index == 0 and skill["role"] == "primary" and activation["disposition"] == "activate"
                            else "deferred"
                        ),
                    }
                    for skill in skill_recommendations(match, inventory)
                ],
                "unresolved_capabilities": unresolved,
            }
        )

    return {
        "contract": {"name": RECOMMENDATION_CONTRACT_NAME, "version": RECOMMENDATION_CONTRACT_VERSION},
        "producer": {
            "id": "lazy-skill-router",
            "inventory_revision": inventory.revision if inventory is not None else None,
            "inventory_state": inventory.state if inventory is not None else "missing",
            "config_trust": config.get("_config_trust", "unknown"),
        },
        "semantics": {
            "mode": "recommendation_only",
            "authority": "none",
            "agent_may_override": True,
            "must_reinspect_user_request": True,
            "must_not_override_higher_priority_instructions": True,
            "availability_is_authorization": False,
            "config_trust_is_authorization": False,
            "must_reauthorize_side_effects": True,
            "execution_requested": False,
        },
        "route_result_ref": {
            "contract_version": route_result["contract_version"],
            "policy_version": route_result["policy_version"],
            "status": route_result["status"],
            "route_ids": [item["route_id"] for item in route_recommendations],
            "fallback_used": route_result["fallback_used"],
            "fallback_reason": route_result["fallback_reason"],
            "ambiguous": route_result["ambiguous"],
            "activation_disposition": activation["disposition"],
            "activation_reason_code": activation["reasonCode"],
            "activation_request_mode": activation["intentFrame"]["requestMode"],
        },
        "recommendations": recommendations,
        "selection_notes": {
            "route_rank_is_not_execution_order": True,
            "no_eligible_skill_behavior": "emit_unresolved_capability_and_continue",
            "cross_hook_conflicts": "defer_to_agent_instruction_and_trust_resolution",
        },
        "compatibility": {
            "legacy_route_v1_top1_input": True,
            "legacy_top1_prose_adapter": False,
            "shadow_mode": True,
        },
    }


def advisory_phase(role: str, answer_only: bool) -> str:
    if answer_only:
        return "explain"
    if role == "verification":
        return "verify"
    return "inspect"


def hook_ir_v1(
    prompt: str,
    config: dict[str, Any],
    inventory: InventorySnapshot | None = None,
) -> dict[str, Any]:
    recommendation_contract = structured_recommendation_v1(prompt, config, inventory)
    recommendations = recommendation_contract["recommendations"]
    route_ref = recommendation_contract["route_result_ref"]
    answer_only = route_ref["activation_request_mode"] == "answer-only"
    ir_routes: list[dict[str, Any]] = []
    for recommendation_item in recommendations:
        for skill in recommendation_item["skills"]:
            if len(ir_routes) >= MAX_HOOK_IR_SKILLS:
                break
            skill_ref = skill["skill_ref"]
            availability = skill["availability"]
            ir_routes.append(
                {
                    "route_id": recommendation_item["route_id"],
                    "rank": recommendation_item["route_rank"],
                    "skill_ref": skill_ref["canonical_id"],
                    "role": skill["role"],
                    "phase": advisory_phase(skill["role"], answer_only),
                    "evidence": skill["selection_evidence"],
                    "eligibility": availability["status"],
                    "trust": recommendation_contract["producer"]["config_trust"],
                    "risk_hint": "advisory",
                    "activation_state": skill["activation_state"],
                    "unresolved_capability": recommendation_item["unresolved_capabilities"],
                }
            )
        if len(ir_routes) >= MAX_HOOK_IR_SKILLS:
            break

    first_match = recommendations[0]["match"] if recommendations else {}
    inventory_state = recommendation_contract["producer"]["inventory_state"]
    degradation_reasons = []
    if inventory_state != "available":
        degradation_reasons.append(f"inventory_{inventory_state}")
    return {
        "schema": HOOK_IR_SCHEMA,
        "producer": {
            "name": "lazy-skill-router",
            "revision": recommendation_contract["producer"]["inventory_revision"] or route_ref["policy_version"],
        },
        "decision": {
            "status": route_ref["status"],
            "match_strength": first_match.get("match_strength", 0.0),
            "score_margin": first_match.get("score_margin"),
            "activation_disposition": route_ref["activation_disposition"],
            "activation_reason_code": route_ref["activation_reason_code"],
        },
        "routes": ir_routes,
        "inventory": {
            "manifest_revision": recommendation_contract["producer"]["inventory_revision"],
            "snapshot_age": "unknown",
            "provenance": "generated-manifest" if inventory_state == "available" else "configured-name-adapter",
            "state": inventory_state,
        },
        "degradation": {
            "mode": "none" if not degradation_reasons else "degraded",
            "reasons": degradation_reasons,
        },
        "semantics": {
            "authority": "none",
            "agent_may_override": True,
            "must_reauthorize_side_effects": True,
            "route_rank_is_not_execution_order": True,
        },
    }
