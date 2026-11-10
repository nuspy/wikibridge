"""Confluence finto su HTTP reale: REST /wiki/rest/api con contenuti,
figli, antenati, restrizioni di lettura e ricerca CQL (sottoinsieme),
modificabili dai test. Conta le chiamate per verificare l'efficienza."""
import re
import threading
from datetime import datetime, timezone

from flask import Flask, abort, jsonify, request
from werkzeug.serving import make_server


class FakeConfluence:
    def __init__(self):
        # id -> {title, body, parent, version, modified, read: {...}}
        self.pages: dict[str, dict] = {}
        self.forbidden: set[str] = set()          # restrizioni non leggibili (403)
        self.unindexed: set[str] = set()          # indice di ricerca in ritardo
        self.calls: list[tuple[str, str]] = []    # (percorso, expand)
        app = Flask("fake-confluence")

        @app.before_request
        def count():
            self.calls.append((request.path, request.args.get("expand", "")))

        app.add_url_rule("/wiki/rest/api/space", view_func=lambda: jsonify({"results": []}))
        app.add_url_rule("/wiki/rest/api/content/search", view_func=self.search)
        app.add_url_rule("/wiki/rest/api/content/<cid>", view_func=self.content)
        app.add_url_rule("/wiki/rest/api/content/<cid>/child/page", view_func=self.children)
        app.add_url_rule("/wiki/rest/api/content/<cid>/restriction/byOperation/read",
                         view_func=self.restriction)
        self.server = make_server("127.0.0.1", 0, app, threaded=True)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()

    def put(self, cid, title, body, parent=None, users=(), groups=()):
        old = self.pages.get(cid)
        self.pages[cid] = {"title": title, "body": body, "parent": parent,
                           "version": old["version"] + 1 if old else 1,
                           "modified": datetime.now(timezone.utc).replace(tzinfo=None),
                           "read": {"users": list(users), "groups": list(groups)}}

    def count(self, pattern: str) -> int:
        return sum(1 for path, expand in self.calls if re.search(pattern, f"{path}?{expand}"))

    def search(self):
        """Sottoinsieme di CQL: type=page, ancestor=N, id=N, space="K",
        lastmodified >= "AAAA-MM-GG HH:MM"."""
        cql = request.args.get("cql", "")
        ids = [c for c in self.pages if c not in self.unindexed]
        anc = re.findall(r"ancestor=(\d+)", cql)
        own = re.findall(r"\bid=(\d+)", cql)
        if anc:
            ids = [c for c in ids if anc[0] in {a["id"] for a in self._ancestors(c)}
                   or c in own]
        m = re.search(r'lastmodified >= "([^"]+)"', cql)
        if m:
            since = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M")
            ids = [c for c in ids if self.pages[c]["modified"] >= since]
        start, limit = int(request.args.get("start", 0)), int(request.args.get("limit", 25))
        return jsonify({"results": [self._ref(c) for c in ids[start:start + limit]],
                        "limit": limit})

    def restrict(self, cid, users=(), groups=()):
        """Cambia solo le restrizioni: in Confluence la versione non cambia."""
        self.pages[cid]["read"] = {"users": list(users), "groups": list(groups)}

    def url(self, cid):
        return f"{self.base}/wiki/spaces/LEG/pages/{cid}/{self.pages[cid]['title']}"

    def _ref(self, cid):
        return {"id": cid, "title": self.pages[cid]["title"],
                "_links": {"webui": f"/spaces/LEG/pages/{cid}/{self.pages[cid]['title']}"}}

    def _ancestors(self, cid):
        chain, p = [], self.pages[cid]["parent"]
        while p:
            chain.insert(0, {"id": p})
            p = self.pages[p]["parent"]
        return chain

    def content(self, cid):
        if cid not in self.pages:
            abort(404)
        p = self.pages[cid]
        return jsonify({**self._ref(cid), "version": {"number": p["version"]},
                        "body": {"storage": {"value": f"<p>{p['body']}</p>"}},
                        "ancestors": self._ancestors(cid), "space": {"key": "LEG"},
                        "children": {"page": {"results": [
                            self._ref(c) for c, q in self.pages.items() if q["parent"] == cid]}}})

    def children(self, cid):
        return jsonify({"results": [self._ref(c) for c, q in self.pages.items()
                                    if q["parent"] == cid]})

    def restriction(self, cid):
        if cid in self.forbidden:
            abort(403)
        r = self.pages[cid]["read"]
        return jsonify({"operation": "read", "restrictions": {
            "user": {"results": [{"username": u, "displayName": u} for u in r["users"]]},
            "group": {"results": [{"name": g} for g in r["groups"]]}}})
