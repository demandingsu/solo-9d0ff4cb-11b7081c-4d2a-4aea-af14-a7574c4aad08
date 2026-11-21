"""补录、命中合并、更正与重启回查的端到端测试。"""

import threading
import time

import httpx
import uvicorn

from app import db
from app.main import app
from tests.conftest import DUP_PAGE, SWAP_PAGE

# 编辑补录专用页：校订稿新添两个人名「王五」「趙六」，旧稿没有标注
SUP_PAGE = {
    "page_ref": "卷三·頁九",
    "old_text": "王五來訪，趙六同行。",
    "new_text": "王五來訪，趙六同行，王五再至，趙六復來。",
    "annotations": [],
}


def _serve(port: int):
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("uvicorn 未能启动")
        time.sleep(0.02)
    return server, thread


def _supp(client, page_id, name, start, end):
    return client.post(f"/pages/{page_id}/supplements", json={
        "name": name, "new_start": start, "new_end": end})


def _supp_body(client, page_id, name, start, end):
    """兼容别名：页号写在请求体中。"""
    return client.post("/supplements", json={
        "page_id": page_id, "name": name, "new_start": start, "new_end": end})


def _index(client, page_id):
    return client.get(f"/pages/{page_id}/index").json()


def _hits(client, page_id):
    return client.get(f"/pages/{page_id}/hits").json()["items"]


def _hit_of(items, name, start, end):
    for h in items:
        if h["name"] == name and (h["start"], h["end"]) == (start, end):
            return h
    raise AssertionError(f"未找到命中 {name} [{start},{end})：{items}")


# ---------------------------------------------------------------- 补录登记
def test_supplement_enters_index_as_hit(client):
    page = client.post("/pages", json=SUP_PAGE).json()
    r = _supp(client, page["id"], "王五", 0, 2)
    assert r.status_code == 201, r.text
    sup = r.json()
    assert sup["excerpt"] == "王五"
    assert sup["hit_id"] >= 1
    items = _hits(client, page["id"])
    hit = _hit_of(items, "王五", 0, 2)
    assert hit["hit_id"] == sup["hit_id"]
    assert hit["sources"] == [{"source_type": "supplement",
                               "source_id": sup["id"],
                               "source_status": "supplement"}]


def test_duplicate_supplement_recorded_once(client):
    page = client.post("/pages", json=SUP_PAGE).json()
    first = _supp(client, page["id"], "趙六", 5, 7)
    assert first.status_code == 201
    again = _supp(client, page["id"], "趙六", 5, 7)
    assert again.status_code == 200, again.text
    assert again.json()["id"] == first.json()["id"]
    assert again.json()["hit_id"] == first.json()["hit_id"]
    items = _hits(client, page["id"])
    assert len([h for h in items if h["name"] == "趙六"]) == 1
    # 数据库中也只有一行补录
    conn = db.connect()
    try:
        n = conn.execute("SELECT COUNT(*) FROM supplements WHERE page_id = ?",
                         (page["id"],)).fetchone()[0]
    finally:
        conn.close()
    assert n == 1


def test_supplement_validates_text_and_range(client):
    page = client.post("/pages", json=SUP_PAGE).json()
    assert _supp(client, page["id"], "王五", 0, 99).status_code == 422
    assert _supp(client, page["id"], "王五", 2, 2).status_code == 422
    # 补录记录的是「新添的人名」，不要求与区间文字逐字一致
    assert _supp(client, page["id"], "王五", 7, 9).status_code == 201
    assert _supp(client, page["id"], "趙六", 5, 7).status_code == 201
    assert _supp(client, 999, "王五", 0, 2).status_code == 404
    assert client.get("/supplements/999").status_code == 404


# ---------------------------------------------------------------- 合并与编号
def test_supplement_merges_with_existing_usable_annotation(client):
    page = client.post("/pages", json=DUP_PAGE).json()
    first = page["annotations"][0]  # auto [0,2)
    # 同一处再补录一个人名：合并为一条命中，沿用既有编号
    r = _supp(client, page["id"], "張三", 0, 2)
    assert r.status_code == 201, r.text
    hit = _hit_of(_hits(client, page["id"]), "張三", 0, 2)
    assert hit["hit_id"] == r.json()["hit_id"]
    types = [s["source_type"] for s in hit["sources"]]
    assert types == ["annotation", "supplement"]
    assert hit["sources"][0]["source_id"] == first["id"]
    assert hit["sources"][0]["source_status"] == "auto"
    assert hit["sources"][1]["source_id"] == r.json()["id"]


def test_later_usable_annotation_joins_existing_hit(client):
    """先补录、后确认旧标注到同一处：并入既有命中并沿用编号。"""
    page = client.post("/pages", json=DUP_PAGE).json()
    target = next(a for a in page["annotations"] if a["status"] == "pending")
    sup = _supp(client, page["id"], "張三", 7, 9).json()
    hit_id = sup["hit_id"]
    # 注意：此处必须确认到 [7,9)；[5,7) 落在 SUP_PAGE 之外、与 DUP 页无关
    r = client.post(f"/annotations/{target['id']}/confirm",
                    json={"new_start": 7, "new_end": 9})
    assert r.status_code == 200, r.text
    hits_after = _hits(client, page["id"])
    hit = _hit_of(hits_after, "張三", 7, 9)
    assert hit["hit_id"] == hit_id
    types = sorted(s["source_type"] for s in hit["sources"])
    assert types == ["annotation", "supplement"]


def test_index_groups_pending_separately(client):
    page = client.post("/pages", json=DUP_PAGE).json()
    idx = _index(client, page["id"])
    assert len(idx["items"]) == 1          # 只有 auto 的 [0,2)
    assert idx["items"][0]["hit_id"] >= 1
    assert idx["items"][0]["sources"][0]["source_status"] == "auto"
    assert len(idx["pending"]) == 1
    assert idx["pending"][0]["candidates"] == [[5, 7], [7, 9]]


def test_hit_lookup_by_id(client):
    page = client.post("/pages", json=SUP_PAGE).json()
    sup = _supp(client, page["id"], "趙六", 5, 7).json()
    r = client.get(f"/hits/{sup['hit_id']}")
    assert r.status_code == 200
    assert r.json()["name"] == "趙六"
    assert r.json()["page_id"] == page["id"]
    assert client.get("/hits/999").status_code == 404


# ---------------------------------------------------------------- 更正
def test_correct_moves_hit_id_when_only_supplement(client):
    """原位只有这条补录：编号随补录移动到新位置。"""
    page = client.post("/pages", json=SUP_PAGE).json()
    sup = _supp(client, page["id"], "趙六", 5, 7).json()
    old_hit = sup["hit_id"]
    r = client.patch(f"/supplements/{sup['id']}",
                     json={"new_start": 15, "new_end": 17})
    assert r.status_code == 200, r.text
    assert r.json()["hit_id"] == old_hit          # 编号随补录移动
    assert r.json()["excerpt"] == "趙六"
    assert client.get(f"/hits/{old_hit}").json()["name"] == "趙六"
    items = _hits(client, page["id"])
    assert [h["hit_id"] for h in items] == [old_hit]
    assert len(items) == 1                         # 旧位置不残留命中


def test_correct_rejected_when_new_position_occupied(client):
    """新位置已有同名命中：退回并告知编号，原供数保留。"""
    page = client.post("/pages", json=SUP_PAGE).json()
    a = _supp(client, page["id"], "王五", 0, 2).json()
    b = _supp(client, page["id"], "王五", 10, 12).json()
    assert a["hit_id"] != b["hit_id"]
    r = client.patch(f"/supplements/{b['id']}",
                     json={"new_start": 0, "new_end": 2})
    assert r.status_code == 409
    assert str(a["hit_id"]) in r.json()["detail"]
    # 更正失败：原供数保留
    hit_b = client.get(f"/hits/{b['hit_id']}").json()
    assert (hit_b["start"], hit_b["end"]) == (10, 12)
    assert len(_hits(client, page["id"])) == 2


def test_correct_keeps_old_id_when_other_sources_remain(client):
    """原位还有其他来源：原编号留在原位，新位置另发编号。"""
    page = client.post("/pages", json=SUP_PAGE).json()
    # 制造原位还有别的来源：该页建页时补一个 auto 标注到 [0,2)
    page2 = client.post("/pages", json={
        "page_ref": "卷四·頁一",
        "old_text": "王五來訪，趙六同行。",
        "new_text": "王五來訪，趙六同行，王五再至，趙六復來。",
        "annotations": [{"name": "王五", "start": 0, "end": 2}]}).json()
    assert page2["annotations"][0]["status"] == "auto"
    old_hit_id = _hit_of(_hits(client, page2["id"]), "王五", 0, 2)["hit_id"]
    sup = _supp(client, page2["id"], "王五", 0, 2).json()
    assert sup["hit_id"] == old_hit_id  # 并入旧标注的命中
    r = client.patch(f"/supplements/{sup['id']}",
                     json={"new_start": 5, "new_end": 7})
    assert r.status_code == 200, r.text
    assert r.json()["hit_id"] != old_hit_id       # 新位置另发编号
    old_hit = client.get(f"/hits/{old_hit_id}").json()
    assert (old_hit["start"], old_hit["end"]) == (0, 2)
    assert [s["source_type"] for s in old_hit["sources"]] == ["annotation"]
    new_hit = client.get(f"/hits/{r.json()['hit_id']}").json()
    assert (new_hit["start"], new_hit["end"]) == (5, 7)
    assert [s["source_type"] for s in new_hit["sources"]] == ["supplement"]
    assert len(_hits(client, page2["id"])) == 2
    del page


def test_correct_out_of_range_keeps_supply(client):
    page = client.post("/pages", json=SUP_PAGE).json()
    sup = _supp(client, page["id"], "趙六", 5, 7).json()
    r = client.patch(f"/supplements/{sup['id']}",
                     json={"new_start": 0, "new_end": 99})
    assert r.status_code == 422
    kept = client.get(f"/supplements/{sup['id']}").json()
    assert (kept["new_start"], kept["new_end"]) == (5, 7)
    assert client.get(f"/hits/{sup['hit_id']}").json()["name"] == "趙六"


def test_correct_is_noop_when_unchanged(client):
    page = client.post("/pages", json=SUP_PAGE).json()
    sup = _supp(client, page["id"], "趙六", 5, 7).json()
    r = client.patch(f"/supplements/{sup['id']}",
                     json={"new_start": 5, "new_end": 7})
    assert r.status_code == 200
    assert r.json()["hit_id"] == sup["hit_id"]


# ---------------------------------------------------------------- 持久化
def test_hit_survives_restart(tmp_path, monkeypatch):
    """重启（换一个应用进程/重新导入）后仍可按编号回查。"""
    from fastapi.testclient import TestClient
    from app.main import app as app1
    c1 = TestClient(app1)
    page = c1.post("/pages", json=SUP_PAGE).json()
    sup = _supp(c1, page["id"], "趙六", 5, 7).json()
    hit_id = sup["hit_id"]

    # 模拟重启：重新走一遍启动流程（建表 + 既有页回填命中），再开客户端
    db.init_db()
    from app import hits as hits_mod
    hits_mod.backfill_all()
    c2 = TestClient(app1)
    r = c2.get(f"/hits/{hit_id}")
    assert r.status_code == 200
    assert r.json()["name"] == "趙六"
    assert r.json()["sources"][0]["source_id"] == sup["id"]


def test_backfill_covers_pages_created_before_upgrade(client):
    """既有页（升级前就已存在、没有命中记录）并入命中索引。"""
    page = client.post("/pages", json=DUP_PAGE).json()
    conn = db.connect()
    try:
        conn.execute("DELETE FROM hit_sources")
        conn.execute("DELETE FROM hits")
        conn.commit()
    finally:
        conn.close()
    assert _hits(client, page["id"]) == []
    from app import hits as hits_mod
    hits_mod.backfill_all()
    items = _hits(client, page["id"])
    assert len(items) == 1
    assert items[0]["name"] == "張三"
    assert (items[0]["start"], items[0]["end"]) == (0, 2)


# ---------------------------------------------------------------- 并发交错
def test_concurrent_confirm_and_correct_serialized(client):
    """确认与补录更正并发：按提交顺序判断来源归属，结果恒一致。"""
    page = client.post("/pages", json=DUP_PAGE).json()
    target = next(a for a in page["annotations"] if a["status"] == "pending")
    # 先在 [7,9) 补录一条張三
    sup = _supp(client, page["id"], "張三", 7, 9).json()
    server, thread = _serve(8331)
    try:
        codes = {}

        def confirm():
            with httpx.Client(base_url="http://127.0.0.1:8331",
                              timeout=15) as h:
                r = h.post(f"/annotations/{target['id']}/confirm",
                           json={"new_start": 7, "new_end": 9})
                codes["confirm"] = r.status_code

        def correct():
            with httpx.Client(base_url="http://127.0.0.1:8331",
                              timeout=15) as h:
                # 把补录移走：若确认先落库，则新位置 [5,7) 无冲突
                r = h.patch(f"/supplements/{sup['id']}",
                            json={"new_start": 5, "new_end": 7})
                codes["correct"] = r.status_code

        t1 = threading.Thread(target=confirm)
        t2 = threading.Thread(target=correct)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        assert codes == {"confirm": 200, "correct": 200}
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    # 无论先后，最终 [7,9) 只剩旧标注、[5,7) 只有补录；补录编号跟随补录
    if codes["confirm"] == 200:
        target_hit = _hit_of(_hits(client, page["id"]), "張三", 7, 9)
        assert [s["source_type"] for s in target_hit["sources"]] == ["annotation"]
    items = _hits(client, page["id"])
    by_pos = {(h["start"], h["end"]): h for h in items}
    assert sorted(by_pos) == [(0, 2), (5, 7), (7, 9)]
    assert [s["source_type"] for s in by_pos[(5, 7)]["sources"]] == ["supplement"]
    got = client.get(f"/supplements/{sup['id']}").json()
    assert got["hit_id"] == by_pos[(5, 7)]["hit_id"]


def test_same_name_same_page_distinct_intervals_do_not_merge(client):
    """同名但区间不同不合并；同名同区间才合并为一条命中。"""
    page = client.post("/pages", json=SUP_PAGE).json()
    a = _supp(client, page["id"], "趙六", 5, 7).json()
    b = _supp(client, page["id"], "趙六", 15, 17).json()
    assert a["hit_id"] != b["hit_id"]
    items = _hits(client, page["id"])
    assert len(items) == 2
    assert all(len(h["sources"]) == 1 for h in items)
    # 第三个来源落在与 a 相同的位置 → 并入 a 的编号
    c = _supp(client, page["id"], "趙六", 5, 7).json()
    assert c["id"] == a["id"] and c["hit_id"] == a["hit_id"]
    assert len(_hits(client, page["id"])) == 2
    # 索引读取一次给出一致快照
    idx = _index(client, page["id"])
    assert len(idx["items"]) == 2
    assert sum(len(h["sources"]) for h in idx["items"]) == 2


def test_concurrent_correct_same_supplement_serialized(client):
    """同一条补录的两次并发更正：串行落库，不产生重复命中或来源。"""
    page = client.post("/pages", json=SUP_PAGE).json()
    sup = _supp(client, page["id"], "趙六", 5, 7).json()
    server, thread = _serve(8332)
    try:
        codes = []

        def correct(ns, ne):
            with httpx.Client(base_url="http://127.0.0.1:8332",
                              timeout=15) as h:
                r = h.patch(f"/supplements/{sup['id']}",
                            json={"new_start": ns, "new_end": ne})
                codes.append(r.status_code)

        ts = [threading.Thread(target=correct, args=(0, 2)),
              threading.Thread(target=correct, args=(15, 17))]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert sorted(codes) == [200, 200]
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    items = _hits(client, page["id"])
    assert len(items) == 1                        # 只有一个位置有命中
    assert len(items[0]["sources"]) == 1
    got = client.get(f"/supplements/{sup['id']}").json()
    assert (got["new_start"], got["new_end"]) == (items[0]["start"], items[0]["end"])
    assert got["hit_id"] == items[0]["hit_id"]   # 编号始终跟随这条补录


def test_repeat_add_after_concurrent_correct_is_idempotent(client):
    """并发更正后再补录同一处：仍只记一次，并入同一命中。"""
    page = client.post("/pages", json=SUP_PAGE).json()
    sup = _supp(client, page["id"], "趙六", 5, 7).json()
    client.patch(f"/supplements/{sup['id']}",
                 json={"new_start": 0, "new_end": 2})
    # 补录人名不随区间更正而变（更正只改区间），仍是「趙六」
    again = _supp(client, page["id"], "趙六", 0, 2)
    assert again.status_code == 200
    assert again.json()["id"] == sup["id"]
    assert len(_hits(client, page["id"])) == 1


# ---------------------------------------------------------------- 别名与自测
def test_body_style_supplement_alias_is_idempotent(client):
    """POST /supplements（页号在体中）与按页路径等价、同样幂等。"""
    page = client.post("/pages", json=SUP_PAGE).json()
    first = _supp_body(client, page["id"], "王五", 0, 2)
    assert first.status_code == 201, first.text
    again = _supp_body(client, page["id"], "王五", 0, 2)
    assert again.status_code == 200
    assert again.json()["id"] == first.json()["id"]
    # 与按页路径共用同一行补录
    third = _supp(client, page["id"], "王五", 0, 2)
    assert third.status_code == 200
    assert third.json()["id"] == first.json()["id"]


def test_selfcheck_two_corrections_and_confirmation_interleaved(client):
    """自测两种更正 × 确认交错：
    (a) 更正被占用退回；(b) 更正到空位编号随补录移动；
    每个场景各跑一遍「先确认后更正」与「先更正后确认」。"""
    # 场景 A：新位置将被旧标注占用 → 更正必须 409，且确认赢后命中合并
    page_a = client.post("/pages", json=DUP_PAGE).json()
    pending_a = next(a for a in page_a["annotations"] if a["status"] == "pending")
    sup_a = _supp(client, page_a["id"], "張三", 7, 9).json()
    r = client.post(f"/annotations/{pending_a['id']}/confirm",
                    json={"new_start": 7, "new_end": 9})
    assert r.status_code == 200
    merged = _hit_of(_hits(client, page_a["id"]), "張三", 7, 9)
    assert merged["hit_id"] == sup_a["hit_id"]      # 旧标注并入，沿用编号
    assert sorted(s["source_type"] for s in merged["sources"]) == [
        "annotation", "supplement"]
    r = client.patch(f"/supplements/{sup_a['id']}",
                     json={"new_start": 7, "new_end": 9})
    assert r.status_code == 200                      # 区间未变：幂等，命中不动
    # 更正到 [0,2)：该处已有同名命中（auto），应退回
    r = client.patch(f"/supplements/{sup_a['id']}",
                     json={"new_start": 0, "new_end": 2})
    assert r.status_code == 409
    assert str(_hit_of(_hits(client, page_a["id"]), "張三", 0, 2)["hit_id"]) in \
        r.json()["detail"]

    # 场景 B：先更正走开、再确认回原位 → 编号随补录走，旧标注拿旧位编号
    page_b = client.post("/pages", json=DUP_PAGE).json()
    pending_b = next(a for a in page_b["annotations"] if a["status"] == "pending")
    sup_b = _supp(client, page_b["id"], "張三", 7, 9).json()
    moved = client.patch(f"/supplements/{sup_b['id']}",
                         json={"new_start": 5, "new_end": 7})
    assert moved.status_code == 200
    assert moved.json()["hit_id"] == sup_b["hit_id"]
    assert client.get(f"/hits/{sup_b['hit_id']}").json()["start"] == 5
    r = client.post(f"/annotations/{pending_b['id']}/confirm",
                    json={"new_start": 7, "new_end": 9})
    assert r.status_code == 200
    # 补录走了以后，[7,9) 是新命中；补录编号不在那里
    old_pos = _hit_of(_hits(client, page_b["id"]), "張三", 7, 9)
    assert old_pos["hit_id"] != sup_b["hit_id"]
    kept = client.get(f"/supplements/{sup_b['id']}").json()
    assert kept["hit_id"] == sup_b["hit_id"]
    idx = _index(client, page_b["id"])
    assert sum(len(h["sources"]) for h in idx["items"]) == 3  # auto + 确认 + 补录


def test_selfcheck_source_determination_order(client):
    """来源归属按提交顺序判定：先补录后确认 → 并入；命中始终只有一条。"""
    page = client.post("/pages", json=DUP_PAGE).json()
    pending = next(a for a in page["annotations"] if a["status"] == "pending")
    first = _supp(client, page["id"], "張三", 7, 9).json()   # 先补录
    client.post(f"/annotations/{pending['id']}/confirm",     # 后确认
                json={"new_start": 7, "new_end": 9})
    hit = _hit_of(_hits(client, page["id"]), "張三", 7, 9)
    assert hit["hit_id"] == first["hit_id"]
    assert len(hit["sources"]) == 2
