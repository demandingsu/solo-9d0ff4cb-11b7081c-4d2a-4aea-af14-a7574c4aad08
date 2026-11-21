#!/usr/bin/env python3
"""端到端演示：重复人名、确认生效、确认冲突、并发冲突。

用法：
    python3 scripts/demo.py            # 默认打 http://127.0.0.1:8000
    BASE_URL=http://localhost:8000 python3 scripts/demo.py

对应 /docs 中的示例：POST /pages 的「重复人名」「确认冲突」两个请求体，
以及 POST /annotations/{id}/confirm 的两个确认体。
"""

from __future__ import annotations

import os
import threading

import httpx

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8000")

DUP_PAGE = {
    "page_ref": "卷一·頁三",
    "old_text": "張三還書，張三攜酒。",
    "new_text": "張三還書，張三張三攜酒。",
    "annotations": [
        {"name": "張三", "start": 0, "end": 2},
        {"name": "張三", "start": 5, "end": 7},
    ],
}
SWAP_PAGE = {
    "page_ref": "卷二·頁七",
    "old_text": "張三和李四",
    "new_text": "李四和張三",
    "annotations": [
        {"name": "張三", "start": 0, "end": 2},
        {"name": "李四", "start": 3, "end": 5},
    ],
}
# 校订稿新添人名（旧稿没有对应标注），用于补录演示
SUP_PAGE = {
    "page_ref": "卷三·頁九",
    "old_text": "王五來訪，趙六同行。",
    "new_text": "王五來訪，趙六同行，王五再至。",
    "annotations": [],
}


def show_ann(a):
    if a["status"] == "pending":
        return (f"    #{a['id']} 「{a['name']}」旧[{a['old_start']},{a['old_end']}) "
                f"→ 待核，候选新区间 {a['candidates']}")
    return (f"    #{a['id']} 「{a['name']}」旧[{a['old_start']},{a['old_end']}) "
            f"→ {a['status']} 新[{a['new_start']},{a['new_end']}) "
            f"「{a['new_excerpt']}」")


def show_page(title, page):
    print(f"  {title}（页 {page['id']}「{page['page_ref']}」，"
          f"插删总数 {page['edit_ops']}，版本 {page['version']}，"
          f"可用 {page['usable_count']} / 待核 {page['pending_count']}）")
    for a in page["annotations"]:
        print(show_ann(a))


def main() -> None:
    http = httpx.Client(base_url=BASE, timeout=15)
    print(f"== 0. 服务自检 {BASE}")
    r = http.get("/")
    r.raise_for_status()
    print(f"  {r.json()}")

    print("\n== 1. 重复人名：建页，一处可用、一处待核")
    page = http.post("/pages", json=DUP_PAGE).json()
    show_page("建页结果", page)
    pending = [a for a in page["annotations"] if a["status"] == "pending"][0]

    print("\n== 2. 检索库视图（命中 + 待核）")
    idx = http.get(f"/pages/{page['id']}/index").json()
    for it in idx["items"]:
        src = "、".join(f"{s['source_type']}#{s['source_id']}"
                        for s in it["sources"])
        print(f"    命中 #{it['hit_id']} 「{it['name']}」新[{it['start']},{it['end']}) "
              f"「{it['excerpt']}」来源：{src}")
    print(f"    待核 {len(idx['pending'])} 条不进入检索库")

    print("\n== 3. 编辑确认待核标注：張三 旧[5,7) → 新[7,9)")
    r = http.post(f"/annotations/{pending['id']}/confirm",
                  json={"new_start": 7, "new_end": 9})
    print(f"  HTTP {r.status_code}")
    show_page("确认后", r.json())

    print("\n== 4. 确认冲突：换位页，两条确认互相排斥")
    page2 = http.post("/pages", json=SWAP_PAGE).json()
    show_page("建页结果", page2)
    zs = next(a for a in page2["annotations"] if a["name"] == "張三")
    ls = next(a for a in page2["annotations"] if a["name"] == "李四")
    r = http.post(f"/annotations/{zs['id']}/confirm",
                  json={"new_start": 3, "new_end": 5})
    print(f"  確認張三→[3,5)：HTTP {r.status_code}")
    r = http.post(f"/annotations/{ls['id']}/confirm",
                  json={"new_start": 0, "new_end": 2})
    print(f"  再确认李四→[0,2)：HTTP {r.status_code} —— {r.json()['detail']}")
    after = http.get(f"/pages/{page2['id']}").json()
    show_page("冲突退回后（当前结果保留）", after)

    print("\n== 5. 补录新添人名并合并为命中")
    page4 = http.post("/pages", json=SUP_PAGE).json()
    show_page("建页结果", page4)
    first = http.post(f"/pages/{page4['id']}/supplements",
                      json={"name": "王五", "new_start": 0, "new_end": 2})
    print(f"  补录 王五 [0,2)：HTTP {first.status_code}，补录 "
          f"#{first.json()['id']} → 命中 #{first.json()['hit_id']}")
    r2 = http.post(f"/pages/{page4['id']}/supplements",
                   json={"name": "王五", "new_start": 0, "new_end": 2})
    print(f"  重复补录同一处：HTTP {r2.status_code}，仍为补录 #{r2.json()['id']}")
    r = http.post(f"/pages/{page4['id']}/supplements",
                  json={"name": "王五", "new_start": 10, "new_end": 12})
    sup = r.json()
    print(f"  补录 王五 [10,12)：命中 #{sup['hit_id']}")
    hits = http.get(f"/pages/{page4['id']}/hits").json()["items"]
    for h in hits:
        src = "、".join(f"{s['source_type']}#{s['source_id']}"
                        for s in h["sources"])
        print(f"    命中 #{h['hit_id']} 「{h['name']}」[{h['start']},{h['end']}) "
              f"来源：{src}")

    print("\n== 6. 更正补录：两种情形")
    # 情形一：新位置已有同名命中 → 409，告知编号，原供数保留
    r = http.patch(f"/supplements/{sup['id']}",
                   json={"new_start": 0, "new_end": 2})
    print(f"  更正到已被占用的 [0,2)：HTTP {r.status_code} —— "
          f"{r.json().get('detail')}")
    # 情形二：新位置无同名命中且原位只剩这条补录 → 编号随补录移动
    r = http.patch(f"/supplements/{sup['id']}",
                   json={"new_start": 11, "new_end": 13})
    moved = r.json()
    print(f"  更正到空位 [11,13)：HTTP {r.status_code}，编号仍为 "
          f"#{moved['hit_id']}")
    hit = http.get(f"/hits/{moved['hit_id']}").json()
    print(f"  按编号 #{hit['hit_id']} 回查：「{hit['name']}」"
          f"[{hit['start']},{hit['end']}) 来源 "
          f"{[(s['source_type'], s['source_id']) for s in hit['sources']]}")

    print("\n== 7. 并发冲突：两个客户端同时确认同一条待核标注")
    page3 = http.post("/pages", json=DUP_PAGE).json()
    target = [a for a in page3["annotations"] if a["status"] == "pending"][0]
    codes = []

    def hit(ns):
        with httpx.Client(base_url=BASE, timeout=15) as h:
            codes.append(h.post(f"/annotations/{target['id']}/confirm",
                                json={"new_start": ns, "new_end": ns + 2}
                                ).status_code)

    threads = [threading.Thread(target=hit, args=(7,)),
               threading.Thread(target=hit, args=(5,))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print(f"  两个并发确认（[7,9) 与 [5,7)）的状态码：{sorted(codes)}")
    show_page("并发之后", http.get(f"/pages/{page3['id']}").json())
    print("\n演示完成。")


if __name__ == "__main__":
    main()
