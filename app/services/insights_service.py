from typing import List, Dict, Any
from collections import defaultdict

class InsightsService:

    def generate_all(self, graph_data: dict) -> dict:
        nodes = graph_data.get("nodes", [])
        edges = graph_data.get("edges", [])

        return {
            "summary": self._summary(nodes, edges),
            "high_dependency": self._high_dependency_modules(nodes, edges),
            "circular_deps": self._detect_circular(nodes, edges),
            "unused_files": self._unused_files(nodes, edges),
            "performance_risks": self._performance_risks(nodes, edges),
            "health_score": self._health_score(nodes, edges),
        }

    def _summary(self, nodes, edges) -> dict:
        return {
            "total_files": len(nodes),
            "total_dependencies": len(edges),
            "avg_dependencies": round(len(edges) / max(len(nodes), 1), 2),
        }

    def _high_dependency_modules(self, nodes, edges, threshold=5) -> List[dict]:
        """Files that are imported by many others = high risk"""
        import_count = defaultdict(int)
        for edge in edges:
            import_count[edge.get("target")] += 1

        results = []
        for node_id, count in import_count.items():
            if count >= threshold:
                node = next((n for n in nodes if n.get("id") == node_id), {})
                results.append({
                    "file": node.get("label", node_id),
                    "imported_by": count,
                    "risk": "high" if count > 10 else "medium",
                    "insight": f"This file is imported by {count} modules. "
                               f"Changes here will have wide impact."
                })

        return sorted(results, key=lambda x: x["imported_by"], reverse=True)

    def _detect_circular(self, nodes, edges) -> List[dict]:
        """Detect circular dependencies using DFS"""
        graph = defaultdict(list)
        for edge in edges:
            graph[edge.get("source")].append(edge.get("target"))

        visited = set()
        rec_stack = set()
        cycles = []

        def dfs(node, path):
            visited.add(node)
            rec_stack.add(node)

            for neighbor in graph[node]:
                if neighbor not in visited:
                    dfs(neighbor, path + [neighbor])
                elif neighbor in rec_stack:
                    cycle_start = path.index(neighbor) if neighbor in path else 0
                    cycles.append({
                        "cycle": path[cycle_start:] + [neighbor],
                        "severity": "critical",
                        "insight": "Circular dependency detected. This can cause "
                                   "memory leaks and hard-to-debug issues."
                    })

            rec_stack.discard(node)

        for node in graph:
            if node not in visited:
                dfs(node, [node])

        return cycles[:10]  # Return top 10

    def _unused_files(self, nodes, edges) -> List[dict]:
        """Files that are never imported by anything"""
        imported = {edge.get("target") for edge in edges}

        unused = []
        for node in nodes:
            node_id = node.get("id")
            if node_id not in imported:
                unused.append({
                    "file": node.get("label", node_id),
                    "path": node.get("path", ""),
                    "insight": "This file is never imported. "
                               "Consider removing it to reduce bundle size."
                })

        return unused

    def _performance_risks(self, nodes, edges) -> List[dict]:
        """Identify files with too many dependencies"""
        dep_count = defaultdict(int)
        for edge in edges:
            dep_count[edge.get("source")] += 1

        risks = []
        for node_id, count in dep_count.items():
            if count > 8:
                node = next((n for n in nodes if n.get("id") == node_id), {})
                risks.append({
                    "file": node.get("label", node_id),
                    "dependency_count": count,
                    "severity": "high" if count > 15 else "medium",
                    "insight": f"This file imports {count} modules. "
                               f"Consider splitting it into smaller modules."
                })

        return sorted(risks, key=lambda x: x["dependency_count"], reverse=True)

    def _health_score(self, nodes, edges) -> dict:
        """Overall codebase health 0-100"""
        score = 100
        issues = []

        # Penalize circular deps
        circulars = len(self._detect_circular(nodes, edges))
        if circulars > 0:
            score -= circulars * 10
            issues.append(f"{circulars} circular dependencies found")

        # Penalize unused files
        unused = len(self._unused_files(nodes, edges))
        if unused > 5:
            score -= 5
            issues.append(f"{unused} unused files detected")

        # Penalize high coupling
        risks = len(self._performance_risks(nodes, edges))
        if risks > 0:
            score -= risks * 3
            issues.append(f"{risks} highly coupled modules")

        score = max(0, min(100, score))

        return {
            "score": score,
            "grade": "A" if score >= 90 else "B" if score >= 75 else "C" if score >= 60 else "D",
            "status": "healthy" if score >= 75 else "needs_attention" if score >= 50 else "critical",
            "issues": issues,
        }