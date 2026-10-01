"""肥料母液 A/B 分罐求解核心：连通分量二着色 + 奇偶状态 BFS 奇环 + 全局翻转组合枚举。

问题模型
--------
- 每份配方设置 A / B 两个母液罐的容量（mL，正整数）。
- 每种肥料录入名称与所需母液量（mL，非负整数；**用量为 0 的肥料不参与求解**）。
- 两两不相容关系构成无向图：有边相连的两种肥料**必须分到不同罐**。

求解步骤
--------
1. 只用「用量 > 0」的肥料构建不相容图，按边拆分**连通分量**并做 BFS 二着色。
2. 出现**奇环**时图不可二着色 -> 在每个非二分分量上以**奇偶状态 BFS**
   （状态为「节点 + 走到该节点路径的奇偶性」）求该分量的最短奇环，
   全图各分量取最短者（同长按规范化肥料 ID 序列决胜），不再给分罐结果。
3. 每个连通分量着色为两侧（side0 / side1），翻转方向决定哪一侧进 A 罐；
   枚举所有分量的 2^k 种全局翻转组合（分量数 > 20 时组合过多直接报错）。
4. 目标：先要求双罐均不超容量；可行组合中以 **|A 体积 − B 体积| 最小**为目标，
   同分按 **A 罐肥料 ID 升序序列**字典序决胜。
5. 所有组合都超容量时不返回「无法分配」，而是给出**总超额最小**的组合
   （超额相同时仍按体积差、A 罐 ID 序列决胜）及其分罐明细。

本模块无状态，用量 / 关系 / 容量变更后由调用方整体重算。
"""

from collections import deque

TANK_A = "A"
TANK_B = "B"

OK = "ok"                # 存在满足双罐容量的组合
OVERFLOW = "overflow"    # 所有组合都超容量，返回最小总超额方案
CONFLICT = "conflict"   # 不相容图含奇环，无法二着色
EMPTY = "empty"          # 没有用量大于 0 的肥料
ERROR = "error"          # 组合规模超限等，无法求解

STATUS_LABEL = {
    OK: "可分配",
    OVERFLOW: "容量超额（已给出最小超额方案）",
    CONFLICT: "不相容冲突（奇环）",
    EMPTY: "暂无参与求解的肥料",
    ERROR: "无法求解",
}

# 2^20 ≈ 1.05e6 次枚举仍可秒级完成；超过则要求用户拆分配方
MAX_COMPONENTS = 20
# 组合数不超过该值时在结果中附全部组合（页面「组合枚举」明细 / 导出复核用）
LIST_COMBINATIONS = 128

VOLUME_MIN, VOLUME_MAX = 1, 1_000_000_000     # mL
AMOUNT_MIN, AMOUNT_MAX = 0, 1_000_000_000


# ---------------------------------------------------------------- 校验

def _int(raw, field, minimum):
    if raw in (None, "") or isinstance(raw, bool):
        if raw in (None, ""):
            raise ValueError(f"缺少字段：{field}")
        raise ValueError(f"{field} 必须为整数")
    if isinstance(raw, int):
        val = raw
    elif isinstance(raw, float):
        if not raw.is_integer():
            raise ValueError(f"{field} 必须为整数")
        val = int(raw)
    elif isinstance(raw, str):
        n = raw.strip()
        if not n:
            raise ValueError(f"缺少字段：{field}")
        try:
            val = int(n)
        except ValueError:
            raise ValueError(f"{field} 必须为整数")
    else:
        raise ValueError(f"{field} 必须为整数")
    if val < minimum:
        raise ValueError(f"{field} 必须 ≥ {minimum}")
    if val > VOLUME_MAX:
        raise ValueError(f"{field} 超出允许上限（{VOLUME_MAX} mL）")
    return val


def validate_cap(raw, field="罐容量"):
    """罐容量：正整数（mL）。"""
    return _int(raw, field, VOLUME_MIN)


def validate_amount(raw, field="所需母液量"):
    """肥料用量：非负整数（mL）；0 表示暂不参与求解。"""
    return _int(raw, field, AMOUNT_MIN)


# ---------------------------------------------------------------- 图拆分 / 二着色

def _build_graph(active, pair_ids):
    """active: [(id, amount)]；pair_ids: [(a_id, b_id, edge_id)] 仅保留两端都在用量>0集合的边。"""
    ids = {fid for fid, _ in active}
    adj = {fid: set() for fid, _ in active}
    edges = []
    for a, b, eid in pair_ids:
        if a in ids and b in ids and a != b:
            adj[a].add(b)
            adj[b].add(a)
            edges.append((a, b, eid))
    return adj, edges


def _split_components(adj):
    """BFS 拆分连通分量并二着色。

    返回 [(member_ids, color, is_conflict)]：
    member_ids 按发现顺序；color 为 {id: 0/1}（奇环时着色仍返回，is_conflict=True）。
    """
    seen = set()
    components = []
    for root in sorted(adj):
        if root in seen:
            continue
        color = {root: 0}
        order = []
        q = deque([root])
        seen.add(root)
        is_conflict = False
        while q:
            u = q.popleft()
            order.append(u)
            for v in sorted(adj[u]):
                if v not in seen:
                    seen.add(v)
                    color[v] = color[u] ^ 1
                    q.append(v)
                elif color[v] == color[u]:
                    is_conflict = True
        components.append((order, color, is_conflict))
    components.sort(key=lambda c: c[0][0])
    return components


def _parity_bfs_dist(adj, src):
    """奇偶状态 BFS：返回 dist[(node, parity)] = src 出发以该奇偶长度到达 node
    的最短**步数**（按边计）。状态 (u, p) 经边 (u, v) 转到 (v, p ^ 1)。
    """
    dist = {(src, 0): 0}
    q = deque([(src, 0)])
    while q:
        u, p = q.popleft()
        nd = dist[(u, p)] + 1
        for v in sorted(adj[u]):
            state = (v, p ^ 1)
            if state not in dist:
                dist[state] = nd
                q.append(state)
    return dist


def _lex_min_parity_walk(adj, start_state, end_state, forward, backward):
    """在所有 start_state → end_state 的最短（步数 = forward[end_state]）状态
    路径中，按节点 ID 序列字典序取最小者，并返回对应的**节点序列**（不含终点状态）。

    forward/backward 分别为起点正向、终点反向（无向图中状态翻转）奇偶 BFS 距离。
    每一步在「剩余步数恰好用完可达终点」的邻点中选编号最小者，保证字典序最小且
    一定落在某条最短路径上。
    """
    total = forward[end_state]
    cur = start_state
    walk_nodes = [cur[0]]
    for step in range(total):
        u, p = cur
        remain = total - step - 1
        candidates = []
        for v in adj[u]:
            nxt = (v, p ^ 1)
            # 正向尚未偏离最短路径（由逐步选择保证），且该下一步能在剩余步数内到终点
            if backward.get(nxt) == remain:
                candidates.append(v)
        chosen = min(candidates)
        cur = (chosen, p ^ 1)
        walk_nodes.append(cur[0])
    return walk_nodes


def _extract_simple_cycle(walk_nodes):
    """最短奇闭游走（lollipop 形：引道 + 简单奇环 + 原路返回）中抽出简单奇环节点序列。

    walk_nodes 以 s 起、以 s 止且长度为奇数；首个重复出现的节点即环与引道的交点，
    重复点之间的段落为简单奇环（闭环时不再追加该点）。
    """
    first_pos = {}
    for idx, node in enumerate(walk_nodes):
        if node in first_pos:
            return walk_nodes[first_pos[node]:idx]
        first_pos[node] = idx
    # 无重复节点：整段就是一个简单奇环（去掉末尾闭合点）
    return walk_nodes[:-1]


def _canonical_ring(nodes):
    """把闭环节点序列旋转 / 反向规整为唯一表示（最小 ID 开头，次节点取较小方向）。"""
    n = len(nodes)
    lo = min(range(n), key=lambda i: nodes[i])
    rotated = tuple(nodes[lo:] + nodes[:lo])
    reversed_ring = tuple([rotated[0]] + list(reversed(rotated[1:])))
    return rotated if rotated < reversed_ring else reversed_ring


def _shortest_odd_ring(adj, members):
    """在一个连通分量内求最短奇环（odd girth）。

    采用奇偶状态 BFS：把每个节点复制为「偶到达 / 奇到达」两个状态，边连接两种奇偶
    状态。从 s 以偶状态出发、回到 s 的奇状态的最短路径 = 经过 s 的最短奇闭游走，
    其环形部分就是一个简单奇环；遍历分量内每个起点 s，取全分量最短者。

    这避免了旧做法「删 BFS 着色冲突边后取端点普通最短路」的漏报：那种方法只看
    删边后的**单条最短路**，当它与冲突边闭合成偶环（更短的偶环捷径）时该边被跳过；
    若全部冲突边都如此，真正更长的奇环会被整体漏报。

    决胜：等长奇环中取**规范化节点 ID 序列**（最小 ID 开头、次节点取较小方向）
    字典序最小者。返回 (长度, 规范化 tuple)；分量为二分图时返回 None。
    """
    member_set = set(members)
    best = None  # (length, canonical tuple)
    for s in sorted(member_set):
        forward = _parity_bfs_dist(adj, s)
        end_state = (s, 1)
        if end_state not in forward:
            continue  # 该起点在二分分量中：无奇环
        # 反向距离：无向图中 (s,1) 反向到 (u,p) 的最短距离，等于正向从 s 出发
        # 到达 (u, 1 ^ p) 的距离（沿边走奇偶性翻转）
        backward = {}
        for (u, p), d in forward.items():
            backward[(u, 1 ^ p)] = d
        walk = _lex_min_parity_walk(adj, (s, 0), end_state, forward, backward)
        cycle = _extract_simple_cycle(walk)
        canonical = _canonical_ring(cycle)
        if best is None or (len(canonical), canonical) < (best[0], best[1]):
            best = (len(canonical), canonical)
    return best


# ---------------------------------------------------------------- 求解

def solve(cap_a, cap_b, fertilizers, incompat_pairs):
    """对一份配方整体重算。

    参数
    ----
    cap_a / cap_b : int，A/B 罐容量（mL，正整数）
    fertilizers   : [{"id", "name", "amount"}]，amount 为非负整数
    incompat_pairs: [{"id", "a_id", "b_id"}]，两两不相容关系

    返回
    ----
    状态字典，status 取 ok / overflow / conflict / empty / error（组合规模超限）。
    """
    cap_a = validate_cap(cap_a, "A 罐容量")
    cap_b = validate_cap(cap_b, "B 罐容量")

    name_by_id = {}
    active_rows = []
    ignored = []
    for f in fertilizers:
        fid = int(f["id"])
        name_by_id[fid] = f["name"]
        amount = validate_amount(f.get("amount"), f"肥料「{f['name']}」所需母液量")
        row = {"id": fid, "name": f["name"], "amount": amount}
        if amount > 0:
            active_rows.append(row)
        else:
            ignored.append(row)
    active_rows.sort(key=lambda r: r["id"])
    amount_by_id = {r["id"]: r["amount"] for r in active_rows}

    pair_rows = []
    seen_pairs = set()
    for p in incompat_pairs:
        a, b = int(p["a_id"]), int(p["b_id"])
        key = (min(a, b), max(a, b))
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        pair_rows.append({"id": p.get("id"), "a_id": key[0], "b_id": key[1]})

    result = {
        "status": EMPTY,
        "status_label": STATUS_LABEL[EMPTY],
        "cap_a": cap_a,
        "cap_b": cap_b,
        "total_amount": 0,
        "active": [],
        "ignored": ignored,
        "components": [],
        "edges": [],
        "conflict_ring": None,
        "conflict_component": None,
        "decision": None,
        "error": None,
    }
    if not active_rows:
        result["edges"] = []  # 无参与肥料时关系也不构成约束
        return result

    adj, _graph_edges = _build_graph(
        [(r["id"], r["amount"]) for r in active_rows],
        [(p["a_id"], p["b_id"], p["id"]) for p in pair_rows])

    raw_components = _split_components(adj)
    comp_index_by_id = {}
    comp_blocks = []
    conflict_comp_idx = None
    conflict_ring = None  # 规范化 tuple；跨分量等长时按规范化 ID 序列决胜
    for idx, (members, color, is_conflict) in enumerate(raw_components, start=1):
        for m in members:
            comp_index_by_id[m] = idx
        sides = [[], []]
        for m in members:
            sides[color[m]].append(m)
        if is_conflict:
            best_in_comp = _shortest_odd_ring(adj, members)
            if best_in_comp is not None:
                length, canonical = best_in_comp
                if (conflict_ring is None
                        or (length, canonical)
                        < (len(conflict_ring), conflict_ring)):
                    conflict_ring = canonical
                    conflict_comp_idx = idx
        sums = [sum(amount_by_id[m] for m in sides[0]),
                sum(amount_by_id[m] for m in sides[1])]
        comp_blocks.append({
            "index": idx,
            "member_ids": members,
            "side0": sides[0],
            "side1": sides[1],
            "sum0": sums[0],
            "sum1": sums[1],
            "conflict": is_conflict,
        })

    ring_set = set(conflict_ring or [])
    edge_rows = []
    for p in pair_rows:
        a, b = p["a_id"], p["b_id"]
        if a not in amount_by_id or b not in amount_by_id:
            continue  # 一端用量为 0 的关系不参与图，但仍可由前端单独展示
        edge_rows.append({
            "id": p["id"],
            "a_id": a, "b_id": b,
            "a_name": name_by_id.get(a), "b_name": name_by_id.get(b),
            "in_ring": bool(ring_set and a in ring_set and b in ring_set
                            and _adjacent_in_ring(conflict_ring, a, b)),
        })
    edge_rows.sort(key=lambda e: (e["a_id"], e["b_id"]))

    result.update({
        "total_amount": sum(r["amount"] for r in active_rows),
        "active": [{
            "id": r["id"], "name": r["name"], "amount": r["amount"],
            "component": comp_index_by_id[r["id"]],
            "color": dict((m, c) for _, color, _ in raw_components
                          for m, c in color.items())[r["id"]],
        } for r in active_rows],
        "components": comp_blocks,
        "edges": edge_rows,
    })

    if conflict_ring is not None:
        result["status"] = CONFLICT
        result["status_label"] = STATUS_LABEL[CONFLICT]
        result["conflict_ring"] = [
            {"id": fid, "name": name_by_id.get(fid)} for fid in conflict_ring]
        result["conflict_component"] = conflict_comp_idx
        return result

    if len(comp_blocks) > MAX_COMPONENTS:
        result["status"] = ERROR
        result["status_label"] = STATUS_LABEL[ERROR]
        result["error"] = (
            f"不相容图连通分量达 {len(comp_blocks)} 个，全局组合数 "
            f"2^{len(comp_blocks)} 过多，无法枚举（上限 {MAX_COMPONENTS} 个分量），"
            "请拆分配方或增加不相容关系")
        return result

    decision = _decide(cap_a, cap_b, comp_blocks, amount_by_id, name_by_id)
    result["status"] = decision["kind"]
    result["status_label"] = STATUS_LABEL[decision["kind"]]
    result["decision"] = decision
    return result


def _adjacent_in_ring(ring, a, b):
    """环（顺序节点列表，首尾相连）中 a、b 是否相邻。"""
    if not ring:
        return False
    n = len(ring)
    for i in range(n):
        x, y = ring[i], ring[(i + 1) % n]
        if (x, y) == (a, b) or (x, y) == (b, a):
            return True
    return False


def _decide(cap_a, cap_b, components, amount_by_id, name_by_id):
    """枚举各分量两种翻转方向的全部全局组合，按规则排序选优。"""
    k = len(components)  # 调用方已保证 k <= MAX_COMPONENTS

    def item_rows(ids):
        return [{"id": fid, "name": name_by_id.get(fid),
                 "amount": amount_by_id[fid]} for fid in ids]

    all_combos = []
    for mask in range(1 << k):
        flips = [(mask >> i) & 1 for i in range(k)]
        a_ids, b_ids = [], []
        for i, comp in enumerate(components):
            side_a = comp["side1"] if flips[i] else comp["side0"]
            side_b = comp["side0"] if flips[i] else comp["side1"]
            a_ids.extend(side_a)
            b_ids.extend(side_b)
        a_ids.sort()
        b_ids.sort()
        used_a = sum(amount_by_id[fid] for fid in a_ids)
        used_b = sum(amount_by_id[fid] for fid in b_ids)
        excess_a = max(0, used_a - cap_a)
        excess_b = max(0, used_b - cap_b)
        all_combos.append({
            "flips": flips,
            "a_ids": a_ids,
            "b_ids": b_ids,
            "used_a": used_a,
            "used_b": used_b,
            "diff": abs(used_a - used_b),
            "excess_a": excess_a,
            "excess_b": excess_b,
            "excess_total": excess_a + excess_b,
            "feasible": used_a <= cap_a and used_b <= cap_b,
        })

    # 决胜：可行优先 → 总超额最小 → 两罐体积差最小 → A 罐肥料 ID 升序序列字典序
    def rank(c):
        return (0 if c["feasible"] else 1, c["excess_total"], c["diff"],
                tuple(c["a_ids"]))

    feasible_count = sum(1 for c in all_combos if c["feasible"])
    chosen = min(all_combos, key=rank)
    kind = OK if feasible_count else OVERFLOW

    enumerated = len(all_combos) <= LIST_COMBINATIONS
    combos_out = []
    if enumerated:
        for c in sorted(all_combos, key=rank):
            combos_out.append({
                "flips": c["flips"],
                "a_ids": c["a_ids"],
                "b_ids": c["b_ids"],
                "used_a": c["used_a"],
                "used_b": c["used_b"],
                "diff": c["diff"],
                "excess_a": c["excess_a"],
                "excess_b": c["excess_b"],
                "excess_total": c["excess_total"],
                "feasible": c["feasible"],
            })

    chosen_out = {
        "flips": chosen["flips"],
        "a_ids": chosen["a_ids"],
        "b_ids": chosen["b_ids"],
        "used_a": chosen["used_a"],
        "used_b": chosen["used_b"],
        "diff": chosen["diff"],
        "excess_a": chosen["excess_a"],
        "excess_b": chosen["excess_b"],
        "excess_total": chosen["excess_total"],
        "spare_a": cap_a - chosen["used_a"],
        "spare_b": cap_b - chosen["used_b"],
        "feasible": chosen["feasible"],
        "tank_a": item_rows(chosen["a_ids"]),
        "tank_b": item_rows(chosen["b_ids"]),
    }
    return {
        "kind": kind,
        "combination_count": len(all_combos),
        "feasible_count": feasible_count,
        "enumerated": enumerated,
        "combinations": combos_out,
        "chosen": chosen_out,
    }
