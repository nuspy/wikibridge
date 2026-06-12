"""Grafo della conoscenza del progetto.

Nodi = pagine wiki (sources/entities/concepts/meetings) + archi da:
- menzioni (frontmatter mentions delle entità/concetti)
- relazioni estratte dal cataloguer (graph/relations.jsonl)
Output: graph/graph.json e graph/graph.html (vis-network da CDN,
nessun passo di build — apribile direttamente nel browser/GUI).
"""
from __future__ import annotations

import json
from pathlib import Path

from ..projects import Project
from .writer import WikiWriter, page_slug

_COLORS = {
    "sources": "#4f8cc9",
    "entities": "#c94f4f",
    "concepts": "#4fc97a",
    "syntheses": "#c9b14f",
    "meetings": "#9b4fc9",
}

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="utf-8">
<title>Grafo — {title}</title>
<script src="https://unpkg.com/vis-network@9.1.9/dist/vis-network.min.js"></script>
<style>
  body {{ margin: 0; font-family: system-ui, sans-serif; background: #1e1e2e; color: #eee; }}
  #graph {{ width: 100vw; height: 100vh; }}
  #legend {{ position: fixed; top: 10px; left: 10px; background: #00000088;
             padding: 8px 12px; border-radius: 8px; font-size: 13px; }}
  .dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%;
          margin-right: 5px; }}
</style>
</head>
<body>
<div id="legend">
  <b>{title}</b><br>
  <span class="dot" style="background:#4f8cc9"></span>Fonti
  <span class="dot" style="background:#c94f4f"></span>Entità
  <span class="dot" style="background:#4fc97a"></span>Concetti
  <span class="dot" style="background:#9b4fc9"></span>Riunioni
</div>
<div id="graph"></div>
<script>
const data = {data};
const nodes = new vis.DataSet(data.nodes);
const edges = new vis.DataSet(data.edges);
new vis.Network(document.getElementById("graph"), {{nodes, edges}}, {{
  physics: {{ stabilization: {{ iterations: 200 }}, barnesHut: {{ gravitationalConstant: -8000 }} }},
  nodes: {{ shape: "dot", font: {{ color: "#eee" }} }},
  edges: {{ color: {{ color: "#666" }}, font: {{ color: "#aaa", size: 10 }}, arrows: "to" }},
}});
</script>
</body>
</html>
"""


def build_graph(project: Project) -> dict:
    writer = WikiWriter(project)
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    name_to_id: dict[str, str] = {}

    for rel_path, fm, _body in writer.iter_pages():
        section = rel_path.split("/", 1)[0]
        if section == "syntheses":
            continue
        title = fm.get("title") or rel_path
        nodes[rel_path] = {
            "id": rel_path, "label": title[:40],
            "title": title, "group": section,
            "color": _COLORS.get(section, "#888"),
            "value": 1,
        }
        name_to_id[page_slug(title)] = rel_path

    # archi dalle menzioni
    for rel_path, fm, _body in writer.iter_pages():
        for mention in fm.get("mentions", []) or []:
            if mention in nodes and rel_path in nodes:
                edges.append({"from": mention, "to": rel_path, "label": ""})
                nodes[rel_path]["value"] += 1

    # archi dalle relazioni del cataloguer
    rel_store = project.graph_dir / "relations.jsonl"
    if rel_store.is_file():
        for line in rel_store.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            src = name_to_id.get(page_slug(str(r.get("from", ""))))
            dst = name_to_id.get(page_slug(str(r.get("to", ""))))
            if src and dst and src != dst:
                edges.append({"from": src, "to": dst,
                              "label": str(r.get("kind", ""))[:20]})

    graph = {"nodes": list(nodes.values()), "edges": edges}
    project.graph_dir.mkdir(parents=True, exist_ok=True)
    (project.graph_dir / "graph.json").write_text(
        json.dumps(graph, ensure_ascii=False, indent=1), encoding="utf-8")
    html = _HTML_TEMPLATE.format(
        title=project.name,
        data=json.dumps(graph, ensure_ascii=False))
    (project.graph_dir / "graph.html").write_text(html, encoding="utf-8")
    return {"nodes": len(nodes), "edges": len(edges)}
