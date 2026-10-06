"""依赖图、口径兼容性与关键路径的纯函数逻辑。

这些函数不触碰数据库，便于单独测试并保证在并发恢复后
只要输入状态一致，结果就唯一且稳定。
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, Iterable


def reachable_downstream(graph: dict[str, set[str]], start: str) -> set[str]:
    """返回从 start 出发沿依赖方向（上游→下游）可达的全部节点。"""

    seen: set[str] = set()
    queue = deque(graph.get(start, ()))
    while queue:
        node = queue.popleft()
        if node in seen:
            continue
        seen.add(node)
        queue.extend(graph.get(node, ()))
    return seen


def reachable_upstream(reverse: dict[str, set[str]], start: str) -> set[str]:
    """返回沿反向边可到达 start 的全部上游节点。"""

    return reachable_downstream(reverse, start)


def topological_order(graph: dict[str, set[str]], nodes: Iterable[str]) -> list[str]:
    """在给定节点集合内给出稳定的拓扑序（编号次序打破并列）。"""

    nodes = set(nodes)
    indegree: dict[str, int] = {node: 0 for node in nodes}
    for node in nodes:
        for nxt in graph.get(node, ()):
            if nxt in nodes:
                indegree[nxt] += 1
    ready = sorted(node for node, degree in indegree.items() if degree == 0)
    order: list[str] = []
    while ready:
        node = ready.pop(0)
        order.append(node)
        for nxt in sorted(graph.get(node, ())):
            if nxt not in nodes:
                continue
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)
        ready.sort()
    return order


def critical_path(graph: dict[str, set[str]], durations: dict[str, int],
                  completed: set[str], roots: Iterable[str]) -> list[str]:
    """计算当前关键路径。

    已完成节点视为工期 0；在从 roots 可达的 DAG 上做最早开始调度，
    返回决定项目最短完工时间的一条路径。并列时按节点编号取字典序最小，
    以保证结果稳定。
    """

    roots = set(roots)
    reachable = set(roots)
    for root in list(roots):
        reachable |= reachable_downstream(graph, root)
    order = topological_order(graph, reachable)

    earliest_finish: dict[str, int] = {}
    predecessor: dict[str, str | None] = {}
    for node in order:
        duration = 0 if node in completed else durations.get(node, 1)
        best_finish = duration
        best_pred: str | None = None
        ups = sorted(pred for pred, nxts in graph.items() if node in nxts and pred in reachable)
        for pred in ups:
            candidate = earliest_finish[pred] + duration
            if candidate > best_finish or (candidate == best_finish and (
                    best_pred is None or pred < best_pred)):
                best_finish = candidate
                best_pred = pred
        earliest_finish[node] = best_finish
        predecessor[node] = best_pred

    if not earliest_finish:
        return []
    # 终点候选：没有下游出边的节点
    endpoints = [node for node in order if not (graph.get(node, set()) & reachable)]
    endpoint = max(
        sorted(endpoints),
        key=lambda node: (earliest_finish[node],),
    )
    path: list[str] = []
    current: str | None = endpoint
    while current is not None:
        path.append(current)
        current = predecessor[current]
    path.reverse()
    return path


def caliber_compatible(metric_history: list[dict[str, Any]], used_version: int,
                       current_version: int) -> bool:
    """判断证据生产时的口径版本对当前版本是否仍然兼容。

    metric_history 为按版本升序的口径版本记录，每个记录含 ``version``
    与 ``compatible``（该版本相对前一版是否兼容）。任一跨越的版本
    被标记为不兼容，则旧口径证据不得继续占用。
    """

    if used_version > current_version:
        return False
    if used_version == current_version:
        return True
    by_version = {item["version"]: item for item in metric_history}
    for version in range(used_version + 1, current_version + 1):
        record = by_version.get(version)
        if record is None or not record.get("compatible", True):
            return False
    return True
