"""肥料母液分罐回归测试：奇环检测（奇偶状态 BFS）。

覆盖核心缺陷：旧实现「删 BFS 着色冲突边后取端点普通最短路」会被**偶环捷径**
欺骗——若每条冲突边删除后的端点最短路都与该边闭合成偶环，旧代码逐条跳过，
导致含奇环的不相容图被误判为可分配，甚至把不相容肥料放进同一罐。

运行（无需启动服务，纯模块级）：

    python -m unittest test_fertilizer_split -v
"""

import unittest

from fertilizer_split import (CONFLICT, EMPTY, ERROR, OK, OVERFLOW,
                              _build_graph, _canonical_ring, _shortest_odd_ring,
                              _split_components, solve)


def solve_graph(edges, n, cap=1_000_000, amounts=None, names=None):
    """以无向边列表快速构造 solve 输入；顶点编号 1..n，默认每点用量 100。"""
    fertilizers = [
        {"id": i, "name": (names or {}).get(i, f"F{i}"),
         "amount": (amounts or {}).get(i, 100)}
        for i in range(1, n + 1)]
    pairs = [{"id": k, "a_id": a, "b_id": b}
             for k, (a, b) in enumerate(edges, 1)]
    return solve(cap, cap, fertilizers, pairs)


def ring_ids(result):
    return [node["id"] for node in result["conflict_ring"]]


class EvenShortcutMissTests(unittest.TestCase):
    """偶环捷径遮蔽更长奇环的漏报场景。"""

    # 奇环 1-2-3-4-5-1（5 环）+ 3-6-7-4（3 条边的奇长捷径）。
    # BFS 二着色的冲突边只有 (3,4) 与 (6,7)：
    #   删 (3,4) 后最短路 3-6-7-4（3 边），与 (3,4) 闭合成 4-偶环；
    #   删 (6,7) 后最短路 6-3-4-7（3 边），与 (6,7) 闭合成 4-偶环。
    # 旧算法逐条跳过 -> 误报可分配，且把不相容的 3、4 放进同一罐。
    EDGES = [(1, 2), (2, 3), (3, 4), (4, 5), (5, 1),
             (3, 6), (6, 7), (7, 4)]

    def test_conflict_detected(self):
        r = solve_graph(self.EDGES, 7)
        self.assertEqual(r["status"], CONFLICT)
        self.assertEqual(ring_ids(r), [1, 2, 3, 4, 5])

    def test_ring_is_odd_and_closed(self):
        r = solve_graph(self.EDGES, 7)
        ring = ring_ids(r)
        self.assertEqual(len(ring) % 2, 1)
        edge_pairs = {(min(a, b), max(a, b)) for a, b in self.EDGES}
        for i, x in enumerate(ring):
            y = ring[(i + 1) % len(ring)]
            self.assertIn((min(x, y), max(x, y)), edge_pairs)

    def test_conflict_component_consistent(self):
        r = solve_graph(self.EDGES, 7)
        idx = r["conflict_component"]
        block = next(c for c in r["components"] if c["index"] == idx)
        self.assertTrue(block["conflict"])
        self.assertTrue(set(ring_ids(r)) <= set(block["member_ids"]))
        # 图整体连通，只有一个分量
        self.assertEqual(len(r["components"]), 1)

    def test_ring_edge_marks_consistent(self):
        r = solve_graph(self.EDGES, 7)
        ring = set(ring_ids(r))
        marked = {(e["a_id"], e["b_id"]) for e in r["edges"] if e["in_ring"]}
        # 恰好环上的 5 条不相容关系被高亮；3 条偶环捷径边不高亮
        self.assertEqual(marked, {(1, 2), (2, 3), (3, 4), (4, 5), (1, 5)})
        for e in r["edges"]:
            self.assertEqual(
                e["in_ring"],
                e["a_id"] in ring and e["b_id"] in ring
                and (e["a_id"], e["b_id"]) in marked)

    def test_no_decision_when_conflict(self):
        r = solve_graph(self.EDGES, 7)
        self.assertIsNone(r["decision"])


class LongerOddRingTests(unittest.TestCase):
    """更长奇环 + 多分量 / 同长决胜。"""

    def test_triangle_shorter_than_seven_cycle_across_components(self):
        # 分量一：1..7 的 7 环；分量二：8-9-10 三角形 -> 最短取三角形
        edges = [(1, 2), (2, 3), (3, 4), (4, 5), (5, 6), (6, 7), (7, 1),
                 (8, 9), (9, 10), (8, 10)]
        r = solve_graph(edges, 10)
        self.assertEqual(r["status"], CONFLICT)
        self.assertEqual(ring_ids(r), [8, 9, 10])
        self.assertEqual(r["conflict_component"], 2)
        marked = {(e["a_id"], e["b_id"]) for e in r["edges"] if e["in_ring"]}
        self.assertEqual(marked, {(8, 9), (9, 10), (8, 10)})
        comp_flags = sorted((c["index"], c["conflict"]) for c in r["components"])
        self.assertEqual(comp_flags, [(1, True), (2, True)])

    def test_equal_length_tie_break_canonical(self):
        # 两个不相交三角形 (2,3,4) 与 (5,6,7)，孤立点 1 自成分量 #1。
        # 同长 3 环按规范化 ID 序列决胜，取 (2,3,4) 所在的分量 #2。
        edges = [(2, 3), (3, 4), (2, 4),
                 (5, 6), (6, 7), (5, 7)]
        r = solve_graph(edges, 7)
        self.assertEqual(ring_ids(r), [2, 3, 4])
        self.assertEqual(r["conflict_component"], 2)

    def test_bare_seven_cycle(self):
        edges = [(i, i + 1) for i in range(1, 7)] + [(7, 1)]
        r = solve_graph(edges, 7)
        self.assertEqual(r["status"], CONFLICT)
        self.assertEqual(ring_ids(r), [1, 2, 3, 4, 5, 6, 7])
        marked = {(e["a_id"], e["b_id"]) for e in r["edges"] if e["in_ring"]}
        self.assertEqual(len(marked), 7)

    def test_k4_odd_girth_is_triangle(self):
        edges = [(a, b) for a in (1, 2, 3, 4) for b in range(a + 1, 5)]
        r = solve_graph(edges, 4)
        self.assertEqual(ring_ids(r), [1, 2, 3])
        marked = {(e["a_id"], e["b_id"]) for e in r["edges"] if e["in_ring"]}
        self.assertEqual(marked, {(1, 2), (2, 3), (1, 3)})


class ShortestOddRingDirectTests(unittest.TestCase):
    """直接对图算法做偶环捷径 / 决胜的单元断言。"""

    def _adj(self, edges, n):
        adj, _ = _build_graph([(i, 1) for i in range(1, n + 1)],
                              [(a, b, k) for k, (a, b) in enumerate(edges, 1)])
        return adj

    def test_even_shortcut_graph_girth_five(self):
        edges = [(1, 2), (2, 3), (3, 4), (4, 5), (5, 1),
                 (3, 6), (6, 7), (7, 4)]
        adj = self._adj(edges, 7)
        members, color, is_conflict = _split_components(adj)[0]
        self.assertTrue(is_conflict)
        length, canonical = _shortest_odd_ring(adj, members)
        self.assertEqual(length, 5)
        self.assertEqual(canonical, (1, 2, 3, 4, 5))

    def test_bipartite_component_returns_none(self):
        edges = [(1, 2), (2, 3), (3, 4), (4, 1)]  # 4 偶环
        adj = self._adj(edges, 4)
        members, _color, is_conflict = _split_components(adj)[0]
        self.assertFalse(is_conflict)
        self.assertIsNone(_shortest_odd_ring(adj, members))

    def test_canonical_ring_rotation_and_reflection(self):
        # 同一 5 环的旋转 / 反向写法规整结果一致
        self.assertEqual(_canonical_ring([3, 4, 5, 1, 2]), (1, 2, 3, 4, 5))
        self.assertEqual(_canonical_ring([1, 5, 4, 3, 2]), (1, 2, 3, 4, 5))


class NonConflictStatusTests(unittest.TestCase):
    """其他状态结果保持不变。"""

    def test_even_cycle_ok_and_separates_incompatibles(self):
        edges = [(1, 2), (2, 3), (3, 4), (4, 1)]  # 4 偶环
        r = solve_graph(edges, 4)
        self.assertEqual(r["status"], OK)
        a = set(r["decision"]["chosen"]["a_ids"])
        b = set(r["decision"]["chosen"]["b_ids"])
        for x, y in edges:
            self.assertFalse({x, y} <= a)
            self.assertFalse({x, y} <= b)
        self.assertIsNone(r["conflict_ring"])
        self.assertFalse(any(e["in_ring"] for e in r["edges"]))

    def test_overflow_unchanged(self):
        # 链 1-2-3：两侧体积 {1,3}=200 / {2}=100，罐容 150 -> 最小总超额 50
        r = solve_graph([(1, 2), (2, 3)], 3, cap=150)
        self.assertEqual(r["status"], OVERFLOW)
        chosen = r["decision"]["chosen"]
        self.assertEqual(chosen["excess_total"], 50)
        self.assertEqual(chosen["used_a"] + chosen["used_b"], 300)
        self.assertTrue(chosen["tank_a"] and chosen["tank_b"])

    def test_zero_amount_breaks_odd_ring(self):
        edges = [(1, 2), (2, 3), (3, 4), (4, 5), (5, 1)]
        r = solve_graph(edges, 5, amounts={3: 0})
        self.assertEqual(r["status"], OK)
        self.assertEqual({n["id"] for n in r["active"]}, {1, 2, 4, 5})

    def test_empty(self):
        r = solve_graph([], 3, amounts={1: 0, 2: 0, 3: 0})
        self.assertEqual(r["status"], EMPTY)

    def test_too_many_components_error_before_decide(self):
        # 21 个互相无边的活跃肥料 -> 21 个二分分量，超 2^20 枚举上限
        r = solve_graph([], 21)
        self.assertEqual(r["status"], ERROR)
        self.assertIn("20", r["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
