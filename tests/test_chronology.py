"""浮动树轮年表：候选始年求解、整份替换、409 回滚与撤销恢复。"""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.solver import (
    CorrelationInput,
    PinInput,
    StratumInput,
    SuperpositionInput,
    solve,
)


@pytest.fixture()
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def _make_site(client, name="测年遗址"):
    r = client.post("/sites", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["record"]["id"]


def _add_stratum(client, site_id, trench, layer_no, intervals):
    r = client.post(
        f"/sites/{site_id}/strata",
        json={
            "trench": trench,
            "layer_no": layer_no,
            "intervals": [{"year_from": lo, "year_to": hi} for lo, hi in intervals],
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["record"]["id"]


def _put_chronology(client, site_id, candidates, samples):
    return client.put(
        f"/sites/{site_id}/chronology",
        json={
            "candidate_start_years": candidates,
            "samples": [
                {"stratum_id": sid, "offset_years": off} for sid, off in samples
            ],
        },
    )


def _group_of(sol, stratum_id):
    return sol["group_of"][str(stratum_id)]


# ---------- 求解器：钉年规则 ----------

def _pin(sample_id, stratum_id, year):
    return PinInput(
        sample_id=sample_id, stratum_id=stratum_id, year=year, detail=f"样{sample_id}"
    )


def test_solver_pin_out_of_domain():
    sol = solve(
        [StratumInput(id=1, code="L1", intervals=[(100, 200)])],
        [],
        [],
        [_pin(1, 1, 250)],
    )
    assert not sol.feasible
    assert sol.contradiction.reason == "PIN_OUT_OF_DOMAIN"
    assert any(w.kind == "sample" for w in sol.contradiction.witness)


def test_solver_pin_conflict_in_same_group():
    strata = [
        StratumInput(id=1, code="L1", intervals=[(1, 100)]),
        StratumInput(id=2, code="L2", intervals=[(1, 100)]),
    ]
    sol = solve(
        strata, [CorrelationInput(1, 1, 2)], [], [_pin(1, 1, 10), _pin(2, 2, 11)]
    )
    assert not sol.feasible
    assert sol.contradiction.reason == "PIN_CONFLICT"
    assert sum(w.kind == "sample" for w in sol.contradiction.witness) == 2


def test_solver_pins_same_year_same_group_ok():
    strata = [
        StratumInput(id=1, code="L1", intervals=[(1, 100)]),
        StratumInput(id=2, code="L2", intervals=[(1, 100)]),
    ]
    sol = solve(
        strata, [CorrelationInput(1, 1, 2)], [], [_pin(1, 1, 10), _pin(2, 2, 10)]
    )
    assert sol.feasible
    assert sol.groups[0].feasible_years == [(10, 10)]


def test_solver_pinned_order_conflict_witness_includes_sample():
    strata = [
        StratumInput(id=1, code="L1", intervals=[(100, 200)]),
        StratumInput(id=2, code="L2", intervals=[(100, 200)]),
    ]
    sol = solve(
        strata,
        [],
        [SuperpositionInput(1, 1, 2)],
        [_pin(1, 1, 150), _pin(2, 2, 120)],
    )
    assert not sol.feasible
    assert sol.contradiction.reason == "ORDER_CONFLICT"
    assert any(w.kind == "sample" for w in sol.contradiction.witness)


def test_solver_pin_clips_downstream_group():
    strata = [
        StratumInput(id=1, code="L1", intervals=[(100, 200)]),
        StratumInput(id=2, code="L2", intervals=[(100, 200)]),
    ]
    sol = solve(strata, [], [SuperpositionInput(1, 1, 2)], [_pin(1, 1, 150)])
    assert sol.feasible
    by_id = {g.group_id: g for g in sol.groups}
    assert by_id[1].feasible_years == [(150, 150)]
    assert by_id[2].feasible_years == [(151, 200)]
    assert sol.earliest_assignment == {1: 150, 2: 151}
    assert sol.latest_assignment == {1: 150, 2: 200}


# ---------- 年表提交与查询 ----------

def test_chronology_union_view_and_complete_assignments(client):
    sid = _make_site(client)
    s1 = _add_stratum(client, sid, "T1", "L1", [(100, 200)])
    s2 = _add_stratum(client, sid, "T2", "L1", [(100, 200)])
    s3 = _add_stratum(client, sid, "T3", "L1", [(100, 200)])
    assert client.post(
        f"/sites/{sid}/superpositions", json={"lower_id": s1, "upper_id": s2}
    ).status_code == 201
    assert client.post(
        f"/sites/{sid}/superpositions", json={"lower_id": s2, "upper_id": s3}
    ).status_code == 201

    r = _put_chronology(client, sid, [100, 150], [(s1, 0), (s2, 5)])
    assert r.status_code == 200, r.text
    sol = r.json()["solution"]
    assert sol["feasible"] is True
    assert sol["feasible_start_years"] == [100, 150]
    assert sol["chronology"]["candidate_start_years"] == [100, 150]

    g1 = _group_of(sol, s1)
    g2 = _group_of(sol, s2)
    g3 = _group_of(sol, s3)
    groups = {g["group_id"]: g for g in sol["groups"]}
    # 各可行始年下可行年代段的并集，保留空档
    assert groups[g1]["feasible_years"] == [[100, 100], [150, 150]]
    assert groups[g2]["feasible_years"] == [[105, 105], [155, 155]]
    assert groups[g3]["feasible_years"] == [[106, 200]]
    # 最早/最晚赋值各自是一套完整解（最小/最大可行始年下）
    assert sol["earliest_assignment"] == {str(g1): 100, str(g2): 105, str(g3): 106}
    assert sol["latest_assignment"] == {str(g1): 150, str(g2): 155, str(g3): 200}
    for assignment in (sol["earliest_assignment"], sol["latest_assignment"]):
        assert assignment[str(g1)] < assignment[str(g2)] < assignment[str(g3)]

    # 按层位查询同样给出并集
    view = client.get(f"/strata/{s1}/correlation-view").json()
    assert view["feasible_years"] == [[100, 100], [150, 150]]
    assert view["feasible_start_years"] == [100, 150]


def test_disconnected_strata_share_single_start_year(client):
    sid = _make_site(client)
    s1 = _add_stratum(client, sid, "T1", "L1", [(100, 110)])
    s2 = _add_stratum(client, sid, "T2", "L1", [(207, 215)])
    # 互不连通，也必须共用同一个始年：只有 110 同时满足两枚木样
    r = _put_chronology(client, sid, [100, 101, 110], [(s1, 0), (s2, 105)])
    assert r.status_code == 200, r.text
    sol = r.json()["solution"]
    assert sol["feasible_start_years"] == [110]
    g1 = _group_of(sol, s1)
    g2 = _group_of(sol, s2)
    assert g1 != g2
    groups = {g["group_id"]: g for g in sol["groups"]}
    assert groups[g1]["feasible_years"] == [[110, 110]]
    assert groups[g2]["feasible_years"] == [[215, 215]]


def test_chronology_replaced_wholesale(client):
    sid = _make_site(client)
    s1 = _add_stratum(client, sid, "T1", "L1", [(100, 400)])
    s2 = _add_stratum(client, sid, "T2", "L1", [(100, 400)])
    assert _put_chronology(client, sid, [100, 150], [(s1, 0), (s2, 10)]).status_code == 200

    # 再提交整份替换：旧候选与旧木样全部失效
    r = _put_chronology(client, sid, [300], [(s1, 0), (s2, 5)])
    assert r.status_code == 200, r.text
    rec = client.get(f"/sites/{sid}/chronology").json()
    assert rec["candidate_start_years"] == [300]
    assert {s["offset_years"] for s in rec["samples"]} == {0, 5}
    sol = client.get(f"/sites/{sid}/solution").json()
    assert sol["feasible_start_years"] == [300]


def test_chronology_validation(client):
    sid = _make_site(client)
    s1 = _add_stratum(client, sid, "T1", "L1", [(100, 200)])
    s2 = _add_stratum(client, sid, "T2", "L1", [(100, 200)])
    # 候选始年：0 个 / 超过 20 个 / 有重复
    assert _put_chronology(client, sid, [], [(s1, 0), (s2, 1)]).status_code == 422
    assert (
        _put_chronology(client, sid, list(range(100, 121)), [(s1, 0), (s2, 1)]).status_code
        == 422
    )
    assert _put_chronology(client, sid, [100, 100], [(s1, 0), (s2, 1)]).status_code == 422
    # 木样：不足两个层位 / 同一层位重复
    assert _put_chronology(client, sid, [100], [(s1, 0)]).status_code == 422
    assert _put_chronology(client, sid, [100], [(s1, 0), (s1, 1)]).status_code == 422
    # 木样层位不存在 / 属于其他遗址
    assert _put_chronology(client, sid, [100], [(s1, 0), (9999, 1)]).status_code == 404
    other = _make_site(client, "另一遗址")
    s3 = _add_stratum(client, other, "T1", "L1", [(100, 200)])
    assert _put_chronology(client, sid, [100], [(s1, 0), (s3, 1)]).status_code == 404
    # 以上全部失败，年表未建立
    assert client.get(f"/sites/{sid}/chronology").status_code == 404
    # 边界：恰好 1 个候选、恰好 20 个候选都可接受
    assert _put_chronology(client, sid, [100], [(s1, 0), (s2, 1)]).status_code == 200
    assert (
        _put_chronology(client, sid, list(range(100, 120)), [(s1, 0), (s2, 1)]).status_code
        == 200
    )


# ---------- 409：全部候选失效 ----------

def test_infeasible_chronology_rejected_and_nothing_changes(client):
    sid = _make_site(client)
    s1 = _add_stratum(client, sid, "T1", "L1", [(100, 110)])
    s2 = _add_stratum(client, sid, "T2", "L1", [(100, 110)])
    assert client.post(
        f"/sites/{sid}/superpositions", json={"lower_id": s1, "upper_id": s2}
    ).status_code == 201
    version_before = client.get(f"/sites/{sid}/solution").json()["version"]

    # 两枚木样把严格叠压的两层钉到同一年：所有候选都不可行
    r = _put_chronology(client, sid, [100, 105], [(s1, 0), (s2, 0)])
    assert r.status_code == 409
    body = r.json()
    assert body["contradiction"]["reason"] == "ORDER_CONFLICT"
    assert any(w["kind"] == "sample" for w in body["contradiction"]["witness"])
    assert {f["start_year"] for f in body["candidate_failures"]} == {100, 105}
    assert body["previous_solution"]["feasible"] is True
    # 年表、版本与年代结论照旧
    assert client.get(f"/sites/{sid}/chronology").status_code == 404
    sol = client.get(f"/sites/{sid}/solution").json()
    assert sol["version"] == version_before
    assert sol["feasible_start_years"] is None
    assert sol["chronology"] is None


def test_failed_replacement_keeps_old_chronology(client):
    sid = _make_site(client)
    s1 = _add_stratum(client, sid, "T1", "L1", [(100, 200)])
    s2 = _add_stratum(client, sid, "T2", "L1", [(100, 200)])
    assert client.post(
        f"/sites/{sid}/superpositions", json={"lower_id": s1, "upper_id": s2}
    ).status_code == 201
    assert _put_chronology(client, sid, [100], [(s1, 0), (s2, 5)]).status_code == 200
    version_before = client.get(f"/sites/{sid}/solution").json()["version"]

    # 换表使全部候选失效：409，原年表照旧
    r = _put_chronology(client, sid, [100], [(s1, 0), (s2, 0)])
    assert r.status_code == 409
    rec = client.get(f"/sites/{sid}/chronology").json()
    assert {s["offset_years"] for s in rec["samples"]} == {0, 5}
    sol = client.get(f"/sites/{sid}/solution").json()
    assert sol["feasible_start_years"] == [100]
    assert sol["version"] == version_before


def test_correlation_killing_all_candidates_rejected(client):
    sid = _make_site(client)
    s1 = _add_stratum(client, sid, "T1", "L1", [(100, 110)])
    s2 = _add_stratum(client, sid, "T2", "L1", [(100, 210)])
    assert _put_chronology(client, sid, [100], [(s1, 0), (s2, 50)]).status_code == 200
    version_before = client.get(f"/sites/{sid}/solution").json()["version"]

    # 对接后两层同组，却被钉到 100 与 150 两个年份：全部候选失效
    r = client.post(
        f"/sites/{sid}/correlations",
        json={"stratum_a_id": s1, "stratum_b_id": s2},
    )
    assert r.status_code == 409
    assert r.json()["contradiction"]["reason"] == "PIN_CONFLICT"
    # 对接未入库，年表与结论照旧
    assert client.get(f"/sites/{sid}/correlations").json() == []
    sol = client.get(f"/sites/{sid}/solution").json()
    assert sol["feasible_start_years"] == [100]
    assert sol["version"] == version_before


def test_superposition_killing_all_candidates_rejected(client):
    sid = _make_site(client)
    s1 = _add_stratum(client, sid, "T1", "L1", [(100, 200)])
    s2 = _add_stratum(client, sid, "T2", "L1", [(100, 200)])
    # 木样把 s1 钉得比 s2 晚 10 年；再登记 s1 在下、s2 在上必然全部失效
    assert _put_chronology(client, sid, [100, 150], [(s1, 10), (s2, 0)]).status_code == 200

    r = client.post(
        f"/sites/{sid}/superpositions", json={"lower_id": s1, "upper_id": s2}
    )
    assert r.status_code == 409
    assert client.get(f"/sites/{sid}/superpositions").json() == []
    assert client.get(f"/sites/{sid}/solution").json()["feasible_start_years"] == [100, 150]


# ---------- 部分失效与撤销恢复 ----------

def test_superposition_killing_some_candidates_and_undo_restores(client):
    sid = _make_site(client)
    s1 = _add_stratum(client, sid, "T1", "L1", [(100, 200)])
    s2 = _add_stratum(client, sid, "T2", "L1", [(100, 200)])
    s3 = _add_stratum(client, sid, "T3", "L1", [(105, 105)])  # 钉死在 105 年
    assert _put_chronology(client, sid, [100, 150], [(s1, 0), (s2, 10)]).status_code == 200
    assert client.get(f"/sites/{sid}/solution").json()["feasible_start_years"] == [100, 150]

    # s3 压在 s1 之下：S=100（s1=100 < 106）失效，S=150 仍可行
    r = client.post(
        f"/sites/{sid}/superpositions", json={"lower_id": s3, "upper_id": s1}
    )
    assert r.status_code == 201, r.text
    assert r.json()["solution"]["feasible_start_years"] == [150]

    # 撤销叠压后，S=100 重新符合条件并回到结果中
    sup_id = r.json()["record"]["id"]
    r = client.delete(f"/superpositions/{sup_id}")
    assert r.status_code == 200
    assert r.json()["solution"]["feasible_start_years"] == [100, 150]


def test_correlation_killing_some_candidates_and_undo_restores(client):
    sid = _make_site(client)
    s1 = _add_stratum(client, sid, "T1", "L1", [(100, 200)])
    s2 = _add_stratum(client, sid, "T2", "L1", [(100, 200)])
    s3 = _add_stratum(client, sid, "T3", "L1", [(120, 260)])
    assert _put_chronology(client, sid, [100, 150], [(s1, 0), (s2, 10)]).status_code == 200

    # 对接 s1 与 s3：组域 [120,200]，S=100 把 s1 钉在 100（域外）→ 只剩 S=150
    r = client.post(
        f"/sites/{sid}/correlations",
        json={"stratum_a_id": s1, "stratum_b_id": s3},
    )
    assert r.status_code == 201, r.text
    assert r.json()["solution"]["feasible_start_years"] == [150]

    corr_id = r.json()["record"]["id"]
    r = client.delete(f"/correlations/{corr_id}")
    assert r.status_code == 200
    assert r.json()["solution"]["feasible_start_years"] == [100, 150]


# ---------- 持久化与兼容性 ----------

def test_chronology_persisted_across_restart(tmp_path):
    db_path = str(tmp_path / "persist.db")
    with TestClient(create_app(db_path=db_path)) as c1:
        sid = _make_site(c1)
        s1 = _add_stratum(c1, sid, "T1", "L1", [(100, 200)])
        s2 = _add_stratum(c1, sid, "T2", "L1", [(100, 200)])
        c1.post(f"/sites/{sid}/superpositions", json={"lower_id": s1, "upper_id": s2})
        assert _put_chronology(c1, sid, [100, 150], [(s1, 0), (s2, 5)]).status_code == 200
        before = c1.get(f"/sites/{sid}/solution").json()

    # 重启：同一份已提交的年表与地层记录，求解结果原样恢复
    with TestClient(create_app(db_path=db_path)) as c2:
        after = c2.get(f"/sites/{sid}/solution").json()
        assert after == before
        rec = c2.get(f"/sites/{sid}/chronology").json()
        assert rec["candidate_start_years"] == [100, 150]


def test_site_without_chronology_keeps_original_behavior(client):
    sid = _make_site(client)
    s1 = _add_stratum(client, sid, "T1", "L1", [(100, 200)])
    sol = client.get(f"/sites/{sid}/solution").json()
    assert sol["feasible"] is True
    assert sol["chronology"] is None
    assert sol["feasible_start_years"] is None
    view = client.get(f"/strata/{s1}/correlation-view").json()
    assert view["feasible_start_years"] is None
    assert view["feasible_years"] == [[100, 200]]


def test_chronology_endpoints_in_openapi(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert "/sites/{site_id}/chronology" in paths
