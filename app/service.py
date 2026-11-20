"""把 SQLite 原始记录装配为求解器输入，并负责结果序列化。

浮动树轮年表：每处遗址至多一份，重复提交整份替换。求解时把每个候选始年
分别换算成一组钉年（候选始年 + 木样末轮偏移 = 该层沉积年），全遗址
（含互不连通的层位）共用同一个始年；存在完整解的候选即为可行始年。
展示层把各可行始年下的可行年代段取并集（保留空档）；最早、最晚赋值分别
取最小、最大可行始年下各自的一套完整解。候选始年全部保存在库中，每次
查询都由已提交的原始记录现场重算，因此撤销关系后重新符合条件的始年会
自动回到结果中。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Optional

from . import db
from .solver import (
    Contradiction,
    CorrelationInput,
    GroupResult,
    PinInput,
    Solution,
    StratumInput,
    SuperpositionInput,
    normalize_intervals,
    solve,
)


@dataclass(frozen=True)
class SampleData:
    id: int
    stratum_id: int
    offset_years: int


@dataclass(frozen=True)
class ChronologyData:
    id: int
    site_id: int
    candidate_start_years: list[int]  # 升序
    samples: list[SampleData]
    created: str


@dataclass
class SiteVerdict:
    """一次完整的现场求解结论（含浮动年表时的全部候选始年）。"""

    feasible: bool
    solution: Solution  # 主视图：无年表=原解；有年表=可行候选的并集视图
    chronology: Optional[ChronologyData]
    feasible_start_years: Optional[list[int]]  # 有年表时升序；无年表为 None
    per_candidate: dict[int, Solution]  # 有年表时：每个候选始年各自的解
    contradiction: Optional[Contradiction]  # 代表矛盾（全候选失效时取最小候选的）


def _load_inputs(
    conn: sqlite3.Connection, site_id: int
) -> tuple[list[StratumInput], list[CorrelationInput], list[SuperpositionInput]]:
    strata: list[StratumInput] = []
    for row in db.list_strata(conn, site_id):
        intervals = normalize_intervals(db.get_intervals(conn, row["id"]))
        strata.append(
            StratumInput(
                id=row["id"],
                code=f"{row['trench']}@{row['layer_no']}",
                intervals=intervals,
            )
        )
    correlations = [
        CorrelationInput(id=r["id"], a_id=r["stratum_a_id"], b_id=r["stratum_b_id"])
        for r in db.list_correlations(conn, site_id)
    ]
    superpositions = [
        SuperpositionInput(id=r["id"], lower_id=r["lower_id"], upper_id=r["upper_id"])
        for r in db.list_superpositions(conn, site_id)
    ]
    return strata, correlations, superpositions


def load_chronology(conn: sqlite3.Connection, site_id: int) -> Optional[ChronologyData]:
    row = db.get_chronology(conn, site_id)
    if row is None:
        return None
    return ChronologyData(
        id=row["id"],
        site_id=row["site_id"],
        candidate_start_years=db.get_chronology_candidates(conn, row["id"]),
        samples=[
            SampleData(id=s["id"], stratum_id=s["stratum_id"], offset_years=s["offset_years"])
            for s in db.get_chronology_samples(conn, row["id"])
        ],
        created=row["created"],
    )


def solve_site(conn: sqlite3.Connection, site_id: int) -> SiteVerdict:
    """按当前已提交（或事务内未提交）的记录现场求解。"""
    strata, correlations, superpositions = _load_inputs(conn, site_id)
    chrono = load_chronology(conn, site_id)

    if chrono is None:  # 无年表的遗址沿用原行为
        solution = solve(strata, correlations, superpositions)
        return SiteVerdict(
            feasible=solution.feasible,
            solution=solution,
            chronology=None,
            feasible_start_years=None,
            per_candidate={},
            contradiction=solution.contradiction,
        )

    codes = {s.id: s.code for s in strata}
    per_candidate: dict[int, Solution] = {}
    for start_year in chrono.candidate_start_years:
        pins = [
            PinInput(
                sample_id=sample.id,
                stratum_id=sample.stratum_id,
                year=start_year + sample.offset_years,
                detail=(
                    f"木样 #{sample.id}"
                    f"（{codes.get(sample.stratum_id, sample.stratum_id)}）："
                    f"末轮距年表起点 {sample.offset_years} 年，"
                    f"候选始年 {start_year} → 沉积年 {start_year + sample.offset_years}"
                ),
            )
            for sample in chrono.samples
        ]
        per_candidate[start_year] = solve(strata, correlations, superpositions, pins)

    feasible_years = [y for y in chrono.candidate_start_years if per_candidate[y].feasible]
    if not feasible_years:
        first_year = chrono.candidate_start_years[0]
        return SiteVerdict(
            feasible=False,
            solution=per_candidate[first_year],
            chronology=chrono,
            feasible_start_years=[],
            per_candidate=per_candidate,
            contradiction=per_candidate[first_year].contradiction,
        )
    return SiteVerdict(
        feasible=True,
        solution=_union_view(per_candidate, feasible_years),
        chronology=chrono,
        feasible_start_years=feasible_years,
        per_candidate=per_candidate,
        contradiction=None,
    )


def _union_view(per_candidate: dict[int, Solution], feasible_years: list[int]) -> Solution:
    """把各可行始年下的解合并为展示视图。

    分组与组间边与钉年无关，各候选一致；每组的可行年代段取各候选解的并集
    （保留空档）；最早、最晚赋值分别取最小、最大可行始年下各自的一套完整解。
    """
    first = per_candidate[feasible_years[0]]
    last = per_candidate[feasible_years[-1]]
    groups: list[GroupResult] = []
    for g in first.groups:
        gid = g.group_id
        intervals: list[tuple[int, int]] = []
        earliest: Optional[int] = None
        latest: Optional[int] = None
        for year in feasible_years:
            gy = next(x for x in per_candidate[year].groups if x.group_id == gid)
            intervals.extend(gy.feasible_years)
            if gy.earliest_year is not None and (
                earliest is None or gy.earliest_year < earliest
            ):
                earliest = gy.earliest_year
            if gy.latest_year is not None and (
                latest is None or gy.latest_year > latest
            ):
                latest = gy.latest_year
        groups.append(
            GroupResult(
                group_id=gid,
                member_ids=g.member_ids,
                domain=g.domain,
                feasible_years=normalize_intervals(intervals),
                earliest_year=earliest,
                latest_year=latest,
            )
        )
    return Solution(
        feasible=True,
        groups=groups,
        group_edges=first.group_edges,
        group_of=first.group_of,
        contradiction=None,
        earliest_assignment=first.earliest_assignment,
        latest_assignment=last.latest_assignment,
    )


def serialize_chronology(chrono: ChronologyData) -> dict[str, Any]:
    return {
        "id": chrono.id,
        "site_id": chrono.site_id,
        "candidate_start_years": list(chrono.candidate_start_years),
        "samples": [
            {"id": s.id, "stratum_id": s.stratum_id, "offset_years": s.offset_years}
            for s in chrono.samples
        ],
        "created": chrono.created,
    }


def serialize_solution(verdict: SiteVerdict, version: int) -> dict[str, Any]:
    solution = verdict.solution
    payload: dict[str, Any] = {
        "feasible": verdict.feasible,
        "version": version,
        "groups": [
            {
                "group_id": g.group_id,
                "member_ids": g.member_ids,
                "domain": [[lo, hi] for lo, hi in g.domain],
                "feasible_years": [[lo, hi] for lo, hi in g.feasible_years],
                "earliest_year": g.earliest_year,
                "latest_year": g.latest_year,
            }
            for g in solution.groups
        ],
        "group_edges": [
            {
                "from_group": e.from_group,
                "to_group": e.to_group,
                "edge_ids": e.edge_ids,
            }
            for e in solution.group_edges
        ],
        "group_of": {str(sid): gid for sid, gid in solution.group_of.items()},
        "contradiction": None,
        "earliest_assignment": None,
        "latest_assignment": None,
        "feasible_start_years": verdict.feasible_start_years,
        "chronology": (
            serialize_chronology(verdict.chronology) if verdict.chronology else None
        ),
    }
    if solution.contradiction is not None:
        payload["contradiction"] = serialize_contradiction(solution.contradiction)
    if solution.earliest_assignment is not None:
        payload["earliest_assignment"] = {
            str(gid): year for gid, year in solution.earliest_assignment.items()
        }
    if solution.latest_assignment is not None:
        payload["latest_assignment"] = {
            str(gid): year for gid, year in solution.latest_assignment.items()
        }
    return payload


def serialize_contradiction(c: Contradiction) -> dict[str, Any]:
    return {
        "reason": c.reason,
        "message": c.message,
        "witness": [
            {"kind": w.kind, "record_id": w.record_id, "detail": w.detail}
            for w in c.witness
        ],
    }


def serialize_stratum(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "site_id": row["site_id"],
        "trench": row["trench"],
        "layer_no": row["layer_no"],
        "note": row["note"],
        "intervals": [
            {"year_from": lo, "year_to": hi}
            for lo, hi in db.get_intervals(conn, row["id"])
        ],
        "created": row["created"],
    }
