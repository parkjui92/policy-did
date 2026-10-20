# policy-did 설계서 (2026-10-20)

## 한 문장

정책 도입 시점표·처치 단위·결과 변수·비교군·예상 방향을 설계서(YAML)로 먼저 받고, 식별 가정 게이트를 통과한 설계에만 외부 추정기(R did/fixest/synthdid/rdrobust, Python statsmodels/differences)를 돌려, 추정표와 **「말할 수 있는 것/없는 것」**을 내는 명령줄 도구.

## 왜 필요한가

- GitHub의 정책평가 DiD 저장소는 과제물 수준(0~3★)이고 "설계서·가정 게이트"를 가진 도구가 없다(2026-10 검색)
- 정부 R&D 평가 실무에서 반복되는 실패는 추정 코드가 아니라 순서에서 난다: staggered 처치에 TWFE, 비교군 없는 "효과", 자료를 본 뒤 가설 수정, 자료에 없는 결과변수
- scientometric-review의 5축(식별전략·추론 언어)을 사람이 아니라 코드가 차단하게 한다

## 흐름

```
init ──▶ design.yaml ──▶ check ──▶ gate.json(PASS/WARN/FAIL, sha) ──▶ estimate ──▶ estimate.json ──▶ report ──▶ report.md + event_study.svg
                              │                                  ▲
                              └── FAIL ──────── 멈춤 ─────────────┘  (설계서·자료 해시가 다르면 다시 check)
```

## 설계 결정

| 결정 | 이유 |
|---|---|
| 설계서가 코드보다 먼저, `direction` 없으면 설계서 오류 | 자료를 본 뒤 가설을 고치는 것을 구조로 막는다. `registered_at`이 없으면 WARN |
| 게이트 파일과 해시 | "게이트를 돌렸다"가 말이 아니라 파일이어야 한다. 설계서·자료가 바뀌면 자동 무효 |
| FAIL은 멈춤, WARN은 옮김 | 가정 위반을 각주로 숨기지 않는다. WARN 문장은 보고서 4절에 ID와 함께 그대로 간다 |
| 추정기 재구현 금지 | 검증된 패키지(did 2.1.2, fixest 0.12, synthdid 0.0.9, rdrobust 2.2, differences 0.3)를 부른다. 없으면 설치 안내만 |
| staggered + TWFE = FAIL | Goodman-Bacon(2021) 분해의 음의 가중. 사용자가 `--force`로 넘길 수 없다 — method를 바꿔야 한다 |
| 사전 추세는 코호트마다 같은 창에서 | 창이 다르면 공통 충격이 "차이"로 둔갑한다(첫 구현에서 합성 자료가 p=0.07로 걸려 발견). 코호트별 Welch → 역분산 합산 |
| 사전 추세 PASS에도 Roth(2022) 문장 | 통과는 증명이 아니다. 검정력이 낮은 작은 패널일수록 그렇다 |
| 작은 YAML 파서 | 의존성 0을 유지. PyYAML이 있으면 그것을 쓰고, 서식은 두 파서가 같은 결과를 내는지 테스트로 고정 |
| SVG 직접 생성 | matplotlib 없이 사건연구 그래프. CI 막대·0선·처치 시점선 |
| kr-sti-data 각주 승계 | export 머리의 `# 시계열 단절` 줄을 패널로 옮기고 게이트 WARN으로 올린다 |

## 게이트가 보는 것

G1 사전 등록 · G2 자료(결과변수 존재·결측·균형·시점표 대조·단절 각주) · G3 처치 구조(비교군·staggered·방법 적합) · G4 표본(클러스터·코호트 크기·사전/사후 기간) · G5 사전 추세 · G6 SUTVA·동시 정책 · G7 기대 효과 · G8 추정기 · G9 RDD(절단점·밀도)

## 합성 자료로 확인한 것

- 60단위·15년·코호트 2015/2018/2021·미처치 24, 효과 2.0 + 0.5/년, 잡음 SD 0.5: CS(R did = Python differences) e=0 2.05 … e=4 4.07; 사전 −0.12~−0.01. SA 전체 ATT 4.30 vs CS 4.29
- `--trend-break`(처치군 사전 기울기 +0.4): G5 WARN(z=12). not_yet_treated 비교에서 첫 코호트는 뒤 코호트가 비교군에 섞여 차이가 작게 나온다 — 합산 검정이 이를 보완
- 단일 코호트: twfe 3.52·synthdid 3.54 (참값 평균 3.5). synthdid는 donor ≤ 처치면 placebo SE 불가 → jackknife로 자동 전환, 게이트 WARN
- RDD(참값 5): rdrobust 3.25 (SE 1.06). 잡음 SD 2·n=600에서 구간이 참값을 덮는다

## 실제 자료에서 드러난 것

KOSIS 부처별 집행(DT_127003_002)으로 2024년 예산 삭감을 보려는 설계는 게이트 FAIL: 모든 부처가 같은 해 처치·미처치 없음(G3), 같은 해 기준 변경 각주(G2g), 클러스터 19(G4), 부처 간 사업 이관 파급(G6). 집계 자료로 "삭감 효과"를 말할 수 없다는 것을 도구가 먼저 말했다.

## 의존

CLI: 표준 라이브러리. 추정: R ≥ 4.3(`did`·`fixest`·`synthdid`·`rdrobust`, `jsonlite`), Python(`pandas`·`statsmodels`·`differences`). 어느 쪽도 없으면 `engines`가 설치 명령을 낸다.

## 하지 않는 것

- 연속 처치(dose-response)·삼중차분·Bacon 분해·HonestDiD 민감도
- 공변량 전달(설계서 자리만)
- 그림의 PNG 변환(SVG만)
