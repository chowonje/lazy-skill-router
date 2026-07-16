# Portable opt-in beta gate — 2026-07-13

## 결론

`v0.5.0b1` 공개는 **차단**한다. 이 평가는 훅 자동 활성화가 아니라 사람이 직접 실행하는 비활성
`route` 미리보기를 위한 별도 beta gate다. 결과에 맞춰 scorer, fixture, 기준을 다시 조정하지 않았다.

사전 기준 중 Positive Top-1, no-skill no-match, degraded/ineligible, latency는 통과했지만 전체 Positive
Recall@3와 한국어 Recall@3가 미달했다. 따라서 버전은 `0.5.0.dev0`으로 유지하며 tag, PyPI, GitHub Release를
생성하지 않는다.

## 격리와 범위

- scorer와 기존 평가 corpus를 보지 않은 두 격리 작성자가 fixture를 별도로 작성했다.
- 4개 fictional catalog, 40개 고유 skill, 64개 고유 case를 포함한다.
- case 구성은 positive 48개, true no-skill 16개이며 `en`, `ko`, `mixed`를 포함한다.
- 동일한 동결 fixture를 10-skill 단독 4개, 20-skill 작성자 조합 2개, 40-skill 전체 조합 1개로 재생했다.
- 총 192개 scenario evaluation을 수행했다.
- 이 격리는 self-attested internal holdout이며 외부 사용자 검증을 대체하지 않는다.

## 사전 선언 기준과 결과

| Metric | Gate | Result | Status |
|---|---:|---:|---|
| Positive Recall@3 | >= 80% | 111/144 (77.08%) | fail |
| Positive Top-1 | >= 60% | 110/144 (76.39%) | pass |
| No-skill no-match | >= 50% | 36/48 (75.00%) | pass |
| Worst catalog Recall@3 | >= 65% | 66.67% | pass |
| Worst scenario Recall@3 | >= 65% | 66.67% | pass |
| English Recall@3 | >= 65% | 95.45% | pass |
| Korean Recall@3 | >= 65% | 33.33% | fail |
| Mixed Recall@3 | >= 65% | 100.00% | pass |
| Degraded / ineligible | 0 / 0 | 0 / 0 | pass |
| p95 latency | <= 20ms | 3.6524ms | pass |

Gate blockers:

- `positive_recall_at_3_below_minimum`
- `language_recall_at_3_below_minimum` (`ko`)

낮은 한국어 Recall은 현재 metadata가 언어 독립적이지 않다는 뜻이다. 반대로 한국어 no-skill no-match는
95.83%였으므로 문제를 단순 threshold 조정이나 광범위 후보 허용으로 해결하면 오탐·미탐 경계를 다시
흐릴 수 있다.

## 개인정보와 재현성

체크인 report에는 prompt, description, alias/capability 원문, 절대 경로가 없다. case/catalog/scenario ID,
기대 skill ID, 후보 skill ID, 상태, 집계, 고정 revision만 남긴다.

| Artifact | Revision |
|---|---|
| manifest | `sha256:5f87e73f04040fb4bf9f918d09f785147b62d4995838c07e18c39d7b9fed5057` |
| scorer implementation | `sha256:18fb5cd91ca2d3e10ca8b49ca438ad3b2139152ba34f31c17d636b5892804f46` |
| evaluator implementation | `sha256:e43b23714146e474816694a22091b00ddc358f5d46b5bd14d07f21239aef44a7` |
| stable report | `sha256:a8706ca2690bc63a42a4524289cc5f24fe4718c25456d440301af8d3a8d086b7` |
| gate | `sha256:3466c65cc10dd4823220f940d686ccacfa7651ca2c00b8da56b41911af52aa5b` |
| full run | `sha256:1bd8cfb431ff674fd98eb5a337053576101c95305425a7ef8c76ba60cdf0c96d` |

Prompt-redacted JSON report:
[`portable-beta-report-2026-07-13.json`](portable-beta-report-2026-07-13.json)

이 dated report와 [`portable_beta_manifest_2026-07-13.json`](../../eval/portable_beta_manifest_2026-07-13.json)은
역사 자료이므로 현재 scorer로 덮어쓰지 않는다. 현재 release-regression 재실행 명령은 다음과 같으며 품질
blocker가 유지되는 동안 정상 종료 코드는 `1`이다. 구조 오류 `2`는 허용하지 않는다.

```bash
mkdir -p .release
python3 eval_portable_beta.py \
  eval/portable_beta_manifest.json \
  --output .release/PORTABLE_BETA_REPORT.json
```

## 다음 tranche

1. 현재 동결 suite/scorer/기준은 수정하지 않는다.
2. corpus를 보지 않은 새 작성 단계에서 공개 skill description 기반의 한영 metadata를 만든다.
3. 새 manifest와 새 holdout revision으로 별도 평가를 수행한다.
4. 새 beta gate가 통과해도 결과는 `eligible-for-opt-in-beta-review`일 뿐 자동 출시나 훅 활성화 권한이 없다.
5. tag release workflow는 portable gate가 통과하지 않으면 package publish 전에 실패해야 한다.
