# policy-did — R&D 정책평가 인과추론 작업대

정책이 도입된 시점과 단위, 결과 변수, 비교군, 예상 효과 방향을 **설계서(YAML)에 먼저 적고**, 식별 가정 **게이트를 통과한 설계에만** 추정이 돕니다. 추정기는 새로 만들지 않고 R(`did`·`fixest`·`synthdid`·`rdrobust`)과 Python(`statsmodels`·`differences`)을 부릅니다. 출력은 추정표, 사건연구 그래프(SVG), 그리고 게이트 결과를 그대로 옮긴 **「말할 수 있는 것 / 없는 것」** 절입니다.

CLI는 표준 라이브러리만 씁니다. 추정기는 선택 의존성이며 없으면 설치 안내만 냅니다.

```bash
python3 policy_did.py init --out design.yaml          # 설계서 서식 — 자료를 보기 전에 가설·방향을 적는다
python3 policy_did.py check design.yaml --data panel.csv --out out      # 식별 가정 게이트 → out/gate.json
python3 policy_did.py estimate design.yaml --data panel.csv --out out   # 게이트 통과분만 → out/estimate.json
python3 policy_did.py report design.yaml --data panel.csv --out out     # out/report.md + event_study.svg
python3 policy_did.py run design.yaml --data panel.csv --out out        # 위 셋을 한 번에
```

## 왜 설계서와 게이트인가

GitHub의 정책평가 DiD 저장소는 거의 전부 과제물 수준이고(2026-10 검색), "설계서 → 가정 점검 → 추정"을 강제하는 도구가 없습니다. 실무에서 흔한 실패는 추정 코드가 아니라 그 앞에서 납니다. 처치 시점이 단위마다 다른데 이원고정효과(TWFE)를 돌리고, 비교군이 없는데 "효과"를 말하고, 자료를 본 뒤 가설을 고치고, 결과 변수가 자료에 없다는 것을 보고서를 쓰다 알아차립니다. 이 도구는 그 순서를 코드로 묶습니다.

- 설계서가 없으면 아무것도 돌지 않는다
- 게이트(`gate.json`)가 없거나, 게이트 뒤에 설계서·자료가 바뀌었거나(해시 대조), 판정이 FAIL이면 `estimate`가 멈춘다
- 보고서는 설계서에 등록한 가설·결과 변수·horizon에 대해서만 판정하고, 게이트의 WARN 문장을 **그대로** '말할 수 없는 것'에 옮긴다

## 설계서

`init`이 쓰는 서식의 핵심 항목:

| 절 | 내용 |
|---|---|
| `data` | 자료 파일, 단위·시점·결과변수 열, 출처, log 변환, 클러스터 |
| `treatment.timing` | **단위별 최초 처치 시점표**. 여기 없는 단위는 미처치. `always_treated`(표본 전 처치)·`exclude_units`는 뺀다 |
| `treatment.comparison` | `never_treated` / `not_yet_treated` |
| `hypotheses` | 사전 등록 가설. `direction`(positive/negative/zero)·`horizon`이 없으면 설계서 오류 |
| `assumptions` | 평행추세 최소 사전 기간, SUTVA 파급 위험과 **근거(비어 있으면 FAIL)**, 동시 정책, 기대 효과 |
| `design.method` | `cs`(Callaway–Sant'Anna) · `sa`(Sun–Abraham) · `twfe` · `synthdid` · `rdd` |

## 게이트 항목

| ID | 점검 | FAIL | WARN |
|---|---|---|---|
| G1 | 가설 방향 사전 등록, 등록일, 결과변수 일치 | 방향 없음·결과변수 불일치 | 등록일 없음 |
| G2 | 결과변수가 자료에 있는가, 결측, 패널 균형, 시점표↔자료 단위 일치, 출처의 **시계열 단절 각주** | 결측 >50%, 시점표에만 있는 단위 | 결측 >10%, 단절 각주 |
| G3 | 처치 구조: 코호트 수, 미처치 단위, staggered 여부 | 비교군 없음, **staggered + TWFE**, synthdid에 코호트 여럿·donor 없음 | 미처치 없는 마지막 코호트 |
| G4 | 클러스터 수, 코호트당 단위 수, 코호트별 사전·사후 기간 | 클러스터 <5, 사전 기간 <2 | 클러스터 <20, 단위 1개 코호트, horizon 초과 |
| G5 | 사전 추세: 코호트마다 같은 창에서 처치군·비교군 단위별 기울기 차(Welch) → 역분산 합산 | — | p<0.10 |
| G6 | SUTVA 근거, 동시 정책 | 근거 비어 있음 | 파급 위험 medium/high, 동시 정책 미기재 |
| G8 | 추정기 가용성 | 설치된 것 없음 | — |
| G9 | (RDD) 배정 변수·절단점 범위, 양쪽 관측 수, 밀도 간이 점검 | 절단점 범위 밖 | 한쪽 <30, 밀도 비대칭 |

사전 추세 검정은 **통과해도 평행추세의 증명이 아닙니다**(Roth 2022). 보고서는 PASS여도 그 문장을 남깁니다.

## 추정기

| method | 호출 | 조건 |
|---|---|---|
| `cs` | R `did::att_gt` + `aggte`(dynamic·simple·group), 없으면 Python `differences.ATTgt` | staggered 가능 |
| `sa` | R `fixest::feols(sunab())` | staggered 가능 |
| `twfe` | Python `statsmodels` OLS(단위·시점 더미, 클러스터 SE) | **단일 도입 시점만**. staggered면 게이트 FAIL |
| `synthdid` | R `synthdid` (donor가 처치보다 적으면 jackknife SE) | 단일 도입 시점·균형 패널·미처치 donor |
| `rdd` | R `rdrobust` | `design.rdd.running`·`cutoff` |

`python3 policy_did.py engines`가 설치 상태와 설치 명령을 보여 줍니다. R 사용자 라이브러리가 쓰기 불가일 때의 우회도 거기 있습니다. 합성 자료 검증에서 R `did`와 Python `differences`는 같은 값을 냈습니다(ATT 3.75, 동적 추정치 소수 둘째 자리까지 일치).

## kr-sti-data 연결

[kr-sti-data](https://github.com/parkjui92/kr-sti-data)의 KOSIS export(긴 형식)를 단위×연도 패널로 바꿉니다. export 머리의 `# 시계열 단절` 각주는 패널에 그대로 옮겨지고 게이트가 WARN으로 올립니다.

```bash
python3 sti.py export --source kosis --tbl DT_127003_002 --out dept.csv   # kr-sti-data
python3 policy_did.py panel dept.csv --item 금액 --name exec --drop 합계 다부처 --out panel.csv
```

## 예시

- `examples/synthetic/` — 효과를 아는 합성 staggered 패널(참값 2.0 + 0.5/년). `simulate`로 다시 만들 수 있고, `--trend-break`로 사전 추세가 다른 자료를 만들어 G5 WARN을 볼 수 있습니다.
- `examples/innotown_region/` — **실제 자료 시범**. 강소연구개발특구 지정(2019·2020·2022, 특구재단 원문 `sources.md`)을 시도 단위 정부 R&D 집행액(KOSIS DT_127003_009, 2011~2024, log)으로. 게이트 **WARN**(클러스터 16, 단위 1개 코호트 2022, 2024 기준 변경 각주, 파급 medium) → `r:did` 전체 ATT 0.030(SE 0.019), 사전 기간 −0.06~0.03, 사후 0~3년 평균 0.024. 보고서 판정은 "방향 일치(구간이 0을 포함) · 클러스터 수 부족으로 유의성 언급 보류". Python `differences`도 같은 값(0.0299). 시도 집행액 수천~수만억 원에 특구당 국비 수십억 원이라 효과가 검출되지 않을 것을 설계서에 미리 적었고, 결과도 그렇다.
- `examples/kosis_budget_cut_2024/` — 2024년 정부 R&D 예산 삭감을 부처별 집행액(KOSIS DT_127003_002)으로 보려는 설계. **게이트가 FAIL로 막습니다**: 모든 부처가 같은 해에 처치되어 비교군이 없고, 같은 해에 예산 기준 변경(약 1.8조원 재분류)이 겹칩니다. 집계 자료로 "삭감의 효과"를 말할 수 없다는 것을 도구가 먼저 말하게 한 예시입니다.

## 정직성 규칙 (출력에 그대로 찍힙니다)

1. **가정 미충족이면 추정치가 없다.** FAIL 상태에서 `estimate`는 종료 코드 3으로 멈추고 `estimate.json`을 만들지 않는다.
2. **등록한 가설에 대해서만 말한다.** 보고서 2절은 설계서의 가설·방향·horizon별 판정이며, 다른 결과변수·기간은 없다.
3. **WARN은 지우지 않고 옮긴다.** 게이트의 WARN 문장이 보고서 4절에 ID와 함께 그대로 들어간다.
4. **사전 추세 검정 통과 ≠ 평행추세.** PASS여도 그 문장을 남긴다.
5. **클러스터가 적으면 유의성 언어를 쓰지 않는다.** G4 WARN이면 가설 판정에 '유의성 언급 보류'가 붙는다.
6. **단위 수준을 넘어 해석하지 않는다.** 부처·지역 집계 자료의 효과를 과제·연구자 효과로 옮기지 않는다는 문장이 3절에 고정으로 들어간다.
7. **해시로 묶는다.** 설계서·자료의 sha가 gate.json·estimate.json·report.md에 들어가고, 하나라도 바뀌면 처음부터 다시 돈다.
8. **추정기를 지어내지 않는다.** 패키지가 없으면 설치 안내만 내고 멈춘다.

## 검증 상태 (2026-10-20)

- 합성 staggered 패널(60단위·15년·코호트 3): 게이트 PASS → `r:did`·`py:differences` 동적 추정치 e=0 2.05, e=1 2.56, e=2 3.10, e=3 3.52, e=4 4.07 (참값 2.0·2.5·3.0·3.5·4.0), 사전 기간 −0.12~−0.01
- `sa`(fixest sunab) 전체 ATT 4.30 vs CS 4.29(같은 자료) — 일치
- 단일 코호트 합성: `twfe` 3.52, `synthdid` 3.54 (참값 평균 3.5)
- RDD 합성(참값 5): `rdrobust` 3.25 (SE 1.06, 강건 CI [0.37, 5.51])
- 게이트 거부 경로: staggered+TWFE, 비교군 없음, SUTVA 근거 없음, 시점표에만 있는 단위, 설계서 변경 후 추정 — 전부 차단 확인
- tests 18건 통과(`python3 policy_did.py selftest`)
- 실제 자료 1건 완주(`examples/innotown_region/`): 게이트 WARN 5건을 보고서에 그대로 옮기고 유의성 언어 없이 보고. 처치 시점은 특구재단 원문으로 확인, 미확정 단위(강원)는 제외하고 사유를 남김

## 한계

- 공변량(covariates)은 설계서에 자리만 있고 추정 호출에 아직 넘기지 않는다
- 연속 처치(처치 강도 차이)·삼중차분·사건연구 Bacon 분해는 범위 밖
- 사전 추세 검정은 단위별 선형 기울기 비교의 간이판이다. 정식 검정은 R `pretrends`·`HonestDiD`
- RDD 밀도 점검은 McCrary·`rddensity`의 대용이다
- 작은 YAML 파서는 설계서 서식의 부분집합만 읽는다(앵커·다중행 블록 없음). PyYAML이 있으면 그것을 쓴다

## 라이선스

MIT. 추정 패키지는 각자의 라이선스를 따릅니다.
