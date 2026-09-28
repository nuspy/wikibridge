"""Server HTTP di wikibridge (:8775) per la GUI Control Center.

Pattern di control_server.py di callAPIcall: Flask + Bearer token
(api_auth_token, vuoto = nessuna auth) + CORS. Un solo job di
import/ingest alla volta (409 se occupato), con progress consultabile.

I segreti dei profili auth sono WRITE-ONLY: la GET restituisce solo
nomi e tipi.
"""
from __future__ import annotations

import json
import logging
import tempfile
import threading
from pathlib import Path

from flask import Flask, jsonify, request
from flask_cors import CORS

from .auth import AuthProfileStore
from .catalog import drain_pending, make_catalog_hook
from .config import Config, REPO_DIR
from .convert.docs import ingest_path
from .importer import ImportsLog, refresh_page, run_import, sync_import
from .limits import ImportLimits
from .projects import ProjectStore
from .wiki.graph import build_graph
from .wiki.search import SearchIndex
from .wiki.writer import WikiWriter

log = logging.getLogger("wikibridge.server")


class JobRunner:
    """Un job (import/ingest/sync) alla volta, con stato osservabile."""

    def __init__(self):
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.cancel = threading.Event()
        self.state: dict = {"running": False}

    def start(self, kind: str, target, *args, **kwargs) -> bool:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self.cancel = threading.Event()
            self.state = {"running": True, "kind": kind, "phase": "starting",
                          "pages": 0, "max_pages": 0, "detail": "", "error": None,
                          "result": None}

            def progress(p: dict) -> None:
                self.state.update(p)

            def runner():
                try:
                    self.state["result"] = target(
                        *args, progress=progress,
                        **({"cancel": self.cancel} if kind == "import" else {}),
                        **kwargs)
                except Exception as e:  # noqa: BLE001
                    log.exception("job %s fallito", kind)
                    self.state["error"] = str(e)
                finally:
                    self.state["running"] = False

            self._thread = threading.Thread(target=runner, daemon=True,
                                            name=f"wikibridge-{kind}")
            self._thread.start()
            return True


def create_app(cfg: Config) -> Flask:
    app = Flask("wikibridge")
    CORS(app)
    store = ProjectStore(cfg)
    auth_store = AuthProfileStore(cfg)
    runner = JobRunner()

    @app.before_request
    def check_auth():
        if request.method == "OPTIONS" or not cfg.api_auth_token:
            return None
        header = request.headers.get("Authorization", "")
        if header != f"Bearer {cfg.api_auth_token}":
            return jsonify({"error": "non autorizzato"}), 401
        return None

    def proj_arg():
        slug = request.args.get("project")
        if not slug and request.is_json:
            slug = (request.get_json(silent=True) or {}).get("project")
        if not slug and request.form:
            slug = request.form.get("project")
        return store.get(slug or None)

    # -- base -------------------------------------------------------------

    @app.get("/health")
    def health():
        return jsonify({"ok": True, "active_project": store.active_slug(),
                        "job": runner.state})

    # -- progetti -----------------------------------------------------------

    @app.get("/projects")
    def projects_list():
        active = store.active_slug()
        return jsonify([{**p.to_dict(), "active": p.slug == active}
                        for p in store.list()])

    @app.post("/projects")
    def projects_create():
        body = request.get_json(force=True)
        try:
            p = store.create(body["name"], body.get("description", ""))
        except ValueError as e:
            return jsonify({"error": str(e)}), 409
        return jsonify(p.to_dict()), 201

    @app.delete("/projects/<slug>")
    def projects_delete(slug):
        try:
            store.delete(slug)
        except (KeyError, ValueError) as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"deleted": slug})

    @app.post("/projects/<slug>/activate")
    def projects_activate(slug):
        try:
            store.activate(slug)
        except KeyError as e:
            return jsonify({"error": str(e)}), 404
        return jsonify({"active": slug})

    # -- import -----------------------------------------------------------

    @app.post("/imports")
    def imports_start():
        body = request.get_json(force=True)
        project = store.get(body.get("project"))
        limits = ImportLimits.from_config(cfg, **{
            k: body.get(k) for k in
            ("max_pages", "max_depth", "max_total_mb", "rate_limit_rps")
            if body.get(k) is not None})
        if body.get("include"):
            limits.include = body["include"]
        if body.get("exclude"):
            limits.exclude = body["exclude"]
        catalog = None if body.get("no_catalog") else make_catalog_hook(cfg)
        ok = runner.start(
            "import", run_import, cfg, project, body["url"],
            depth=int(body.get("depth", 1)),
            scope=body.get("scope", "page"), limits=limits,
            auth_profile=body.get("auth_profile"),
            adapter_name=body.get("adapter", "auto"), catalog=catalog)
        if not ok:
            return jsonify({"error": "un altro job è in corso"}), 409
        return jsonify({"started": True, "project": project.slug}), 202

    @app.get("/imports")
    def imports_list():
        project = proj_arg()
        return jsonify({"project": project.slug,
                        "job": runner.state,
                        "imports": ImportsLog(project).load()})

    @app.get("/imports/<import_id>")
    def imports_get(import_id):
        project = proj_arg()
        try:
            return jsonify(ImportsLog(project).get(import_id))
        except KeyError as e:
            return jsonify({"error": str(e)}), 404

    @app.post("/imports/<import_id>/cancel")
    def imports_cancel(import_id):
        runner.cancel.set()
        return jsonify({"cancelling": True})

    @app.post("/imports/<import_id>/sync")
    def imports_sync(import_id):
        project = proj_arg()
        ok = runner.start("sync", sync_import, cfg, project, import_id,
                          catalog=make_catalog_hook(cfg))
        if not ok:
            return jsonify({"error": "un altro job è in corso"}), 409
        return jsonify({"started": True}), 202

    # -- ingest documenti ----------------------------------------------------

    @app.post("/ingest")
    def ingest_upload():
        project = proj_arg()
        f = request.files.get("file")
        if not f or not f.filename:
            return jsonify({"error": "file mancante"}), 400
        tmp_dir = Path(tempfile.mkdtemp(prefix="wikibridge-"))
        dest = tmp_dir / Path(f.filename).name
        f.save(dest)
        ok = runner.start("ingest", ingest_path, cfg, project, dest,
                          catalog=make_catalog_hook(cfg))
        if not ok:
            return jsonify({"error": "un altro job è in corso"}), 409
        return jsonify({"started": True}), 202

    @app.post("/ingest/dir")
    def ingest_dir():
        body = request.get_json(force=True)
        project = store.get(body.get("project"))
        path = Path(body["path"])
        if not path.exists():
            return jsonify({"error": f"percorso inesistente: {path}"}), 400
        ok = runner.start("ingest", ingest_path, cfg, project, path,
                          recursive=bool(body.get("recursive", True)),
                          catalog=make_catalog_hook(cfg))
        if not ok:
            return jsonify({"error": "un altro job è in corso"}), 409
        return jsonify({"started": True}), 202

    # -- riunioni → wiki -------------------------------------------------------

    @app.post("/meetings")
    def meetings_push():
        body = request.get_json(force=True)
        project = store.get(body.get("project"))
        writer = WikiWriter(project)
        path = writer.write_meeting(
            meeting_id=body.get("meeting_id", ""),
            title=body.get("title") or "Riunione",
            date=body.get("date", ""),
            source=body.get("source", "meetbot"),
            participants=body.get("participants", []),
            summary=body.get("summary", ""),
            highlights=body.get("highlights", []),
            agreements=body.get("agreements", []),
            tasks=body.get("tasks", []),
            transcript_path=body.get("transcript_path", ""))
        rel = f"meetings/{path.name}"
        fm, page_body = writer.read_page(rel)
        SearchIndex(project).upsert(rel, "meetings", fm.get("title", ""),
                                    page_body)
        writer.append_log(f"riunione inserita: {rel} ({body.get('source')})")
        writer.rebuild_index()
        return jsonify({"path": rel}), 201

    # -- wiki -----------------------------------------------------------------

    @app.get("/wiki/tree")
    def wiki_tree():
        project = proj_arg()
        return jsonify({"project": project.slug,
                        "tree": WikiWriter(project).tree()})

    @app.get("/wiki/page")
    def wiki_page():
        project = proj_arg()
        rel = request.args.get("path", "")
        try:
            fm, body = WikiWriter(project).read_page(rel)
        except (FileNotFoundError, ValueError) as e:
            return jsonify({"error": str(e)}), 404
        return jsonify({"path": rel, "frontmatter": fm, "body": body})

    @app.post("/wiki/refresh")
    def wiki_refresh():
        body = request.get_json(force=True)
        project = store.get(body.get("project"))
        try:
            result = refresh_page(cfg, project, body["path"],
                                  catalog=make_catalog_hook(cfg))
        except (FileNotFoundError, ValueError) as e:
            return jsonify({"error": str(e)}), 400
        return jsonify(result)

    @app.get("/search")
    def search():
        project = proj_arg()
        q = request.args.get("q", "")
        return jsonify({"project": project.slug,
                        "hits": SearchIndex(project).query(q)})

    @app.get("/graph")
    def graph_get():
        project = proj_arg()
        stats = build_graph(project)
        data = json.loads((project.graph_dir / "graph.json")
                          .read_text(encoding="utf-8"))
        return jsonify({**stats, "graph": data,
                        "html_path": str(project.graph_dir / "graph.html")})

    @app.post("/catalog/drain")
    def catalog_drain():
        project = proj_arg()
        return jsonify(drain_pending(cfg, project))

    # -- skill -----------------------------------------------------------------

    def _run_ensure() -> dict:
        import subprocess
        import sys
        r = subprocess.run(
            [sys.executable, str(REPO_DIR / "scripts" / "ensure_llmwiki.py")],
            capture_output=True, text=True, timeout=600)
        try:
            return {"results": json.loads(r.stdout), "ok": r.returncode == 0}
        except json.JSONDecodeError:
            return {"ok": False, "error": (r.stdout + r.stderr)[-500:]}

    @app.get("/skill/status")
    def skill_status():
        return jsonify(_run_ensure())

    @app.post("/skill/install")
    def skill_install():
        return jsonify(_run_ensure())

    # -- profili auth (write-only) ---------------------------------------------

    # stato dell'ultimo login interattivo (il flusso apre il browser sulla
    # macchina del server e attende il callback OAuth / la cattura cookie)
    login_state: dict = {"running": False, "profile": None,
                         "ok": None, "error": None, "result": None}

    @app.post("/auth/login")
    def auth_login():
        body = request.get_json(force=True)
        name = body.get("name", "")
        if not name:
            return jsonify({"error": "nome profilo mancante"}), 400
        if login_state["running"]:
            return jsonify({"error": "un login è già in corso",
                            "profile": login_state["profile"]}), 409

        def runner():
            try:
                login_state["result"] = auth_store.login(
                    name, wait_for_user=False,
                    capture_only=bool(body.get("capture")))
                login_state["ok"] = True
            except Exception as e:  # noqa: BLE001
                log.exception("login %s fallito", name)
                login_state.update(ok=False, error=str(e))
            finally:
                login_state["running"] = False

        login_state.update(running=True, profile=name, ok=None,
                           error=None, result=None)
        threading.Thread(target=runner, daemon=True,
                         name="wikibridge-auth-login").start()
        return jsonify({"started": True, "profile": name,
                        "note": "browser aperto sulla macchina del server; "
                                "stato su GET /auth/login/status"}), 202

    @app.get("/auth/login/status")
    def auth_login_status():
        return jsonify(login_state)

    @app.post("/auth/detect")
    def auth_detect():
        from .oauth import detect_protection
        body = request.get_json(force=True)
        try:
            return jsonify(detect_protection(body["url"], cfg.user_agent))
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": str(e)}), 502

    @app.get("/auth/profiles")
    def auth_list():
        return jsonify(auth_store.names())

    @app.post("/auth/profiles")
    def auth_set():
        body = request.get_json(force=True)
        name = body.pop("name", "")
        if not name:
            return jsonify({"error": "nome mancante"}), 400
        try:
            auth_store.set(name, body)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"saved": name}), 201

    @app.delete("/auth/profiles/<name>")
    def auth_delete(name):
        auth_store.remove(name)
        return jsonify({"removed": name})

    return app


def serve(cfg: Config) -> None:
    logging.basicConfig(level=logging.INFO)
    app = create_app(cfg)
    log.info("wikibridge API su http://%s:%d", cfg.api_host, cfg.api_port)
    app.run(host=cfg.api_host, port=cfg.api_port, threaded=True)
