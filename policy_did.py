#!/usr/bin/env python3
"""policy-did — R&D 정책평가 인과추론 작업대.

설계서(YAML)가 먼저, 식별 가정 게이트를 통과한 뒤에만 추정이 돈다. 추정기는 재구현하지 않고
R(did·fixest·synthdid·rdrobust)이나 Python(statsmodels·linearmodels·differences)을 부른다.

명령:
  init       설계서 서식을 쓴다
  simulate   효과를 알고 있는 합성 패널(staggered)을 만든다 — 도구 자체 점검·연습용
  panel      kr-sti-data(KOSIS) 긴 형식 export를 단위×연도 패널로 바꾼다
  check      설계서 + 자료 → 식별 가정 게이트(gate.json). 추정 전 필수
  estimate   게이트 통과분만 추정(estimate.json). 가정 위반(FAIL)이면 돌지 않는다
  report     추정표 + '말할 수 있는 것/없는 것' + 사건연구 그래프(SVG)
  run        check → estimate → report 한 번에
  engines    이 컴퓨터에서 쓸 수 있는 추정기 목록과 설치 안내
  selftest   합성 자료로 게이트·추정·보고 전 경로 자체 점검

설계: docs/design.md
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

__version__ = "0.1.1"

METHODS = {
    "cs": "Callaway–Sant'Anna (R did::att_gt / Python differences)",
    "sa": "Sun–Abraham (R fixest::sunab)",
    "twfe": "이원고정효과 OLS (Python statsmodels) — staggered 처치에는 금지",
    "synthdid": "합성 이중차분 (R synthdid) — 단일 도입 시점·균형 패널",
    "rdd": "회귀불연속 (R rdrobust)",
}
DIRECTIONS = {"positive", "negative", "zero"}
COMPARISONS = {"never_treated", "not_yet_treated"}
NEVER = 0  # 미처치 단위의 처치 시점 부호


# ---------------------------------------------------------------------------
# 0. 작은 YAML 읽기 (표준 라이브러리만; PyYAML이 있으면 그것을 쓴다)
# ---------------------------------------------------------------------------

def load_yaml(text: str):
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text)
    except ImportError:
        return _MiniYaml(text).parse()


class _MiniYaml:
    """설계서에 필요한 부분집합: 매핑·목록·스칼라·따옴표·주석·인라인 [a, b]. 앵커·다중행 블록은 없음."""

    def __init__(self, text: str):
        self.lines = []
        for raw in text.splitlines():
            s = _strip_comment(raw).rstrip()
            if s.strip() == "" or s.strip() == "---":
                continue
            indent = len(s) - len(s.lstrip(" "))
            self.lines.append((indent, s.strip()))
        self.i = 0

    def parse(self):
        if not self.lines:
            return {}
        return self._block(self.lines[0][0])

    def _block(self, indent):
        if self.i >= len(self.lines):
            return None
        if self.lines[self.i][1].startswith("- "):
            return self._list(indent)
        return self._map(indent)

    def _map(self, indent):
        out = {}
        while self.i < len(self.lines):
            ind, s = self.lines[self.i]
            if ind < indent:
                break
            if ind > indent:
                raise ValueError(f"YAML 들여쓰기 오류: {s!r}")
            if s.startswith("- "):
                break
            key, val = _split_kv(s)
            self.i += 1
            if val == "":
                if self.i < len(self.lines) and self.lines[self.i][0] > indent:
                    out[key] = self._block(self.lines[self.i][0])
                elif self.i < len(self.lines) and self.lines[self.i][0] == indent and self.lines[self.i][1].startswith("- "):
                    out[key] = self._list(indent)
                else:
                    out[key] = None
            else:
                out[key] = _scalar(val)
        return out

    def _list(self, indent):
        out = []
        while self.i < len(self.lines):
            ind, s = self.lines[self.i]
            if ind < indent or not s.startswith("- "):
                break
            if ind > indent:
                raise ValueError(f"YAML 들여쓰기 오류: {s!r}")
            body = s[2:].strip()
            self.i += 1
            if body == "":
                out.append(self._block(self.lines[self.i][0]) if self.i < len(self.lines) else None)
            elif _looks_kv(body):
                # "- key: val" 로 시작하는 매핑. 이어지는 줄은 indent+2 로 들어온다
                sub_indent = indent + 2
                self.lines.insert(self.i, (sub_indent, body))
                out.append(self._map(sub_indent))
            else:
                out.append(_scalar(body))
        return out


def _strip_comment(s: str) -> str:
    inq = None
    for k, ch in enumerate(s):
        if inq:
            if ch == inq:
                inq = None
        elif ch in "\"'":
            inq = ch
        elif ch == "#" and (k == 0 or s[k - 1] in " \t"):
            return s[:k]
    return s


def _looks_kv(s: str) -> bool:
    if s.startswith(("[", "{", "\"", "'")):
        return False
    m = re.match(r"^[^:]+?:(\s|$)", s)
    return bool(m)


def _split_kv(s: str):
    m = re.match(r"^(.+?):(?:\s+(.*))?$", s)
    if not m:
        raise ValueError(f"YAML 'key: value' 형식이 아님: {s!r}")
    key = m.group(1).strip().strip("\"'")
    return key, (m.group(2) or "").strip()


def _scalar(v: str):
    if v == "":
        return None
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        return [] if inner == "" else [_scalar(x.strip()) for x in _split_inline(inner)]
    if v.startswith("{") and v.endswith("}"):
        inner = v[1:-1].strip()
        out = {}
        for part in _split_inline(inner):
            if part.strip():
                k, val = _split_kv(part.strip())
                out[k] = _scalar(val)
        return out
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        return v[1:-1]
    low = v.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "~", "none"):
        return None
    if re.fullmatch(r"[-+]?\d+", v):
        return int(v)
    if re.fullmatch(r"[-+]?(\d+\.\d*|\.\d+|\d+)([eE][-+]?\d+)?", v):
        return float(v)
    return v


def _split_inline(s: str):
    out, cur, depth, inq = [], "", 0, None
    for ch in s:
        if inq:
            cur += ch
            if ch == inq:
                inq = None
        elif ch in "\"'":
            inq = ch
            cur += ch
        elif ch in "[{":
            depth += 1
            cur += ch
        elif ch in "]}":
            depth -= 1
            cur += ch
        elif ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    return out


# ---------------------------------------------------------------------------
# 1. 설계서
# ---------------------------------------------------------------------------

DESIGN_TEMPLATE = """# policy-did 설계서 — 코드보다 먼저 쓴다. 여기 적지 않은 가설·결과변수는 보고서에 나오지 않는다.
name: 예시_제도도입_효과
question: "제도 X 도입이 단위별 결과 Y를 바꿨는가"
registered_at: {today}          # 사전 등록일. 자료를 보기 전에 적는다

data:
  file: panel.csv               # 단위×시점 긴 형식. --data 로 덮어쓸 수 있다
  unit: unit                    # 처치 단위 열(부처·지역·기관·사업)
  time: year                    # 시점 열(정수)
  outcome: y                    # 결과 변수 열 — 자료에 실제로 있어야 한다
  outcome_source: "출처(표 ID·조회일)를 적는다"
  log_outcome: false            # true면 log(y) 를 쓴다(0·음수 있으면 게이트가 막는다)
  cluster: unit                 # 표준오차 클러스터(기본 단위)
  covariates: []                # 시간불변 공변량만 CS에서 허용

treatment:
  name: "제도 X"
  timing:                       # 단위별 최초 처치 시점. 여기 없는 단위는 미처치로 본다
    A: 2019
    B: 2019
    C: 2021
  always_treated: []            # 표본 시작 전부터 처치된 단위 — 비교에서 뺀다
  exclude_units: []             # 그 밖에 빼는 단위(사유를 notes에)
  comparison: not_yet_treated   # never_treated | not_yet_treated
  anticipation: 0               # 처치 전 몇 기부터 반응이 시작될 수 있는가

hypotheses:                     # 사전 등록 가설 — 방향을 반드시 적는다
  - id: H1
    outcome: y
    direction: positive         # positive | negative | zero
    horizon: [0, 3]             # 처치 후 몇 기까지 말할 것인가
    statement: "도입 후 3년 내 Y가 증가한다"

assumptions:
  parallel_trends:
    pre_periods_min: 3          # 코호트마다 최소 사전 기간
    note: "왜 평행추세를 믿는가"
  sutva:
    spillover_risk: low         # low | medium | high
    justification: "처치 단위 간 자원 이동이 없는 이유"
  no_other_policy: "같은 시점에 겹친 다른 정책과 그 처리"
  anticipation_note: "발표~시행 간격"

design:
  method: cs                    # cs | sa | twfe | synthdid | rdd
  min_clusters: 20              # 이보다 적으면 경고(추론 신뢰도)
  event_window: [-4, 4]
  # rdd:
  #   running: score
  #   cutoff: 60
  #   bandwidth: null           # null이면 rdrobust 자동 선택

notes: ""
"""


@dataclass
class Hypothesis:
    id: str
    outcome: str
    direction: str
    horizon: list
    statement: str = ""


@dataclass
class Design:
    name: str
    question: str
    data: dict
    treatment: dict
    hypotheses: list
    assumptions: dict
    design: dict
    registered_at: str = ""
    notes: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def method(self) -> str:
        return str(self.design.get("method", "cs")).lower()

    @property
    def unit_col(self) -> str:
        return str(self.data["unit"])

    @property
    def time_col(self) -> str:
        return str(self.data["time"])

    @property
    def outcome_col(self) -> str:
        return str(self.data["outcome"])

    @property
    def timing(self) -> dict:
        t = self.treatment.get("timing") or {}
        return {str(k): v for k, v in t.items()}


def load_design(path: Path) -> tuple[Design, list[str]]:
    """설계서를 읽고 형식 오류 목록을 돌려준다(빈 목록이면 정상)."""
    raw = load_yaml(path.read_text(encoding="utf-8"))
    errors = []
    if not isinstance(raw, dict):
        return None, ["설계서 최상위가 매핑이 아님"]
    for key in ("name", "data", "treatment", "hypotheses", "assumptions", "design"):
        if key not in raw or raw[key] in (None, "", []):
            errors.append(f"설계서에 '{key}' 절이 없음")
    if errors:
        return None, errors
    for key in ("unit", "time", "outcome"):
        if not raw["data"].get(key):
            errors.append(f"data.{key} 가 비어 있음")
    hyps = []
    for k, h in enumerate(raw["hypotheses"] or []):
        if not isinstance(h, dict):
            errors.append(f"hypotheses[{k}] 가 매핑이 아님")
            continue
        hid = str(h.get("id") or f"H{k + 1}")
        d = str(h.get("direction") or "").lower()
        if d not in DIRECTIONS:
            errors.append(f"{hid}: direction 이 positive/negative/zero 중 하나가 아님 (사전 등록 없는 가설은 추정하지 않는다)")
        hz = h.get("horizon") or [0, 0]
        if not (isinstance(hz, list) and len(hz) == 2 and all(isinstance(x, int) for x in hz)):
            errors.append(f"{hid}: horizon 은 [시작, 끝] 정수 둘")
        hyps.append(Hypothesis(hid, str(h.get("outcome") or raw["data"].get("outcome")), d, hz, str(h.get("statement") or "")))
    if not hyps:
        errors.append("가설이 하나도 없음")
    m = str(raw["design"].get("method", "cs")).lower()
    if m not in METHODS:
        errors.append(f"design.method '{m}' 는 지원 목록({', '.join(METHODS)})에 없음")
    comp = str((raw["treatment"] or {}).get("comparison", "not_yet_treated")).lower()
    if comp not in COMPARISONS:
        errors.append(f"treatment.comparison '{comp}' 는 never_treated/not_yet_treated 중 하나")
    if m != "rdd" and not (raw["treatment"] or {}).get("timing"):
        errors.append("treatment.timing(단위별 처치 시점표)이 비어 있음")
    if m == "rdd":
        rd = raw["design"].get("rdd") or {}
        if not rd.get("running") or rd.get("cutoff") is None:
            errors.append("rdd 설계에는 design.rdd.running 과 cutoff 가 필요")
    d = Design(
        name=str(raw["name"]), question=str(raw.get("question") or ""), data=raw["data"], treatment=raw["treatment"] or {},
        hypotheses=hyps, assumptions=raw["assumptions"] or {}, design=raw["design"], registered_at=str(raw.get("registered_at") or ""),
        notes=str(raw.get("notes") or ""), raw=raw,
    )
    return d, errors


# ---------------------------------------------------------------------------
# 2. 자료
# ---------------------------------------------------------------------------

@dataclass
class Panel:
    rows: list            # dict 목록(원 열 + unit, time, y, g, rel)
    unit_col: str
    time_col: str
    outcome_col: str
    notes: list           # CSV 머리의 '# ' 줄(kr-sti-data 단절 각주 등)
    columns: list
    dropped: dict         # 사유별 제외 단위


def read_csv_with_notes(path: Path) -> tuple[list[dict], list[str], list[str]]:
    notes, body = [], []
    in_preamble = True
    with open(path, encoding="utf-8-sig", newline="") as f:
        for line in f:
            if in_preamble and line.startswith("#"):
                notes.append(line.lstrip("#").strip())
            else:
                if not line.strip() and in_preamble:
                    continue
                in_preamble = False
                body.append(line)
    reader = csv.DictReader(body)
    rows = [dict(r) for r in reader]
    return rows, notes, list(reader.fieldnames or [])


def _num(v):
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if s in ("", "-", "NA", "na", "NaN", "nan", "null", "None", "…", "X", "x"):
        return None
    try:
        value = float(s)
        return value if math.isfinite(value) else None
    except ValueError:
        return None


def build_panel(d: Design, data_path: Path) -> Panel:
    rows, notes, cols = read_csv_with_notes(data_path)
    for col in (d.unit_col, d.time_col, d.outcome_col):
        if col not in cols:
            raise SystemExit(f"자료에 열 '{col}' 이 없음. 있는 열: {', '.join(cols)}")
    timing = d.timing
    always = {str(x) for x in (d.treatment.get("always_treated") or [])}
    excl = {str(x) for x in (d.treatment.get("exclude_units") or [])}
    log_y = bool(d.data.get("log_outcome"))
    out, dropped = [], defaultdict(list)
    for row_number, r in enumerate(rows, 2):
        u = str(r[d.unit_col] or "").strip()
        if not u:
            raise SystemExit(f"자료 {row_number}행: 단위 식별자가 비어 있습니다")
        if u in always:
            dropped["always_treated"].append(u)
            continue
        if u in excl:
            dropped["exclude_units"].append(u)
            continue
        t = _num(r[d.time_col])
        if t is None or not t.is_integer():
            raise SystemExit(f"자료 {row_number}행: 시점은 유한한 정수여야 합니다 ({r[d.time_col]!r})")
        t = int(t)
        y = _num(r[d.outcome_col])
        if y is not None and log_y:
            y = math.log(y) if y > 0 else None
        g = timing.get(u, NEVER)
        g = int(g) if g not in (None, "", NEVER) else NEVER
        rec = dict(r)
        rec.update({"unit": u, "time": t, "y": y, "g": g, "rel": (t - g) if g != NEVER else None})
        out.append(rec)
    for k in dropped:
        dropped[k] = sorted(set(dropped[k]))
    return Panel(out, d.unit_col, d.time_col, d.outcome_col, notes, cols, dict(dropped))


# ---------------------------------------------------------------------------
# 3. 통계 보조 (작은 것만; 추정기는 여기서 만들지 않는다)
# ---------------------------------------------------------------------------

def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _norm_cdf(z: float) -> float:
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def _ols_slope(ts, ys):
    """단순회귀 기울기 (y ~ t). 2점 미만이면 None."""
    pts = [(t, y) for t, y in zip(ts, ys) if y is not None]
    if len(pts) < 2:
        return None
    mt = sum(t for t, _ in pts) / len(pts)
    my = sum(y for _, y in pts) / len(pts)
    sxx = sum((t - mt) ** 2 for t, _ in pts)
    if sxx == 0:
        return None
    return sum((t - mt) * (y - my) for t, y in pts) / sxx


def _welch(a, b):
    """두 표본 평균 차이의 Welch t·양측 p(정규 근사). 각 표본 2개 미만이면 None."""
    a = [x for x in a if x is not None]
    b = [x for x in b if x is not None]
    if len(a) < 2 or len(b) < 2:
        return None
    ma, mb = statistics.mean(a), statistics.mean(b)
    va, vb = statistics.variance(a), statistics.variance(b)
    se = math.sqrt(va / len(a) + vb / len(b))
    if se == 0:
        return {"diff": ma - mb, "t": None, "p": None, "n_a": len(a), "n_b": len(b)}
    t = (ma - mb) / se
    return {"diff": ma - mb, "t": t, "p": 2 * (1 - _norm_cdf(abs(t))), "n_a": len(a), "n_b": len(b)}


# ---------------------------------------------------------------------------
# 4. 식별 가정 게이트
# ---------------------------------------------------------------------------

@dataclass
class GateItem:
    id: str
    severity: str      # PASS | INFO | WARN | FAIL
    title: str
    detail: str
    say: str = ""      # 보고서 '말할 수 없는 것/주의'에 그대로 옮길 문장


def _cohorts(panel: Panel):
    by_unit = {}
    for r in panel.rows:
        by_unit[r["unit"]] = r["g"]
    coh = defaultdict(list)
    for u, g in by_unit.items():
        coh[g].append(u)
    return dict(coh), by_unit


def pre_trend_raw(panel: Panel, window: tuple[int, int], comparison: str) -> list[dict]:
    """상대시점별 (처치군 평균 − 비교군 평균), rel=-1 기준. 추정이 아니라 그림·점검용 원평균."""
    cohorts, _ = _cohorts(panel)
    treated_g = sorted(g for g in cohorts if g != NEVER)
    out = []
    for e in range(window[0], window[1] + 1):
        diffs = []
        for g in treated_g:
            t = g + e
            tr = [r["y"] for r in panel.rows if r["g"] == g and r["time"] == t]
            if comparison == "never_treated":
                cp = [r["y"] for r in panel.rows if r["g"] == NEVER and r["time"] == t]
            else:
                cp = [r["y"] for r in panel.rows if (r["g"] == NEVER or r["g"] > t) and r["time"] == t]
            base_tr = [r["y"] for r in panel.rows if r["g"] == g and r["time"] == g - 1]
            if comparison == "never_treated":
                base_cp = [r["y"] for r in panel.rows if r["g"] == NEVER and r["time"] == g - 1]
            else:
                base_cp = [r["y"] for r in panel.rows if (r["g"] == NEVER or r["g"] > g - 1) and r["time"] == g - 1]
            mt, mc, bt, bc = _mean(tr), _mean(cp), _mean(base_tr), _mean(base_cp)
            if None in (mt, mc, bt, bc):
                continue
            diffs.append(((mt - mc) - (bt - bc), len([x for x in tr if x is not None])))
        if diffs:
            wsum = sum(n for _, n in diffs)
            out.append({"rel": e, "diff": sum(v * n for v, n in diffs) / wsum, "n_treated": wsum, "cohorts": len(diffs)})
        else:
            out.append({"rel": e, "diff": None, "n_treated": 0, "cohorts": 0})
    return out


def run_gate(d: Design, panel: Panel) -> list[GateItem]:
    items: list[GateItem] = []
    cohorts, by_unit = _cohorts(panel)
    treated_g = sorted(g for g in cohorts if g != NEVER)
    never = cohorts.get(NEVER, [])
    times = sorted({r["time"] for r in panel.rows})
    units = sorted(by_unit)
    method = d.method
    comparison = str(d.treatment.get("comparison", "not_yet_treated")).lower()
    pa = d.assumptions.get("parallel_trends") or {}
    pre_min = int(pa.get("pre_periods_min", 3) or 3)
    min_clusters = int(d.design.get("min_clusters", 20) or 20)

    # G1 사전 등록
    missing_dir = [h.id for h in d.hypotheses if h.direction not in DIRECTIONS]
    if missing_dir:
        items.append(GateItem("G1", "FAIL", "가설 방향 사전 등록", f"방향 없는 가설: {missing_dir}", "방향을 등록하지 않은 가설은 추정하지 않는다"))
    else:
        items.append(GateItem("G1", "PASS", "가설 방향 사전 등록", f"가설 {len(d.hypotheses)}건, 방향 전부 등록" + (f", 등록일 {d.registered_at}" if d.registered_at else ", 등록일 없음")))
        if not d.registered_at:
            items.append(GateItem("G1b", "WARN", "사전 등록일", "registered_at 이 비어 있음", "가설이 자료를 본 뒤 적혔을 가능성을 배제할 수 없다(등록일 미기재)"))
    for h in d.hypotheses:
        if h.outcome != d.outcome_col:
            items.append(GateItem("G1c", "FAIL", "가설 결과변수", f"{h.id} 의 결과변수 '{h.outcome}' 가 data.outcome '{d.outcome_col}' 와 다름", "설계서에 등록한 결과변수에 대해서만 말한다"))

    # G2 자료
    n_rows = len(panel.rows)
    n_missing = sum(1 for r in panel.rows if r["y"] is None)
    if n_rows == 0:
        items.append(GateItem("G2", "FAIL", "결과변수 존재", "자료 행이 0", "결과변수가 자료에 없다"))
        return items
    miss_rate = n_missing / n_rows
    if miss_rate > 0.5:
        items.append(GateItem("G2", "FAIL", "결과변수 결측", f"결측 {n_missing}/{n_rows} ({miss_rate:.0%})", "결과변수 절반 이상이 비어 있어 추정 불가"))
    elif miss_rate > 0.1:
        items.append(GateItem("G2", "WARN", "결과변수 결측", f"결측 {n_missing}/{n_rows} ({miss_rate:.0%})", f"결과변수 결측 {miss_rate:.0%} — 결측이 처치와 상관이면 편향"))
    else:
        items.append(GateItem("G2", "PASS", "결과변수 존재·결측", f"'{d.outcome_col}' 결측 {n_missing}/{n_rows}" + (" (log 변환)" if d.data.get("log_outcome") else "")))
    if d.data.get("log_outcome"):
        nonpos = sum(1 for r in panel.rows if r["y"] is None and _num(r.get(d.outcome_col)) is not None and _num(r.get(d.outcome_col)) <= 0)
        if nonpos:
            items.append(GateItem("G2b", "WARN", "log 변환", f"0 이하 값 {nonpos}건이 결측 처리됨", f"log 변환으로 0 이하 {nonpos}건이 빠짐"))
    # 균형
    per_unit = Counter(r["unit"] for r in panel.rows)
    if len(set(per_unit.values())) > 1:
        short = [u for u, n in per_unit.items() if n < len(times)]
        items.append(GateItem("G2c", "INFO", "패널 균형", f"단위별 관측 수가 다름(짧은 단위 {len(short)}개: {', '.join(short[:6])}{'…' if len(short) > 6 else ''})", "불균형 패널 — 단위 진입·퇴출이 처치와 무관한지 확인 필요"))
    else:
        items.append(GateItem("G2c", "PASS", "패널 균형", f"단위 {len(units)} × 시점 {len(times)} ({times[0]}~{times[-1]})"))
    # 시점표에 있으나 자료에 없는 단위
    ghost = [u for u in d.timing if u not in by_unit and u not in (d.treatment.get("always_treated") or []) and u not in (d.treatment.get("exclude_units") or [])]
    if ghost:
        items.append(GateItem("G2d", "FAIL", "처치 시점표 ↔ 자료", f"시점표에 있지만 자료에 없는 단위: {ghost}", "설계서의 처치 단위가 자료에 없다"))
    untimed = [u for u in units if u not in d.timing]
    if untimed and method != "rdd":
        items.append(GateItem("G2e", "INFO", "시점표에 없는 단위", f"{len(untimed)}개 단위를 미처치로 간주: {', '.join(untimed[:8])}{'…' if len(untimed) > 8 else ''}"))
    for k, v in panel.dropped.items():
        items.append(GateItem("G2f", "INFO", f"제외 단위({k})", ", ".join(v), f"{k} 사유로 제외한 단위: {', '.join(v)}"))
    # 자료 머리의 단절 각주
    for n in panel.notes:
        if "단절" in n or "break" in n.lower():
            items.append(GateItem("G2g", "WARN", "시계열 단절 각주", n, f"자료 출처의 단절 각주: {n}"))

    if method == "rdd":
        items.extend(_gate_rdd(d, panel))
        return items

    # G3 처치 구조
    staggered = len(treated_g) > 1
    if not treated_g:
        items.append(GateItem("G3", "FAIL", "처치군", "처치 시점이 있는 단위가 자료에 없음", "처치군이 없다"))
        return items
    if comparison == "never_treated" and not never:
        items.append(GateItem("G3", "FAIL", "비교군", "미처치 단위가 없는데 comparison=never_treated", "미처치 비교군이 없다 — not_yet_treated 로 바꾸거나 설계를 재고"))
    elif not never and not staggered:
        items.append(GateItem("G3", "FAIL", "비교군", "모든 단위가 같은 시점에 처치되고 미처치 단위도 없음", "비교군이 없어 DiD로 식별되지 않는다(처치 강도 차이만 있다면 연속 처치 설계이며 이 도구의 범위 밖)"))
    else:
        items.append(GateItem("G3", "PASS", "처치 구조", f"코호트 {len(treated_g)}개({', '.join(map(str, treated_g))}), 미처치 {len(never)}개 단위, 비교군={comparison}" + (", staggered" if staggered else "")))
    if not never and staggered and comparison == "not_yet_treated":
        last = max(treated_g)
        items.append(GateItem("G3b", "WARN", "마지막 코호트", f"미처치 단위가 없어 마지막 코호트({last})의 처치 후 효과는 식별되지 않음", f"{last}년 코호트의 처치 후 효과와 그 이후 시점은 비교군이 없어 말할 수 없다"))
    if staggered and method == "twfe":
        items.append(GateItem("G3c", "FAIL", "TWFE 금지", "처치 시점이 단위마다 달라(staggered) 이원고정효과 OLS는 음의 가중·이미 처치된 단위를 비교군으로 쓰는 편향(Goodman-Bacon 2021)", "staggered 처치에 TWFE를 쓰지 않는다 — method를 cs 또는 sa로"))
    if method == "synthdid" and staggered:
        items.append(GateItem("G3d", "FAIL", "synthdid 적용 조건", "synthdid 는 단일 도입 시점(블록 처치)·균형 패널 전제", "도입 시점이 여럿이면 synthdid 대신 cs/sa"))
    if method == "synthdid" and not never:
        items.append(GateItem("G3e", "FAIL", "synthdid 비교군", "합성통제에는 미처치 단위(donor pool)가 필요", "donor pool 없음"))
    elif method == "synthdid" and len(never) <= sum(len(us) for g, us in cohorts.items() if g != NEVER):
        items.append(GateItem("G3e", "WARN", "synthdid donor pool", f"미처치 {len(never)} ≤ 처치 {sum(len(us) for g, us in cohorts.items() if g != NEVER)} — placebo 표준오차 불가, jackknife 사용", "donor pool이 처치 단위보다 적어 placebo 표준오차를 쓸 수 없다(jackknife 대체)"))

    # G4 표본·클러스터
    n_clusters = len({r.get(str(d.data.get("cluster") or d.unit_col), r["unit"]) for r in panel.rows})
    if n_clusters < 5:
        items.append(GateItem("G4", "FAIL", "클러스터 수", f"{n_clusters}개", f"클러스터 {n_clusters}개 — 표준오차를 믿을 수 없다"))
    elif n_clusters < min_clusters:
        items.append(GateItem("G4", "WARN", "클러스터 수", f"{n_clusters}개 (기준 {min_clusters})", f"클러스터 {n_clusters}개로 기준({min_clusters})에 못 미침 — 신뢰구간이 과소 추정될 수 있어 유의성 언어를 쓰지 않는다"))
    else:
        items.append(GateItem("G4", "PASS", "클러스터 수", f"{n_clusters}개"))
    small = {g: us for g, us in cohorts.items() if g != NEVER and len(us) < 2}
    if small:
        items.append(GateItem("G4b", "WARN", "코호트당 단위 수", f"단위 1개뿐인 코호트: {dict(small)}", f"단위 1개 코호트({', '.join(map(str, small))})의 효과는 그 단위의 특이 충격과 구분되지 않는다"))
    for g in treated_g:
        n_pre = len([t for t in times if t < g])
        if n_pre < pre_min:
            sev = "FAIL" if n_pre < 2 else "WARN"
            items.append(GateItem("G4c", sev, f"사전 기간(코호트 {g})", f"{n_pre}기 (기준 {pre_min})", f"코호트 {g}의 사전 기간이 {n_pre}기뿐 — 평행추세를 점검할 수 없다" if n_pre < 2 else f"코호트 {g}의 사전 기간 {n_pre}기로 기준({pre_min})에 못 미침"))
        n_post = len([t for t in times if t >= g])
        max_h = max(h.horizon[1] for h in d.hypotheses)
        if n_post - 1 < max_h:
            items.append(GateItem("G4d", "WARN", f"사후 기간(코호트 {g})", f"{n_post}기, 가설 horizon 최대 {max_h}", f"코호트 {g}는 처치 후 {n_post - 1}기까지만 관측돼 horizon {max_h}까지 말할 수 없다"))

    # G5 사전 추세
    window = tuple(d.design.get("event_window") or [-4, 4])
    raw = pre_trend_raw(panel, (min(window[0], -2), -1), comparison)
    pre_vals = [x["diff"] for x in raw if x["rel"] <= -2 and x["diff"] is not None]
    # 코호트별, 같은 창(t < g)에서 처치군 단위 기울기 vs 비교군 단위 기울기(Welch). 창을 맞춰야 공통 충격이 차이로 둔갑하지 않는다
    unit_series = defaultdict(list)
    for r in panel.rows:
        unit_series[r["unit"]].append((r["time"], r["y"]))
    tests = []
    for g in treated_g:
        tr, cp = [], []
        for u, gu in by_unit.items():
            pre = [(t, y) for t, y in sorted(unit_series[u]) if t < g]
            s = _ols_slope([t for t, _ in pre], [y for _, y in pre])
            if s is None:
                continue
            if gu == g:
                tr.append(s)
            elif gu == NEVER or (comparison == "not_yet_treated" and gu > g):
                cp.append(s)
        w = _welch(tr, cp)
        if w is not None:
            tests.append((g, w))
    if not tests:
        items.append(GateItem("G5", "WARN", "사전 추세 기울기 검정", "비교할 단위가 모자람(코호트·비교군 각 2단위 이상 필요)", "사전 추세 차이를 검정할 수 없었다"))
    else:
        parts, num, den = [], 0.0, 0.0
        for g, w in tests:
            parts.append(f"코호트 {g}: 기울기 차 {w['diff']:.4g}" + (f", t={w['t']:.2f}, p={w['p']:.3f}" if w["t"] is not None else " (분산 0)") + f" ({w['n_a']} vs {w['n_b']} 단위)")
            if w["t"]:
                se = abs(w["diff"] / w["t"])
                num += w["diff"] / se ** 2
                den += 1 / se ** 2
        pooled = None
        if den > 0:
            z = (num / den) / math.sqrt(1 / den)
            pooled = {"diff": num / den, "z": z, "p": 2 * (1 - _norm_cdf(abs(z)))}
        txt = "; ".join(parts) + (f" / 코호트 합산(역분산 가중) 차 {pooled['diff']:.4g}, z={pooled['z']:.2f}, p={pooled['p']:.3f}" if pooled else "")
        if pooled and pooled["p"] < 0.10:
            items.append(GateItem("G5", "WARN", "사전 추세 기울기 검정", txt, f"처치 전 추세가 처치군과 비교군에서 다르다({txt}) — 평행추세 가정이 의심되며, 사전 추세 검정은 통과하더라도 가정을 증명하지 않는다(Roth 2022)"))
        else:
            items.append(GateItem("G5", "PASS", "사전 추세 기울기 검정", txt, "사전 추세 검정 통과는 평행추세 가정의 증명이 아니다(검정력 한계, Roth 2022)"))
    if pre_vals:
        big = [x for x in raw if x["rel"] <= -2 and x["diff"] is not None]
        items.append(GateItem("G5b", "INFO", "사전 기간 원평균 차(rel=-1 기준)", "; ".join(f"e={x['rel']}: {x['diff']:.4g}" for x in big), ""))

    # G6 SUTVA
    sutva = d.assumptions.get("sutva") or {}
    risk = str(sutva.get("spillover_risk") or "").lower()
    just = str(sutva.get("justification") or "").strip()
    if not just:
        items.append(GateItem("G6", "FAIL", "SUTVA 근거", "assumptions.sutva.justification 이 비어 있음", "처치 단위 간 파급(spillover)이 없다는 근거를 적지 않았다"))
    elif risk == "high":
        items.append(GateItem("G6", "WARN", "SUTVA", f"spillover_risk=high: {just}", f"파급 위험을 높음으로 등록함 — 추정치는 직접효과와 파급의 합이며 순효과로 해석하지 않는다({just})"))
    elif risk == "medium":
        items.append(GateItem("G6", "WARN", "SUTVA", f"spillover_risk=medium: {just}", f"파급 가능성(medium): {just}"))
    else:
        items.append(GateItem("G6", "PASS", "SUTVA", f"spillover_risk={risk or 'low'}: {just}"))
    if not str(d.assumptions.get("no_other_policy") or "").strip():
        items.append(GateItem("G6b", "WARN", "동시 정책", "assumptions.no_other_policy 가 비어 있음", "같은 시점의 다른 정책을 점검하지 않았다 — 효과가 이 정책만의 것이라고 말할 수 없다"))
    ant = int(d.treatment.get("anticipation") or 0)
    items.append(GateItem("G7", "INFO", "기대 효과(anticipation)", f"{ant}기" + (f" — {d.assumptions.get('anticipation_note')}" if d.assumptions.get("anticipation_note") else ""), ""))

    # G8 추정기 가용성
    av = engine_availability()
    need = {"cs": ("r:did", "py:differences"), "sa": ("r:fixest",), "twfe": ("py:statsmodels",), "synthdid": ("r:synthdid",)}[method]
    ok = [n for n in need if av.get(n)]
    if ok:
        items.append(GateItem("G8", "PASS", "추정기", f"{method}: {', '.join(ok)} 사용 가능"))
    else:
        items.append(GateItem("G8", "FAIL", "추정기 없음", f"{method} 에 필요한 {', '.join(need)} 중 설치된 것이 없음", INSTALL_HINT.get(need[0], "")))
    return items


def _gate_rdd(d: Design, panel: Panel) -> list[GateItem]:
    items = []
    rd = d.design.get("rdd") or {}
    run_col, cutoff = str(rd.get("running")), float(rd.get("cutoff"))
    if run_col not in panel.columns:
        return [GateItem("G9", "FAIL", "배정 변수", f"열 '{run_col}' 없음", "배정 변수가 자료에 없다")]
    xs = [(_num(r.get(run_col)), r["y"]) for r in panel.rows]
    xs = [(x, y) for x, y in xs if x is not None and y is not None]
    if not xs:
        return [GateItem("G9", "FAIL", "배정 변수", "유효 관측 0", "")]
    lo, hi = min(x for x, _ in xs), max(x for x, _ in xs)
    if not (lo < cutoff < hi):
        items.append(GateItem("G9", "FAIL", "절단점 위치", f"cutoff {cutoff} 가 배정 변수 범위({lo}~{hi}) 밖", "절단점이 자료 범위 밖"))
    left = [x for x, _ in xs if x < cutoff]
    right = [x for x, _ in xs if x >= cutoff]
    items.append(GateItem("G9a", "PASS" if min(len(left), len(right)) >= 30 else "WARN", "절단점 양쪽 관측", f"왼쪽 {len(left)}, 오른쪽 {len(right)}", "" if min(len(left), len(right)) >= 30 else "절단점 한쪽 관측이 30 미만 — 국소 추정이 불안정"))
    # 밀도 불연속 간이 점검(McCrary 대용): 절단점 ± 띠폭(범위 10%) 안 건수 비
    bw = (hi - lo) * 0.1
    nl = len([x for x in left if x >= cutoff - bw])
    nr = len([x for x in right if x < cutoff + bw])
    if nl and nr and (max(nl, nr) / min(nl, nr) > 1.5):
        items.append(GateItem("G9b", "WARN", "배정 변수 밀도(간이)", f"절단점 ±{bw:.3g} 안 왼쪽 {nl} vs 오른쪽 {nr}", f"절단점 주변 밀도가 비대칭({nl} vs {nr}) — 조작(sorting) 가능성, rddensity 로 정식 검정 필요"))
    else:
        items.append(GateItem("G9b", "PASS", "배정 변수 밀도(간이)", f"절단점 ±{bw:.3g} 안 왼쪽 {nl} vs 오른쪽 {nr}", "밀도 간이 점검은 McCrary 검정의 대용이다"))
    if not str((d.assumptions.get("sutva") or {}).get("justification") or "").strip():
        items.append(GateItem("G6", "FAIL", "SUTVA 근거", "비어 있음", "처치 단위 간 파급이 없다는 근거 없음"))
    av = engine_availability()
    if av.get("r:rdrobust"):
        items.append(GateItem("G8", "PASS", "추정기", "rdd: r:rdrobust"))
    else:
        items.append(GateItem("G8", "FAIL", "추정기 없음", "r:rdrobust 미설치", INSTALL_HINT["r:rdrobust"]))
    return items


def gate_status(items: list[GateItem]) -> str:
    sev = {i.severity for i in items}
    if "FAIL" in sev:
        return "FAIL"
    if "WARN" in sev:
        return "WARN"
    return "PASS"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def write_gate(d: Design, design_path: Path, data_path: Path, panel: Panel, items: list[GateItem], out_dir: Path) -> dict:
    window = tuple(d.design.get("event_window") or [-4, 4])
    comparison = str(d.treatment.get("comparison", "not_yet_treated")).lower()
    raw = pre_trend_raw(panel, (window[0], window[1]), comparison) if d.method != "rdd" else []
    g = {
        "tool": f"policy-did {__version__}",
        "design": str(design_path), "design_sha": _sha(design_path),
        "data": str(data_path), "data_sha": _sha(data_path),
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "method": d.method, "status": gate_status(items),
        "items": [asdict(i) for i in items],
        "raw_event_means": raw,
        "n_rows": len(panel.rows), "n_units": len({r['unit'] for r in panel.rows}),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "gate.json").write_text(json.dumps(g, ensure_ascii=False, indent=1), encoding="utf-8")
    if raw:
        (out_dir / "pretrend_raw.svg").write_text(svg_event_study(raw, "diff", None, None, title="사전 추세 원평균 차(처치−비교, rel=-1 기준) — 추정치 아님"), encoding="utf-8")
    return g


def print_gate(g: dict):
    print(f"# 식별 가정 게이트 — {g['status']}  ({g['method']}, {g['n_units']} 단위 · {g['n_rows']} 행)")
    print()
    print("| ID | 판정 | 항목 | 내용 |")
    print("|---|---|---|---|")
    for i in g["items"]:
        print(f"| {i['id']} | {i['severity']} | {i['title']} | {i['detail'].replace('|', '/')} |")
    print()
    if g["status"] == "FAIL":
        print("→ FAIL 항목을 해소하기 전에는 estimate 가 돌지 않는다. 설계서나 자료를 고친 뒤 check 를 다시 한다.")
    elif g["status"] == "WARN":
        print("→ WARN 상태로 추정은 가능하나, WARN 문장이 보고서 '말할 수 없는 것'에 그대로 들어간다.")


# ---------------------------------------------------------------------------
# 5. 추정기 (외부 패키지 호출)
# ---------------------------------------------------------------------------

INSTALL_HINT = {
    "r:did": "R에서 install.packages('did') — 또는 Python: pip install differences",
    "r:fixest": "R에서 install.packages('fixest')",
    "r:synthdid": "R에서 install.packages('synthdid')",
    "r:rdrobust": "R에서 install.packages('rdrobust')",
    "py:statsmodels": "pip install statsmodels pandas",
    "py:differences": "pip install differences",
}


def _r_has(pkg: str) -> bool:
    if not shutil.which("Rscript"):
        return False
    try:
        r = subprocess.run(["Rscript", "-e", f"cat(requireNamespace('{pkg}', quietly=TRUE))"], capture_output=True, text=True, timeout=60)
        return r.stdout.strip().endswith("TRUE")
    except Exception:
        return False


def _py_has(mod: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(mod) is not None


_AV_CACHE: dict = {}


def engine_availability() -> dict:
    if _AV_CACHE:
        return _AV_CACHE
    av = {
        "r:did": _r_has("did"), "r:fixest": _r_has("fixest"), "r:synthdid": _r_has("synthdid"), "r:rdrobust": _r_has("rdrobust"),
        "py:statsmodels": _py_has("statsmodels") and _py_has("pandas"), "py:differences": _py_has("differences") and _py_has("pandas"),
    }
    _AV_CACHE.update(av)
    return av


def cmd_engines(_args):
    av = engine_availability()
    print("| 추정기 | 방법 | 상태 | 설치 |")
    print("|---|---|---|---|")
    use = {"r:did": "cs", "py:differences": "cs", "r:fixest": "sa", "py:statsmodels": "twfe", "r:synthdid": "synthdid", "r:rdrobust": "rdd"}
    for k, ok in av.items():
        print(f"| {k} | {use[k]} | {'있음' if ok else '없음'} | {'' if ok else INSTALL_HINT[k]} |")
    print()
    print("R 사용자 라이브러리가 쓰기 불가이면: Rscript -e 'dir.create(Sys.getenv(\"R_LIBS_USER\"), recursive=TRUE)' 뒤 install.packages(..., lib=Sys.getenv(\"R_LIBS_USER\"))")


def _panel_csv(panel: Panel, path: Path, extra_cols=()):
    units = sorted({r["unit"] for r in panel.rows})
    uid = {u: k + 1 for k, u in enumerate(units)}
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["unit", "unit_id", "time", "y", "g", "treated", *extra_cols])
        for r in panel.rows:
            if r["y"] is None:
                continue
            treated = 1 if (r["g"] != NEVER and r["time"] >= r["g"]) else 0
            w.writerow([r["unit"], uid[r["unit"]], r["time"], r["y"], r["g"], treated, *[_num(r.get(c)) for c in extra_cols]])
    return uid


R_CS = r"""
args <- commandArgs(TRUE); spec <- jsonlite::fromJSON(args[1]); d <- read.csv(spec$data)
suppressPackageStartupMessages(library(did))
res <- att_gt(yname="y", tname="time", idname="unit_id", gname="g", data=d,
              control_group=spec$control, anticipation=spec$anticipation, base_period="universal",
              bstrap=TRUE, cband=FALSE, biters=1000, allow_unbalanced_panel=TRUE)
dyn <- aggte(res, type="dynamic", min_e=spec$min_e, max_e=spec$max_e, na.rm=TRUE)
simple <- aggte(res, type="simple", na.rm=TRUE)
grp <- aggte(res, type="group", na.rm=TRUE)
out <- list(engine="r:did", version=as.character(packageVersion("did")),
  overall=list(att=simple$overall.att, se=simple$overall.se),
  dynamic=data.frame(rel=dyn$egt, att=dyn$att.egt, se=dyn$se.egt),
  group=data.frame(cohort=grp$egt, att=grp$att.egt, se=grp$se.egt),
  n_units=length(unique(d$unit_id)), control=spec$control)
jsonlite::write_json(out, args[2], auto_unbox=TRUE, digits=NA, na="null")
"""

R_SA = r"""
args <- commandArgs(TRUE); spec <- jsonlite::fromJSON(args[1]); d <- read.csv(spec$data)
suppressPackageStartupMessages(library(fixest))
d$cohort <- ifelse(d$g == 0, 10000, d$g)   # 미처치는 먼 미래 코호트
m <- feols(y ~ sunab(cohort, time, ref.p=-1) | unit_id + time, data=d, cluster=~unit_id)
ct <- as.data.frame(coeftable(m)); rel <- as.integer(sub(".*::(-?[0-9]+).*", "\\1", rownames(ct)))
att <- as.data.frame(coeftable(summary(m, agg="att")))
out <- list(engine="r:fixest::sunab", version=as.character(packageVersion("fixest")),
  overall=list(att=att[1,1], se=att[1,2]),
  dynamic=data.frame(rel=rel, att=ct[,1], se=ct[,2]), group=list(),
  n_units=length(unique(d$unit_id)), control="never + not-yet (sunab)")
jsonlite::write_json(out, args[2], auto_unbox=TRUE, digits=NA, na="null")
"""

R_SDID = r"""
args <- commandArgs(TRUE); spec <- jsonlite::fromJSON(args[1]); d <- read.csv(spec$data)
suppressPackageStartupMessages(library(synthdid))
setup <- panel.matrices(d[, c("unit_id","time","y","treated")], unit=1, time=2, outcome=3, treatment=4)
est <- synthdid_estimate(setup$Y, setup$N0, setup$T0)
n_tr <- nrow(setup$Y) - setup$N0
se_method <- if (setup$N0 > n_tr) "placebo" else "jackknife"
se <- tryCatch(sqrt(vcov(est, method=se_method)), error=function(e) NA_real_)
out <- list(engine="r:synthdid", version=as.character(packageVersion("synthdid")),
  overall=list(att=as.numeric(est), se=as.numeric(se)), dynamic=list(), group=list(),
  n_units=nrow(setup$Y), control=sprintf("donor pool %d units, %d pre periods, se=%s", setup$N0, setup$T0, se_method))
jsonlite::write_json(out, args[2], auto_unbox=TRUE, digits=NA, na="null")
"""

R_RDD = r"""
args <- commandArgs(TRUE); spec <- jsonlite::fromJSON(args[1]); d <- read.csv(spec$data)
suppressPackageStartupMessages(library(rdrobust))
x <- d[[spec$running]]; y <- d$y
if (is.null(spec$bandwidth)) r <- rdrobust(y, x, c=spec$cutoff) else r <- rdrobust(y, x, c=spec$cutoff, h=spec$bandwidth)
out <- list(engine="r:rdrobust", version=as.character(packageVersion("rdrobust")),
  overall=list(att=r$coef[1,1], se=r$se[1,1], robust_p=r$pv[3,1], robust_ci=as.numeric(r$ci[3,])),
  dynamic=list(), group=list(), n_units=sum(r$N_h),
  control=sprintf("bandwidth h=%.4g (left n=%d, right n=%d)", r$bws[1,1], r$N_h[1], r$N_h[2]))
jsonlite::write_json(out, args[2], auto_unbox=TRUE, digits=NA, na="null")
"""


def _run_r(script: str, spec: dict, workdir: Path) -> dict:
    sp = workdir / "spec.json"
    sp.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    rs = workdir / "run.R"
    rs.write_text(script, encoding="utf-8")
    outp = workdir / "r_out.json"
    r = subprocess.run(["Rscript", str(rs), str(sp), str(outp)], capture_output=True, text=True, timeout=1800)
    if r.returncode != 0 or not outp.exists():
        raise RuntimeError("R 실행 실패:\n" + (r.stderr[-3000:] or r.stdout[-3000:]))
    res = json.loads(outp.read_text(encoding="utf-8"))
    res["stderr_tail"] = r.stderr[-800:]
    return res


def _py_twfe(data_csv: Path, spec: dict) -> dict:
    import pandas as pd  # type: ignore
    import statsmodels.formula.api as smf  # type: ignore
    df = pd.read_csv(data_csv)
    df["rel"] = (df["time"] - df["g"]).where(df["g"] != 0, -1000)
    lo, hi = spec["min_e"], spec["max_e"]
    df["relb"] = df["rel"].clip(lo, hi).where(df["g"] != 0, -1)
    # 상대시점 더미(-1 기준)
    rels = sorted(set(df.loc[df["g"] != 0, "relb"]) - {-1})
    for e in rels:
        df[f"ev_{'m' if e < 0 else 'p'}{abs(e)}"] = ((df["relb"] == e) & (df["g"] != 0)).astype(int)
    evs = [f"ev_{'m' if e < 0 else 'p'}{abs(e)}" for e in rels]
    m_static = smf.ols("y ~ treated + C(unit_id) + C(time)", df).fit(cov_type="cluster", cov_kwds={"groups": df["unit_id"]})
    m_dyn = smf.ols("y ~ " + " + ".join(evs) + " + C(unit_id) + C(time)", df).fit(cov_type="cluster", cov_kwds={"groups": df["unit_id"]})
    import statsmodels  # type: ignore
    return {
        "engine": "py:statsmodels(TWFE)", "version": statsmodels.__version__,
        "overall": {"att": float(m_static.params["treated"]), "se": float(m_static.bse["treated"])},
        "dynamic": [{"rel": e, "att": float(m_dyn.params[v]), "se": float(m_dyn.bse[v])} for e, v in zip(rels, evs)],
        "group": [], "n_units": int(df["unit_id"].nunique()), "control": "never-treated (TWFE, 비staggered 전용)",
    }


def _py_differences(data_csv: Path, spec: dict) -> dict:
    import pandas as pd  # type: ignore
    from differences import ATTgt  # type: ignore
    df = pd.read_csv(data_csv)
    df["cohort"] = df["g"].where(df["g"] != 0, pd.NA).astype("Int64")
    df = df.set_index(["unit_id", "time"])
    att = ATTgt(data=df, cohort_column="cohort", base_period="universal", anticipation=spec["anticipation"])
    att.fit(formula="y", control_group="never_treated" if spec["control"] == "nevertreated" else "not_yet_treated", progress_bar=False)
    ev = att.aggregate("event")
    simple = att.aggregate("simple")

    def _cols(frame):
        cols = list(frame.columns)
        att_c = next((c for c in cols if str(c[-1] if isinstance(c, tuple) else c).upper().startswith("ATT")), cols[0])
        se_c = next((c for c in cols if "std" in str(c[-1] if isinstance(c, tuple) else c).lower() or str(c[-1] if isinstance(c, tuple) else c).lower() == "se"), cols[1])
        return att_c, se_c

    ac, sc = _cols(ev)
    dyn = []
    for idx, row in ev.iterrows():
        rel = idx[-1] if isinstance(idx, tuple) else idx
        try:
            if spec["min_e"] <= int(rel) <= spec["max_e"]:
                dyn.append({"rel": int(rel), "att": float(row[ac]), "se": float(row[sc])})
        except (TypeError, ValueError):
            pass
    ac2, sc2 = _cols(simple)
    import differences  # type: ignore
    return {
        "engine": "py:differences(ATTgt)", "version": getattr(differences, "__version__", "?"),
        "overall": {"att": float(simple.iloc[0][ac2]), "se": float(simple.iloc[0][sc2])},
        "dynamic": dyn, "group": [], "n_units": int(df.index.get_level_values(0).nunique()), "control": spec["control"],
    }


def estimate(d: Design, panel: Panel, gate: dict, out_dir: Path, engine: str | None = None) -> dict:
    method = d.method
    av = engine_availability()
    window = tuple(d.design.get("event_window") or [-4, 4])
    comparison = str(d.treatment.get("comparison", "not_yet_treated")).lower()
    work = out_dir / "_work"
    work.mkdir(parents=True, exist_ok=True)
    extra = []
    if method == "rdd":
        extra = [str((d.design.get("rdd") or {}).get("running"))]
    data_csv = work / "panel_for_estimation.csv"
    _panel_csv(panel, data_csv, extra)
    spec = {"data": str(data_csv), "control": "nevertreated" if comparison == "never_treated" else "notyettreated",
            "anticipation": int(d.treatment.get("anticipation") or 0), "min_e": window[0], "max_e": window[1]}
    res = None
    tried = []
    if method == "cs":
        order = [engine] if engine else ["r:did", "py:differences"]
        for e in order:
            if e == "r:did" and av.get("r:did"):
                res = _run_r(R_CS, spec, work)
                break
            if e == "py:differences" and av.get("py:differences"):
                res = _py_differences(data_csv, spec)
                break
            tried.append(e)
    elif method == "sa":
        if av.get("r:fixest"):
            res = _run_r(R_SA, spec, work)
    elif method == "twfe":
        if av.get("py:statsmodels"):
            res = _py_twfe(data_csv, spec)
    elif method == "synthdid":
        if av.get("r:synthdid"):
            res = _run_r(R_SDID, spec, work)
    elif method == "rdd":
        rd = d.design.get("rdd") or {}
        spec.update({"running": str(rd.get("running")), "cutoff": float(rd.get("cutoff")), "bandwidth": rd.get("bandwidth")})
        if av.get("r:rdrobust"):
            res = _run_r(R_RDD, spec, work)
    if res is None:
        raise SystemExit(f"추정기가 없어 돌리지 않음(method={method}, 시도={tried or '없음'}). 설치: " + "; ".join(v for k, v in INSTALL_HINT.items() if k.split(':')[1] in {'did', 'differences'} and method == 'cs' or method != 'cs' and k.endswith(('fixest', 'synthdid', 'rdrobust', 'statsmodels'))))
    # 정리: 사건창 밖은 dynamic_full 에 두고 dynamic 은 창 안만
    res["dynamic_full"] = list(res.get("dynamic") or [])
    res["dynamic"] = [r for r in res["dynamic_full"] if r.get("rel") is not None and window[0] <= r["rel"] <= window[1]]
    for row in res.get("dynamic") or []:
        for k in ("att", "se"):
            if row.get(k) is None or (isinstance(row.get(k), float) and math.isnan(row[k])):
                row[k] = None
    res.update({
        "tool": f"policy-did {__version__}", "method": method, "estimated_at": datetime.now().isoformat(timespec="seconds"),
        "design_sha": gate["design_sha"], "data_sha": gate["data_sha"], "gate_status": gate["status"],
        "event_window": list(window), "comparison": comparison,
    })
    (out_dir / "estimate.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return res


# ---------------------------------------------------------------------------
# 6. 보고
# ---------------------------------------------------------------------------

def svg_event_study(points: list[dict], key: str, se_key, zero_line, title="") -> str:
    pts = [p for p in points if p.get(key) is not None]
    W, H, L, R, T, B = 720, 360, 60, 20, 40, 50
    if not pts:
        return f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}"><text x="20" y="40">자료 없음</text></svg>'
    xs = [p["rel"] for p in pts]
    ys = []
    for p in pts:
        v = p[key]
        s = p.get(se_key) if se_key else None
        ys.append(v)
        if s:
            ys += [v - 1.96 * s, v + 1.96 * s]
    ys.append(0.0)
    xmin, xmax = min(xs) - 0.5, max(xs) + 0.5
    ymin, ymax = min(ys), max(ys)
    pad = (ymax - ymin) * 0.1 or 1.0
    ymin, ymax = ymin - pad, ymax + pad

    def X(x):
        return L + (x - xmin) / (xmax - xmin) * (W - L - R)

    def Y(y):
        return T + (ymax - y) / (ymax - ymin) * (H - T - B)

    o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" font-family="sans-serif" font-size="12">',
         f'<rect width="{W}" height="{H}" fill="white"/>',
         f'<text x="{L}" y="22" font-size="13" fill="#222">{_esc(title)}</text>',
         f'<line x1="{L}" y1="{Y(0):.1f}" x2="{W - R}" y2="{Y(0):.1f}" stroke="#888" stroke-dasharray="4 3"/>',
         f'<line x1="{X(-0.5):.1f}" y1="{T}" x2="{X(-0.5):.1f}" y2="{H - B}" stroke="#c44" stroke-dasharray="2 3"/>',
         f'<line x1="{L}" y1="{H - B}" x2="{W - R}" y2="{H - B}" stroke="#333"/>',
         f'<line x1="{L}" y1="{T}" x2="{L}" y2="{H - B}" stroke="#333"/>']
    for x in range(int(math.floor(xmin)), int(math.ceil(xmax)) + 1):
        o.append(f'<text x="{X(x):.1f}" y="{H - B + 16}" text-anchor="middle">{x}</text>')
    for k in range(6):
        y = ymin + (ymax - ymin) * k / 5
        o.append(f'<text x="{L - 6}" y="{Y(y) + 4:.1f}" text-anchor="end">{y:.3g}</text>')
    prev = None
    for p in pts:
        x, v = X(p["rel"]), p[key]
        s = p.get(se_key) if se_key else None
        if s:
            o.append(f'<line x1="{x:.1f}" y1="{Y(v - 1.96 * s):.1f}" x2="{x:.1f}" y2="{Y(v + 1.96 * s):.1f}" stroke="#1f4e79" stroke-width="1.5"/>')
        if prev:
            o.append(f'<line x1="{prev[0]:.1f}" y1="{prev[1]:.1f}" x2="{x:.1f}" y2="{Y(v):.1f}" stroke="#1f4e79" stroke-opacity="0.4"/>')
        o.append(f'<circle cx="{x:.1f}" cy="{Y(v):.1f}" r="4" fill="{"#1f4e79" if p["rel"] >= 0 else "#777"}"/>')
        prev = (x, Y(v))
    o.append(f'<text x="{(L + W - R) / 2:.0f}" y="{H - 8}" text-anchor="middle">처치 시점 대비 상대 기간 (0 = 처치 첫 해, 기준 −1)</text>')
    o.append("</svg>")
    return "\n".join(o)


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fmt(v, nd=3):
    if v is None:
        return "—"
    return f"{v:.{nd}g}" if abs(v) < 1e4 else f"{v:,.0f}"


def build_report(d: Design, gate: dict, est: dict, out_dir: Path, data_path: Path) -> str:
    items = gate["items"]
    warns = [i for i in items if i["severity"] in ("WARN", "FAIL") and i.get("say")]
    infos = [i for i in items if i["severity"] in ("INFO", "PASS") and i.get("say")]
    ov = est.get("overall") or {}
    att, se = ov.get("att"), ov.get("se")
    ci = (att - 1.96 * se, att + 1.96 * se) if att is not None and se else None
    unit_label = "log 차이(≈비율 변화)" if d.data.get("log_outcome") else f"'{d.outcome_col}' 단위"
    few_clusters = any(i["id"] == "G4" and i["severity"] != "PASS" for i in items)
    lines = [f"# {d.name} — 추정 결과 보고", "",
             f"- 질문: {d.question}", f"- 처치: {d.treatment.get('name', '')} · 방법: {d.method} ({est.get('engine')} {est.get('version', '')})",
             f"- 자료: `{data_path.name}` (sha {gate['data_sha']}) · 결과변수 `{d.outcome_col}` ({d.data.get('outcome_source', '출처 미기재')})",
             f"- 설계서 sha {gate['design_sha']} · 게이트 {gate['status']} ({gate['checked_at']}) · 추정 {est.get('estimated_at')}",
             f"- 비교군: {est.get('control')} · 단위 {est.get('n_units')} · 사건창 {est.get('event_window')}", ""]
    lines += ["## 1. 추정표", "", "| 추정량 | 추정치 | 표준오차 | 95% 구간 | 단위 |", "|---|---|---|---|---|",
              f"| 전체 ATT | {_fmt(att)} | {_fmt(se)} | {f'[{_fmt(ci[0])}, {_fmt(ci[1])}]' if ci else '—'} | {unit_label} |"]
    if ov.get("robust_p") is not None:
        lines.append(f"| (rdrobust 강건 p) | {_fmt(ov['robust_p'])} | | [{_fmt(ov['robust_ci'][0])}, {_fmt(ov['robust_ci'][1])}] | |")
    dyn = est.get("dynamic") or []
    if dyn:
        lines += ["", "| 상대 기간 | ATT | SE | 95% 구간 |", "|---|---|---|---|"]
        for p in sorted(dyn, key=lambda x: x["rel"]):
            a, s = p.get("att"), p.get("se")
            c = f"[{_fmt(a - 1.96 * s)}, {_fmt(a + 1.96 * s)}]" if a is not None and s else "—"
            lines.append(f"| {p['rel']:+d} | {_fmt(a)} | {_fmt(s)} | {c} |")
        (out_dir / "event_study.svg").write_text(svg_event_study(dyn, "att", "se", 0, title=f"{d.name}: 사건연구 ({est.get('engine')})"), encoding="utf-8")
        lines += ["", "![사건연구](event_study.svg)"]
    grp = est.get("group") or []
    if grp:
        lines += ["", "| 코호트 | ATT | SE |", "|---|---|---|"]
        for p in grp:
            lines.append(f"| {p.get('cohort')} | {_fmt(p.get('att'))} | {_fmt(p.get('se'))} |")

    # 2. 가설별 판정
    lines += ["", "## 2. 사전 등록 가설별 판정", "", "| 가설 | 등록 방향 | 관측 | 판정 |", "|---|---|---|---|"]
    for h in d.hypotheses:
        hz = h.horizon
        in_h = [p for p in dyn if hz[0] <= p["rel"] <= hz[1] and p.get("att") is not None]
        if in_h:
            m = sum(p["att"] for p in in_h) / len(in_h)
            obs = f"horizon {hz}: 평균 {_fmt(m)} ({len(in_h)}기)"
        elif att is not None:
            m = att
            obs = f"전체 ATT {_fmt(att)}"
        else:
            m = None
            obs = "추정치 없음"
        if m is None:
            verdict = "판정 불가"
        else:
            sign = "positive" if m > 0 else "negative" if m < 0 else "zero"
            sig = (ci is not None and (ci[0] > 0 or ci[1] < 0)) if not in_h else all((p["att"] - 1.96 * p["se"] > 0) or (p["att"] + 1.96 * p["se"] < 0) for p in in_h if p.get("se"))
            if h.direction == "zero":
                verdict = "구간이 0을 포함 — 등록(영효과)과 모순되지 않음" if not sig else "0과 구분되는 효과 — 등록(영효과)과 어긋남"
            elif sign == h.direction:
                verdict = "방향 일치" + ("" if sig else " (구간이 0을 포함)")
            else:
                verdict = "방향 불일치" + ("" if sig else " (구간이 0을 포함)")
            if few_clusters:
                verdict += " · 클러스터 수 부족으로 유의성 언급 보류"
        lines.append(f"| {h.id} {h.statement} | {h.direction} | {obs} | {verdict} |")

    # 3. 말할 수 있는 것 / 없는 것
    lines += ["", "## 3. 말할 수 있는 것", ""]
    comp_txt = {"never_treated": "같은 기간 미처치 단위", "not_yet_treated": "아직 처치되지 않은 단위(미처치 포함)"}.get(est.get("comparison"), est.get("control"))
    lines.append(f"- 이 추정치는 **{d.treatment.get('name', '처치')}** 를 받은 단위의 `{d.outcome_col}` 가, {comp_txt}와 비교해 처치 전 차이를 유지했을 때(평행추세)보다 얼마나 달라졌는가이다.")
    lines.append(f"- 말할 수 있는 범위는 설계서에 등록한 가설 {', '.join(h.id for h in d.hypotheses)} 의 결과변수·horizon 안이다. 다른 결과변수·다른 기간에 대한 해석은 이 보고서에 없다.")
    if dyn:
        pre = [p for p in dyn if p["rel"] < -1 and p.get("att") is not None]
        if pre:
            lines.append(f"- 처치 전 상대 기간({', '.join(str(p['rel']) for p in pre)})의 추정치는 평행추세의 '관측 가능한 부분'에 대한 점검이며, 0에 가까워도 가정의 증명은 아니다.")
    lines.append(f"- 단위가 '{d.unit_col}' 이므로 효과는 그 단위 수준의 평균이다. 하위 단위(개별 과제·연구자·기관)의 효과로 옮기지 않는다.")
    lines += ["", "## 4. 말할 수 없는 것 · 주의 (게이트에서 그대로 옮김)", ""]
    if warns:
        for i in warns:
            lines.append(f"- [{i['id']} {i['severity']}] {i['say']}")
    else:
        lines.append("- 게이트 WARN 항목 없음.")
    for i in infos:
        lines.append(f"- [{i['id']}] {i['say']}")
    lines.append("- 추정치는 등록한 비교군·사건창·가정 아래의 값이며, 설계서를 바꾸면 다시 게이트부터 돈다(설계서·자료 해시가 보고서 머리에 있다).")
    lines += ["", "## 5. 재현", "", "```bash",
              f"python3 policy_did.py check {Path(gate['design']).name} --data {data_path.name} --out {out_dir.name}",
              f"python3 policy_did.py estimate {Path(gate['design']).name} --data {data_path.name} --out {out_dir.name}",
              f"python3 policy_did.py report {Path(gate['design']).name} --data {data_path.name} --out {out_dir.name}", "```", ""]
    text = "\n".join(lines)
    (out_dir / "report.md").write_text(text, encoding="utf-8")
    return text


# ---------------------------------------------------------------------------
# 7. 자료 준비: 합성 패널 · KOSIS 긴 형식 → 패널
# ---------------------------------------------------------------------------

def simulate(out_dir: Path, n_units=60, t0=2010, t1=2024, cohorts=(2015, 2018, 2021), n_never=24, effect=2.0, growth=0.3, seed=7, trend_break=False, noise=0.5):
    """효과를 알고 있는 staggered 패널. 효과는 처치 후 'effect'만큼 수준 이동(동적으로 0.5씩 커짐)."""
    import random
    rnd = random.Random(seed)
    units = [f"U{k:02d}" for k in range(1, n_units + 1)]
    timing = {}
    treated_units = units[: n_units - n_never]
    for k, u in enumerate(treated_units):
        timing[u] = cohorts[k % len(cohorts)]
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "panel.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["unit", "year", "y", "x1"])
        for u in units:
            alpha = rnd.gauss(50, 5)
            slope = growth + (0.4 if (trend_break and u in timing) else 0.0)
            for t in range(t0, t1 + 1):
                y = alpha + slope * (t - t0) + 0.8 * math.sin(t) + rnd.gauss(0, noise)
                g = timing.get(u)
                if g and t >= g:
                    y += effect + 0.5 * (t - g)
                w.writerow([u, t, f"{y:.4f}", f"{rnd.random():.3f}"])
    design = DESIGN_TEMPLATE.format(today=datetime.now().date().isoformat())
    design = design.replace("name: 예시_제도도입_효과", "name: 합성_staggered_예시")
    design = design.replace('question: "제도 X 도입이 단위별 결과 Y를 바꿨는가"', 'question: "합성 자료: 처치가 y를 effect만큼 올리는가(참값 2.0 + 0.5/년)"')
    timing_block = "\n".join(f"    {u}: {g}" for u, g in timing.items())
    design = re.sub(r"  timing:.*?\n(    .*\n)+", "  timing:\n" + timing_block + "\n", design, count=1)
    if len(cohorts) == 1:
        design = design.replace("comparison: not_yet_treated", "comparison: never_treated")
    design = design.replace('    justification: "처치 단위 간 자원 이동이 없는 이유"', '    justification: "합성 자료 — 단위 간 상호작용 없이 생성"')
    design = design.replace('  no_other_policy: "같은 시점에 겹친 다른 정책과 그 처리"', '  no_other_policy: "합성 자료 — 다른 정책 없음"')
    design = design.replace('    statement: "도입 후 3년 내 Y가 증가한다"', '    statement: "처치 후 0~3기에 y가 증가한다(참값 양)"')
    (out_dir / "design.yaml").write_text(design, encoding="utf-8")
    (out_dir / "truth.json").write_text(json.dumps({"effect_level": effect, "effect_slope_per_year": 0.5, "cohorts": list(cohorts), "n_never": n_never, "trend_break": trend_break, "seed": seed}, ensure_ascii=False, indent=1), encoding="utf-8")
    return out_dir


def cmd_panel(args):
    """kr-sti-data export(긴 형식: prd_de, itm_id/itm_nm, c1_nm…, dt) → unit,year,<outcome> 패널."""
    rows, notes, cols = read_csv_with_notes(Path(args.src))
    unit_col = args.unit_col
    item_col = "itm_nm" if args.item in {r.get("itm_nm") for r in rows} else "itm_id"
    sel = [r for r in rows if r.get(item_col) == args.item]
    if not sel:
        raise SystemExit(f"항목 '{args.item}' 이 없음. 있는 항목: {sorted({(r.get('itm_id'), r.get('itm_nm')) for r in rows})}")
    # 더 세분된 분류(c2…)가 있으면 합계 행만 남기도록 --filter 로 지정
    filt = {}
    for f in args.filter or []:
        if "=" not in f:
            raise SystemExit("--filter 는 열=값 형식이어야 합니다")
        k, v = f.split("=", 1)
        if k not in cols:
            raise SystemExit(f"필터 열이 없습니다: {k}")
        filt[k] = v
    sel = [r for r in sel if all(r.get(k) == v for k, v in filt.items())]
    if args.drop:
        sel = [r for r in sel if r.get(unit_col) not in set(args.drop)]
    if not sel:
        raise SystemExit("필터·제외 조건 뒤에 남는 행이 없습니다")
    for col in (unit_col, "prd_de", "dt"):
        if col not in cols:
            raise SystemExit(f"자료에 열 '{col}' 이 없습니다")
    if len({r.get("unit") for r in sel}) > 1:
        raise SystemExit("서로 다른 금액 단위가 섞여 있습니다 — 단위를 먼저 통일하세요")
    seen = set()
    for r in sel:
        year = _num(r["prd_de"])
        if not (r[unit_col] or "").strip() or year is None or not year.is_integer():
            raise SystemExit("단위 식별자 또는 연도가 잘못되었습니다")
        r[unit_col] = r[unit_col].strip()
        r["prd_de"] = str(int(year))
        key = (r[unit_col], r["prd_de"])
        if key in seen:
            raise SystemExit("단위×연도 중복입니다 — --filter 로 분류를 하나로 좁히세요")
        seen.add(key)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as f:
        for n in notes:
            f.write(f"# {n}\n")
        w = csv.writer(f)
        w.writerow(["unit", "year", args.name])
        for r in sorted(sel, key=lambda r: (r.get(unit_col), int(r["prd_de"]))):
            w.writerow([r.get(unit_col), int(r["prd_de"]), r.get("dt")])
    units = sorted({r.get(unit_col) for r in sel})
    years = sorted({int(r["prd_de"]) for r in sel})
    print(f"→ {out} : {len(sel)}행, 단위 {len(units)}, 연도 {years[0]}~{years[-1]}, 각주 {len(notes)}")
    print("단위:", ", ".join(units))


# ---------------------------------------------------------------------------
# 8. 명령
# ---------------------------------------------------------------------------

def _load(args):
    design_path = Path(args.design)
    d, errs = load_design(design_path)
    if errs:
        print("설계서 오류:")
        for e in errs:
            print(f"  - {e}")
        raise SystemExit(2)
    filename = args.data or d.data.get("file")
    if not filename:
        raise SystemExit("자료 파일을 설계서 data.file 또는 --data 로 지정하세요")
    data_path = Path(filename)
    if not data_path.is_file():
        cand = design_path.parent / (d.data.get("file") or "")
        if not args.data and cand.is_file():
            data_path = cand
        else:
            raise SystemExit(f"자료 파일이 없음: {data_path} (설계서 data.file 또는 --data)")
    out_dir = Path(args.out) if args.out else design_path.parent / "out"
    return d, design_path, data_path, out_dir


def cmd_init(args):
    p = Path(args.out)
    if p.exists() and not args.force:
        raise SystemExit(f"{p} 가 이미 있음 (--force 로 덮어씀)")
    p.write_text(DESIGN_TEMPLATE.format(today=datetime.now().date().isoformat()), encoding="utf-8")
    print(f"→ {p}  (자료를 보기 전에 가설·방향·비교군을 적는다)")


def simulate_rdd(out_dir: Path, n=600, cutoff=60.0, effect=5.0, seed=7):
    """배정 점수 score(0~100)가 cutoff 이상이면 선정. 결과 y = 20 + 0.3·score + effect·1[score≥cutoff] + 잡음."""
    import random
    rnd = random.Random(seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "rdd.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["unit", "year", "score", "y"])
        for k in range(n):
            sc = rnd.uniform(0, 100)
            y = 20 + 0.3 * sc + (effect if sc >= cutoff else 0) + rnd.gauss(0, 2)
            w.writerow([f"P{k:04d}", 2020, f"{sc:.3f}", f"{y:.4f}"])
    design = f"""name: 합성_RDD_예시
question: "합성 자료: 선정 점수 {cutoff:g}점 경계에서 y가 {effect:g}만큼 뛰는가"
registered_at: {datetime.now().date().isoformat()}
data:
  file: rdd.csv
  unit: unit
  time: year
  outcome: y
  outcome_source: "합성"
treatment:
  name: "점수 {cutoff:g}점 이상 선정"
  timing: {{}}
hypotheses:
  - id: H1
    outcome: y
    direction: positive
    horizon: [0, 0]
    statement: "선정 경계에서 y가 양의 불연속을 보인다(참값 {effect:g})"
assumptions:
  sutva:
    spillover_risk: low
    justification: "합성 자료"
  no_other_policy: "합성 자료"
design:
  method: rdd
  rdd:
    running: score
    cutoff: {cutoff:g}
    bandwidth: null
"""
    (out_dir / "design.yaml").write_text(design, encoding="utf-8")
    (out_dir / "truth.json").write_text(json.dumps({"effect": effect, "cutoff": cutoff, "n": n, "seed": seed}, indent=1), encoding="utf-8")
    return out_dir


def cmd_simulate(args):
    if args.rdd:
        out = simulate_rdd(Path(args.out), effect=args.effect, seed=args.seed)
        print(f"→ {out}/rdd.csv, design.yaml, truth.json (참값 불연속={args.effect})")
        return
    cohorts = tuple(args.cohorts) if args.cohorts else (2015, 2018, 2021)
    out = simulate(Path(args.out), n_units=args.units, n_never=args.never, effect=args.effect, seed=args.seed, trend_break=args.trend_break, cohorts=cohorts)
    print(f"→ {out}/panel.csv, design.yaml, truth.json (참값 effect={args.effect}, +0.5/년, 코호트 {cohorts})")


def cmd_check(args):
    d, design_path, data_path, out_dir = _load(args)
    panel = build_panel(d, data_path)
    items = run_gate(d, panel)
    g = write_gate(d, design_path, data_path, panel, items, out_dir)
    print_gate(g)
    print(f"→ {out_dir}/gate.json" + (f", pretrend_raw.svg" if g["raw_event_means"] else ""))
    return 0 if g["status"] != "FAIL" else 1


def _require_gate(d, design_path, data_path, out_dir) -> dict:
    gp = out_dir / "gate.json"
    if not gp.exists():
        raise SystemExit("gate.json 이 없음 — 먼저 `check` 를 돌린다. 게이트 없이는 추정하지 않는다.")
    g = json.loads(gp.read_text(encoding="utf-8"))
    if g["design_sha"] != _sha(design_path) or g["data_sha"] != _sha(data_path):
        raise SystemExit("설계서나 자료가 게이트 이후 바뀜 — `check` 를 다시 돌린다.")
    if g["status"] == "FAIL":
        fails = [i for i in g["items"] if i["severity"] == "FAIL"]
        print("게이트 FAIL — 추정하지 않음:")
        for i in fails:
            print(f"  - [{i['id']}] {i['title']}: {i['detail']}")
        raise SystemExit(3)
    return g


def cmd_estimate(args):
    d, design_path, data_path, out_dir = _load(args)
    g = _require_gate(d, design_path, data_path, out_dir)
    panel = build_panel(d, data_path)
    est = estimate(d, panel, g, out_dir, engine=args.engine)
    ov = est.get("overall") or {}
    print(f"{est['engine']} {est.get('version', '')}: ATT={_fmt(ov.get('att'))} (SE {_fmt(ov.get('se'))}), 동적 {len(est.get('dynamic') or [])}기")
    print(f"→ {out_dir}/estimate.json")


def cmd_report(args):
    d, design_path, data_path, out_dir = _load(args)
    g = _require_gate(d, design_path, data_path, out_dir)
    ep = out_dir / "estimate.json"
    if not ep.exists():
        raise SystemExit("estimate.json 이 없음 — 먼저 `estimate`")
    est = json.loads(ep.read_text(encoding="utf-8"))
    if est.get("design_sha") != g["design_sha"] or est.get("data_sha") != g["data_sha"]:
        raise SystemExit("추정 결과가 현재 게이트와 다른 설계서·자료에서 나옴 — estimate 를 다시")
    text = build_report(d, g, est, out_dir, data_path)
    if not args.quiet:
        print(text)
    print(f"→ {out_dir}/report.md" + (", event_study.svg" if (out_dir / 'event_study.svg').exists() else ""))


def cmd_run(args):
    rc = cmd_check(args)
    if rc:
        raise SystemExit(3)
    cmd_estimate(args)
    args.quiet = True
    cmd_report(args)


def cmd_selftest(args):
    import unittest
    sys.argv = [sys.argv[0]]
    here = Path(__file__).resolve().parent
    suite = unittest.defaultTestLoader.discover(str(here / "tests"), pattern="test_*.py")
    res = unittest.TextTestRunner(verbosity=1).run(suite)
    raise SystemExit(0 if res.wasSuccessful() else 1)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="policy_did.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"policy-did {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="설계서 서식")
    p.add_argument("--out", default="design.yaml")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("simulate", help="합성 staggered 패널")
    p.add_argument("--out", default="examples/synthetic")
    p.add_argument("--units", type=int, default=60)
    p.add_argument("--never", type=int, default=24)
    p.add_argument("--cohorts", type=int, nargs="*", help="처치 시점들(하나만 주면 비staggered)")
    p.add_argument("--rdd", action="store_true", help="RDD 합성 자료(score·cutoff)")
    p.add_argument("--effect", type=float, default=2.0)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--trend-break", action="store_true", help="처치군에 사전 추세 차이를 심는다(게이트 WARN 시험용)")
    p.set_defaults(fn=cmd_simulate)

    p = sub.add_parser("panel", help="kr-sti-data export → 패널")
    p.add_argument("src")
    p.add_argument("--item", required=True, help="itm_nm 또는 itm_id (예: 금액)")
    p.add_argument("--unit-col", default="c1_nm")
    p.add_argument("--name", default="y", help="결과변수 열 이름")
    p.add_argument("--filter", action="append", help="col=value (예: c2_nm=합계)")
    p.add_argument("--drop", nargs="*", help="제외할 단위(합계 등)")
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_panel)

    for name, fn, h in (("check", cmd_check, "식별 가정 게이트"), ("estimate", cmd_estimate, "추정(게이트 통과분만)"), ("report", cmd_report, "보고서"), ("run", cmd_run, "check→estimate→report")):
        p = sub.add_parser(name, help=h)
        p.add_argument("design")
        p.add_argument("--data")
        p.add_argument("--out")
        p.add_argument("--engine", help="cs 전용: r:did | py:differences")
        p.add_argument("--quiet", action="store_true")
        p.set_defaults(fn=fn)

    sub.add_parser("engines", help="추정기 가용성").set_defaults(fn=cmd_engines)
    sub.add_parser("selftest", help="자체 점검").set_defaults(fn=cmd_selftest)

    args = ap.parse_args(argv)
    rc = args.fn(args)
    raise SystemExit(rc or 0)


if __name__ == "__main__":
    main()
