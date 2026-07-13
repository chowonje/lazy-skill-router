# Router A/B bilingual metadata pilot — 2026-07-13

## 결론

명시적 host-catalog sync에서 검토된 `aliases`와 `capabilities`를 inventory/index revision에 포함시키는 경로는
유효했다. 동일한 교정 corpus에서 B의 전체 Top-1은 control `134/240`에서 pilot `168/240`으로, 한국어는
`12/46`에서 `41/46`으로 올랐다.

그러나 **behavior promotion은 계속 차단**한다. pilot metadata는 이 corpus를 참고한 calibration artifact이고,
no-relevant lexical no-match는 `6/20`에서 개선되지 않았다. B는 최종 ownership이나 semantic abstention을 수행한
시스템이 아니라 Top-K 후보 검색기다.

## 실험 경계

- A: legacy route + ActivationIR
- B control: 기존 156-entry English-only capability index
- B pilot: 19개 skill에 한국어 alias 20개와 capability phrase 16개를 explicit sync로 추가한 index
- corpus: 240개 synthetic case, positive owner case 208개, expected-abstain case 32개
- metadata provenance: `corpus-informed-calibration`, promotion 불가
- runtime 번역, LLM 호출, prompt-to-skill route 추가, score threshold 변경: 없음
- label correction: live configured name과 달랐던 5개 shorthand를 current inventory identity로 교정

`retrieval no-match`는 lexical candidate가 없다는 뜻일 뿐이다. A의 ActivationIR abstain과 B의 no-match를 같은
최종 행동으로 해석하지 않도록 candidate-only 지표를 별도로 기록했다.

## 동결 revision

| 입력 | revision |
|---|---|
| config | `sha256:61ce736541d8dd03642d634f618f56f4c6106d837963a6ca1b6fbca8e5d7b939` |
| control inventory | `sha256:18288ad816bfd373d0bb7986f4efe3ecd06f2242ba21ccf073493bf697f1140b` |
| control index | `sha256:51aadd022302fa49d8f86ded79d27997e8130fe22d90bc80f72497c42f4fbe9f` |
| pilot host catalog | `sha256:773f85d9038cde126d425cfb18d26a4d17757df82d8ec29adf34ba0043950641` |
| pilot inventory | `sha256:70e2d9b6f579b1f741252e151e8aa95697c08b20385d33f717cd83104b7a96b5` |
| pilot index | `sha256:971627493bdf7cd20d5fe9853a085f692d718a6e5a7c4d397b4320e665bbdfd1` |
| experiment code | `sha256:b2f8117eb7511e6ced62d544bd75af4dec931ad570b92322cb305ce46edfc2bf` |
| control manifest | `sha256:70e2ea29f6e065e37f998583c100a426a197b64df3665ded65cf591208b63c76` |
| pilot manifest | `sha256:3ba4f3ff650353262266fbe5b895c02e9127fca4a5498c186528941bd6f8f422` |
| control report | `sha256:632c41591fde3a4cca6d70e7800e2e4c5278cc9847572f7548f621715019ce2b` |
| control benchmark run | `sha256:524e202121477d43e69dc900f7ce86850cb3c0e20d0b7a56156a579294b31854` |
| pilot report | `sha256:8c5686c32494bada1e7ed6e2318118df4944294e45a5501650868d329c32fb03` |
| pilot benchmark run | `sha256:2951a72cfc8e38d6e81882932407ea0d4732ee2eae991075164f372ae7c1ef98` |
| promotion policy | `sha256:94e3aceed5e77dcc5f38698c76768540a49dc099e7fa202900febc763dee11bb` |
| control promotion gate | `sha256:a39b885eda5f6402ab94fcb7f5bf923776809f4607f68cbbb2c944e44a3a41f4` |
| control promotion gate run | `sha256:fcd351e0da74a2a55da60fcaa4df3eeb1b8a3ed821903d86471bd6aca1495bf5` |
| pilot promotion gate | `sha256:3bdf83ebacfe414f44df5088d05fae18534edd98229f6663c44a5497b93ad9f0` |
| pilot promotion gate run | `sha256:959cfa5fcd25bc59aede8a17e75c56705ee9ba7b0ffadd4595d87f7226f8fe77` |
| retrieval | `lexical-bm25-char3/v1`, K=3 |

`reportRevision`과 `gateRevision`은 latency와 실행 환경을 제외한 재생 안정 decision revision이다.
`runRevision`은 latency를 포함한 전체 단일 실행 증거다. Control과 pilot을 각각 두 번 재생했을 때
report/gate revision은 같았고 run revision은 달랐다. 위 표의 run 값은 저장된 report 실행을 가리킨다.
Privacy 검사는 1자 이상의 모든 prompt를 report 문자열과 exact/substring으로 대조한다.

Checked-in `eval/router_ab_manifest.json` is the corrected control manifest. The revision-bound
`eval/router_ab_manifest_bilingual_pilot.overlay.json` changes the frozen inventory/index revisions and
`evidence.metadataProvenance` after regenerating the pilot catalog. Materialize it with:

```bash
python3 materialize_router_ab_manifest.py \
  eval/router_ab_manifest.json \
  eval/router_ab_manifest_bilingual_pilot.overlay.json \
  --output /tmp/router-ab-bilingual-pilot.json
```

The command rejects a stale base revision and prints the resulting pilot manifest revision. This keeps one canonical
240-case corpus without hiding the provenance change. The frozen pilot config/inventory/index/manifest and calibration
provenance are now preserved in the private CAS ref `bilingual-pilot-2026-07-13-v2`; its descriptor revision is
`sha256:bb151ce6673e73244d5aff869687992a3a0d8cd5da5976f36422098e69a4d859`. The overlay does not reconstruct those bytes
by itself, and the CAS proves replay identity rather than promotion quality.

## 결과

| 지표 | A | B control | B bilingual pilot |
|---|---:|---:|---:|
| 전체 Top-1, abstain 포함 | 100/240 | 134/240 | 168/240 |
| candidate Top-1, positive 208개 | 76/208 | 127/208 | 161/208 |
| Recall@3, positive 208개 | 36.78% | 73.08% | 85.10% |
| 한국어 전체 | 20/46 | 12/46 | 41/46 |
| 한국어 candidate Top-1 | 12/37 | 5/37 | 34/37 |
| 한국어 Recall@3 | 32.43% | 24.32% | 91.89% |
| no-relevant lexical no-match | 19/20 | 6/20 | 6/20 |
| expected-abstain no-match recall | 75.00% | 21.88% | 21.88% |
| labelled conflict Top-1 | 78 | 32 | 28 |
| labelled conflict Top-K hits | 78 | 113 | 123 |
| inventory-ineligible candidate hits | 0 | 0 | 0 |
| p95 latency, 단일 실행 | 9.1386ms | 14.7679ms | 16.0563ms |

Pilot B와 A의 positive-case paired comparison은 rescue `97`, harm `12`, net `+85/208`이었다. 이 숫자는
metadata를 같은 corpus에 맞춰 작성했으므로 독립 효과 추정치가 아니다. Top-K conflict 증가는 후보를 더 많이
회수한 결과이며, runtime-ineligible hit는 세 variant 모두 0이다.

## 판단

1. **host-catalog metadata contract: shadow merge 검토 가능.** Optional metadata는 제한, validation, revision,
   inventory reconciliation, index revision을 모두 통과한다.
2. **pilot metadata: active catalog 반영 금지.** Gold를 본 calibration artifact이므로 제품 metadata가 아니다.
3. **retrieval threshold: 보류.** Description score는 positive와 abstain 분포가 크게 겹친다. 절대 점수 threshold는
   index 크기와 prompt 길이에 종속되고 같은 corpus에 과적합된다.
4. **behavior promotion: 실패.** Recall@3가 아직 95% gate 미만이고, semantic no-skill/ownership 실험과 독립
   holdout이 없다.

Expected-abstain lexical no-match recall도 A `75.00%`에서 pilot B `21.88%`로 하락해 절대 `95%`와 legacy
비퇴행 기준을 모두 실패한다. 이 값은 semantic abstention을 증명하지 않고 no-skill 퇴행을 보수적으로 막는다.

생성된 pilot `PromotionGateV1`도 `blocked`이며 `authority: none`, `autoPromote: false`다. 개인정보 검사,
positive candidate-only CI, inventory-ineligible hit `0`, operational failure `0`, p95 `20ms` 이하는 통과했다.
Privacy leak scan은 최소 비교 길이를 `1`자로 보수화했으며 raw prompt 누출 없이 통과했다. Case ID는 재현과
추적을 위해 report에 의도적으로 유지된다.
차단 사유는 다음과 같다.

- `evidence_artifact_verifier_unavailable`
- `metadata_corpus_informed`
- `independent_holdout_missing`
- `independent_adjudication_missing`
- `ownership_observation_missing`
- `activation_observation_missing`
- `outcome_observation_missing`
- `candidate_recall_below_minimum`
- `expected_abstain_no_match_recall_below_minimum`
- `expected_abstain_no_match_regressed`

Manifest나 evidence에 `sha256:` 모양의 revision이 있다는 사실만으로는 독립 검증이 성립하지 않는다. 이는
self-attestation일 뿐이다. Evaluator는 이제 명시적 `--artifact-root` 아래 실제 bytes를 검증할 수 있지만, 이
실행에는 독립 artifact가 제공되지 않아 verification은 `unavailable`로 남는다. Byte identity만으로 독립성이나
라벨 품질이 증명되지는 않는다. 수집 절차는
[`promotion-evidence-protocol.md`](promotion-evidence-protocol.md)를 따른다.

## 다음 실험

1. 새 hook contract가 배포된 이후의 prospective shadow 요청만 수집한다. 기존 240-case prompt나 결과를
   objective signal 생성에 사용하지 않는다.
2. Exact configured-name reference가 있는 고유 요청 100건까지 수집하고 Recall@3 `0.95`, Top-1 `0.90`,
   degraded/invalid `0`, p95 `20ms` 중단선을 적용한다.
3. `AutomatedShadowEvidenceV1`을 재생해 collection readiness만 판단한다. No-skill·ownership·outcome 증거로
   확대 해석하지 않는다.
4. 사람 작성 holdout을 제외하는 동안 behavior promotion은 시도하지 않고 `PromotionGateV1`을 `blocked`로
   유지한다.

## 근거

- [`eval/capability_metadata_ko_pilot.json`](../../eval/capability_metadata_ko_pilot.json)
- [`eval/router_ab_manifest.json`](../../eval/router_ab_manifest.json)
- [`eval/router_ab_manifest_bilingual_pilot.overlay.json`](../../eval/router_ab_manifest_bilingual_pilot.overlay.json)
- [`eval/results/router_ab_2026-07-13_corrected-control.json`](../../eval/results/router_ab_2026-07-13_corrected-control.json)
- [`eval/results/router_ab_2026-07-13_bilingual-pilot.json`](../../eval/results/router_ab_2026-07-13_bilingual-pilot.json)
- [`automated-shadow-evidence.md`](automated-shadow-evidence.md)
