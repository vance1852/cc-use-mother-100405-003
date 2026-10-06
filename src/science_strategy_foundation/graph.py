"""里程碑依赖图：环检测、影响闭包与关键路径计算。"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable, Iterable


class CycleError(ValueError):
    """新增依赖会在里程碑之间形成环。"""


def detect_cycle(upstream: str, downstream: str,
                 successors: Callable[[str], Iterable[str]]) -> bool:
    """若新增边 upstream->downstream 后成环则返回 True。

    即 downstream 已经能沿现有依赖到达 upstream。
    """

    stack = [downstream]
    seen: set[str] = set()
    while stack:
        node = stack.pop()
        if node == upstream:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(successors(node))
    return False


def transitive_closure(start: str, successors: Callable[[str], Iterable[str]]) -> set[str]:
    """返回从 start 沿后继边可达的全部节点（不含 start）。"""

    result: set[str] = set()
    stack = list(successors(start))
    while stack:
        node = stack.pop()
        if node in result:
            continue
        result.add(node)
        stack.extend(successors(node))
    return result


def topo_order(nodes: Iterable[str], successors: Callable[[str], Iterable[str]]) -> list[str]:
    """对给定节点做确定性拓扑排序（按节点编号兜底）。"""

    node_set = set(nodes)
    visited: set[str] = set()
    order: list[str] = []

    def visit(node: str, path: set[str]) -> None:
        if node in visited:
            return
        if node in path:
            raise CycleError(node)
        path.add(node)
        for child in sorted(successors(node)):
            if child in node_set:
                visit(child, path)
        path.discard(node)
        visited.add(node)
        order.append(node)

    for node in sorted(node_set):
        visit(node, set())
    return order


def critical_path(milestones: dict[str, dict[str, Any]],
                  edges: set[tuple[str, str]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """计算最早完成工期与关键路径。

    milestones[m] 至少包含 planned_days；edges 为 (upstream, downstream) 集合。
    返回（关键路径上的节点列表, 每个节点的最早完成天数）。
    """

    successors: dict[str, list[str]] = defaultdict(list)
    predecessors: dict[str, list[str]] = defaultdict(list)
    for upstream, downstream in edges:
        if upstream in milestones and downstream in milestones:
            successors[upstream].append(downstream)
            predecessors[downstream].append(upstream)

    order = list(reversed(topo_order(milestones.keys(), lambda n: successors.get(n, ()))))
    earliest_finish: dict[str, int] = {}
    for node in order:
        own = int(milestones[node].get("planned_days", 0))
        pre = [earliest_finish[p] for p in predecessors.get(node, ()) if p in earliest_finish]
        earliest_finish[node] = max(pre, default=0) + own

    if not earliest_finish:
        return [], {}
    project_finish = max(earliest_finish.values())

    # 从最晚完成的汇点反向回溯：松弛为 0 的边即在关键路径上。
    sinks = [n for n in order if not successors.get(n)]
    end = max(sinks, key=lambda n: (earliest_finish[n], n)) if sinks else None
    path: list[str] = []
    if end is not None:
        current = end
        path.append(current)
        while True:
            pres = predecessors.get(current, ())
            critical_pre = [p for p in pres
                            if earliest_finish.get(p) == earliest_finish[current]
                            - int(milestones[current].get("planned_days", 0))]
            if not critical_pre:
                break
            current = sorted(critical_pre)[0]
            path.append(current)
        path.reverse()
    return [milestones[n] for n in path], earliest_finish
