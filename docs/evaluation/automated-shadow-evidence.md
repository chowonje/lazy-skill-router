# Automated shadow evidence protocol

## 결론

사람이 작성한 holdout 없이 수집할 수 있는 범위는 **prospective explicit-skill-reference slice**로 제한한다.
이 lane은 실제 미래 요청에서 `$ponytail`, `skill code-review`, `github:github 스킬`처럼 사용자가 스킬 이름을
명시한 경우만 로컬에서 결정론적으로 라벨링한다. 일반 의미 ownership과 no-skill 판단은 만들지 않는다.

출력은 `AutomatedShadowEvidenceV1`이며 항상 다음 경계를 유지한다.

```json
{
  "promotionStatus": "blocked",
  "authority": "none",
  "autoPromote": false,
  "provesIndependence": false,
  "provesQuality": false,
  "provesSemanticOwnership": false,
  "provesSemanticAbstention": false
}
```

## 수집 계약

- 입력: current-schema `RoutingObservationV1`이 있는 prospective capability shadow decision
- objective signal: inventory에서 하나로 resolve되는 exact configured name, 최대 3개
- 허용 표기: `$configured-name`, `skill configured-name`, `configured-name skill`, `스킬 configured-name`,
  `configured-name 스킬` 중 같은 절에 명시적인 use/apply/invoke 또는 사용/적용/실행 의도가 있는 경우
- 제외: 질문, 설치 여부 확인, `do not`/`without`/`말고` 같은 부정·제외 절
- 저장: configured skill ID, fixed reason code, prompt hash; raw prompt는 저장하지 않음
- 집계: prompt hash로 중복을 제거하지만 hash 자체는 evidence report에 내보내지 않음
- complete retrieval miss: objective signal을 ranking 전에 계산하므로 Recall@3 실패로 집계됨
- provenance: retrieval revision, parser revision, policy/config/catalog/runtime revision이 모두 단일해야 함

Collection gate의 기본 중단선은 다음과 같다.

| 항목 | 기준 |
|---|---:|
| unique explicit-reference cases | 100 이상 |
| explicit-reference Recall@3 | 95% 이상 |
| explicit-reference Top-1 | 90% 이상 |
| degraded observation | 0 |
| invalid observation/signal | 0 |
| candidate p95 | 20ms 이하 |
| legacy selection affected | 0 |
| automatic promotion requested | 0 |

통과 상태는 `ready-for-automated-shadow-review`다. 이는 collection lane의 운영 준비만 뜻하고 behavior
promotion 자격이 아니다.

## 실행

```bash
lazy-skill-router shadow-evidence \
  --config ~/.codex/lazy-skill-router/routes.json \
  --json
```

2026-07-13 baseline은 기존 형식 capability decision `50`건, valid `RoutingObservationV1` `0`건,
objective signal `0`건이다. 관찰 시도는 있었지만 현재 계약으로 검증할 수 없으므로 collection과 promotion은
모두 `blocked`다. 저장 artifact revision은
`sha256:20740bbaf6758d448637cc259051a6b9ba17a1a6366bcf8fe27df3b8fed8357a`다.

근거: [`eval/results/automated_shadow_evidence_2026-07-13.json`](../../eval/results/automated_shadow_evidence_2026-07-13.json)

## 재현 입력 보존

Control/pilot replay 입력은 Git 밖의 private CAS에 보존했다. 기본 저장소는
`~/.codex/private/lazy-skill-router/router-ab`이며 descriptor는 source 절대경로를 포함하지 않는다.

| bundle | descriptor revision | manifest revision |
|---|---|---|
| control-2026-07-13-v2 | `sha256:ff94b2c53f1b42b8a0220bb021f979d3ef617ec48982a519bf6bee07e241cddf` | `sha256:70e2ea29f6e065e37f998583c100a426a197b64df3665ded65cf591208b63c76` |
| bilingual-pilot-2026-07-13-v2 | `sha256:bb151ce6673e73244d5aff869687992a3a0d8cd5da5976f36422098e69a4d859` | `sha256:3ba4f3ff650353262266fbe5b895c02e9127fca4a5498c186528941bd6f8f422` |

CAS는 byte SHA-256으로 중복 제거하고, directory/file mode를 각각 `0700`/`0600`으로 제한한다. 저장 후에는
CAS blob을 다시 읽어 experiment manifest의 config/inventory/index revision과 일치하는지 검증한다.

## 영구 한계

자동 lane만 사용하는 동안 다음 promotion blocker는 제거할 수 없다.

- `explicit_reference_scope_only`
- `independent_holdout_not_proven`
- `independent_adjudication_not_proven`
- `ownership_unobserved`
- `semantic_abstention_unobserved`
- `outcome_runtime_linkage_unavailable`

따라서 사람 작성 holdout을 제외하는 선택은 가능하지만, 그 대신 behavior promotion도 계속 제외된다.
