"""Server HTTP di wikibridge (:8775) per la GUI Control Center.

Senza identity provider: pattern di control_server.py di callAPIcall,
Bearer token opzionale (api_auth_token) + CORS, single-user. Con identity
provider: sessione utente (POST /identity/login), chiave applicazione
separata in X-Wikibridge-Key, route di gestione riservate al ruolo admin.
Un solo job di import/ingest/update alla volta (409 se occupato).

I segreti dei profili auth sono WRITE-ONLY: la GET restituisce solo
nomi e tipi.
"""
from __future__ import annotations

import functools
import hmac
import json
import logging
import tempfile
import threading
from pathlib import Path

from flask import Flask, abort, g, jsonify, request
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix

from .auth import AuthProfileStore
from .catalog import drain_pending, make_catalog_hook
from .config import Config, REPO_DIR
from .convert.docs import ingest_path
from .entitlements import EntitlementCache
from .identity import LOGIN_FAILED, IdentityError, build_identity_provider
from . import acl_admin
from .acl import ADMIN_ROLE, LEVELS, Access, user_clearance
from .sessions import LoginThrottle, SessionStore
from .wiki.graph import filter_graph
from .wiki.segments import filter_body
from .wiki.writer import SECTION_TITLES
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


_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
# chiamabili senza sessione utente (in modalità identity)
_PUBLIC = {("GET", "/health"), ("POST", "/identity/login")}


def check_deployment(cfg: Config) -> None:
    """Rifiuta configurazioni che esporrebbero credenziali o token in
    chiaro sulla rete."""
    if (cfg.identity_provider != "none" and cfg.api_host not in _LOOPBACK
            and not (cfg.api_tls_cert and cfg.api_tls_key)
            and not cfg.behind_tls_proxy):
        raise ValueError(
            f"api_host={cfg.api_host} con identity_provider="
            f"{cfg.identity_provider} richiede TLS: imposta api_tls_cert/"
            "api_tls_key oppure behind_tls_proxy=true (reverse proxy HTTPS)")


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def require_admin(fn):
    """Route di gestione (scritture, import, profili, configurazione)."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not g.get("is_admin"):
            return jsonify({"error": "operazione riservata al ruolo admin"}), 403
        return fn(*args, **kwargs)
    return wrapper


def create_app(cfg: Config, *, identity_provider=None) -> Flask:
    check_deployment(cfg)
    app = Flask("wikibridge")
    identity_mode = cfg.identity_provider != "none"
    if not identity_mode:
        CORS(app)                     # uso locale single-user: come prima
    elif cfg.cors_origins:
        CORS(app, origins=cfg.cors_origins,
             allow_headers=["Authorization", "Content-Type", "X-Wikibridge-Key"])
    if cfg.behind_tls_proxy:
        # l'IP reale del client (per il limite dei login) arriva dal proxy
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
    store = ProjectStore(cfg)
    auth_store = AuthProfileStore(cfg)
    runner = JobRunner()

    identity_provider = identity_provider or build_identity_provider(cfg)
    entitlements = EntitlementCache(identity_provider,
                                    ttl_s=cfg.entitlement_cache_ttl_s)
    sessions = SessionStore(cfg.session_idle_min * 60, cfg.session_max_hours * 3600)
    throttle = LoginThrottle(cfg.login_max_failures_user,
                             cfg.login_max_failures_ip, cfg.login_window_s)

    if cfg.identity_provider == "none":
        for p in store.list():
            if p.acl_enforcement:
                log.warning(
                    "progetto %r ha acl_enforcement attivo ma "
                    "identity_provider=none: la ricerca resterà senza "
                    "filtro ACL finché non si configura un provider "
                    "(fail-closed non applicato per evitare di bloccare "
                    "tutti gli utenti per un errore di configurazione)",
                    p.slug)

    def bearer() -> str:
        header = request.headers.get("Authorization", "")
        return header[len("Bearer "):] if header.startswith("Bearer ") else ""

    @app.before_request
    def check_auth():
        """Due credenziali distinte, in due header distinti:
        - X-Wikibridge-Key: chiave dell'APPLICAZIONE client (api_auth_token);
        - Authorization: Bearer <token di sessione> dell'UTENTE.
        Senza identity provider resta il modello single-user di prima: il
        bearer è api_auth_token e chi lo presenta (o il loopback senza
        token) è admin."""
        if request.method == "OPTIONS":
            return None
        g.identity = None
        g.user_roles = None           # None = nessun filtro ACL
        g.is_admin = False
        g.session_token = ""
        if not identity_mode:
            token = request.headers.get("X-Wikibridge-Key") or bearer()
            if cfg.api_auth_token and not _same(token, cfg.api_auth_token):
                return jsonify({"error": "non autorizzato"}), 401
            g.is_admin = True
            return None

        public = (request.method, request.path) in _PUBLIC
        if cfg.api_auth_token and not _same(
                request.headers.get("X-Wikibridge-Key", ""), cfg.api_auth_token):
            if request.path != "/health":
                return jsonify({"error": "chiave applicazione mancante o errata"}), 401
            return None
        token = bearer()
        identity = sessions.get(token) if token else None
        if identity is None:
            if public:
                return None
            return jsonify({"error": "sessione assente o scaduta: "
                                     "POST /identity/login"}), 401
        g.identity = identity
        g.session_token = token
        g.user_roles = entitlements.roles_for(identity)
        g.is_admin = ADMIN_ROLE in g.user_roles
        return None

    def access(project) -> Access:
        return Access(project, principals=g.get("user_roles"),
                      is_admin=g.get("is_admin", False),
                      enforce=identity_mode and project.acl_enforcement,
                      default_clearance=cfg.default_clearance)

    def proj_arg():
        """Progetto della richiesta. 404 se non esiste o se il gate di
        progetto non lo rende visibile (stessa risposta: l'esistenza di un
        progetto riservato non trapela)."""
        slug = request.args.get("project")
        if not slug and request.is_json:
            slug = (request.get_json(silent=True) or {}).get("project")
        if not slug and request.form:
            slug = request.form.get("project")
        try:
            project = store.get(slug or None)
        except KeyError:
            abort(404)
        if not access(project).project_visible():
            abort(404)
        return project

    # -- base -------------------------------------------------------------

    @app.get("/health")
    def health():
        if not g.get("is_admin"):
            return jsonify({"ok": True})   # niente nomi di progetto o job
        return jsonify({"ok": True, "active_project": store.active_slug(),
                        "job": runner.state})

    # -- progetti -----------------------------------------------------------

    @app.get("/projects")
    def projects_list():
        active = store.active_slug()
        return jsonify([{**p.to_dict(), "active": p.slug == active}
                        for p in store.list() if access(p).project_visible()])

    @app.post("/projects")
    @require_admin
    def projects_create():
        body = request.get_json(force=True)
        try:
            p = store.create(body["name"], body.get("description", ""))
        except ValueError as e:
            return jsonify({"error": str(e)}), 409
        return jsonify(p.to_dict()), 201

    @app.delete("/projects/<slug>")
    @require_admin
    def projects_delete(slug):
        try:
            store.delete(slug)
        except (KeyError, ValueError) as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"deleted": slug})

    @app.post("/projects/<slug>/activate")
    @require_admin
    def projects_activate(slug):
        try:
            store.activate(slug)
        except KeyError as e:
            return jsonify({"error": str(e)}), 404
        return jsonify({"active": slug})

    @app.post("/projects/<slug>/acl")
    @require_admin
    def projects_set_acl(slug):
        body = request.get_json(force=True)
        if "enabled" not in body:
            return jsonify({"error": "campo 'enabled' mancante"}), 400
        try:
            p = store.set_acl_enforcement(slug, bool(body["enabled"]))
        except KeyError as e:
            return jsonify({"error": str(e)}), 404
        return jsonify(p.to_dict())

    # -- import -----------------------------------------------------------

    @app.post("/imports")
    @require_admin
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
    @require_admin
    def imports_list():
        project = proj_arg()
        return jsonify({"project": project.slug,
                        "job": runner.state,
                        "imports": ImportsLog(project).load()})

    @app.get("/imports/<import_id>")
    @require_admin
    def imports_get(import_id):
        project = proj_arg()
        try:
            return jsonify(ImportsLog(project).get(import_id))
        except KeyError as e:
            return jsonify({"error": str(e)}), 404

    @app.post("/imports/<import_id>/cancel")
    @require_admin
    def imports_cancel(import_id):
        runner.cancel.set()
        return jsonify({"cancelling": True})

    @app.post("/imports/<import_id>/sync")
    @require_admin
    def imports_sync(import_id):
        project = proj_arg()
        ok = runner.start("sync", sync_import, cfg, project, import_id,
                          catalog=make_catalog_hook(cfg))
        if not ok:
            return jsonify({"error": "un altro job è in corso"}), 409
        return jsonify({"started": True}), 202

    # -- controllo aggiornamenti (on-demand: nessuno scheduler interno) --------

    @app.get("/sources")
    @require_admin
    def sources_list():
        from .updater import list_sources
        project = proj_arg()
        return jsonify({"project": project.slug, "sources": list_sources(project)})

    @app.post("/update")
    @require_admin
    def update_start():
        """Body: {project?, imports?: [id], ingests?: [id], paths?: [file|dir],
        discover?: bool, dry_run?: bool, no_catalog?: bool}. Senza selettori
        controlla tutte le sorgenti del progetto. Esito su GET /health (job)."""
        from .updater import check_updates
        body = request.get_json(silent=True) or {}
        project = store.get(body.get("project"))
        catalog = None if body.get("no_catalog") or body.get("dry_run") \
            else make_catalog_hook(cfg)
        ok = runner.start(
            "update", check_updates, cfg, project,
            imports=body.get("imports"), ingests=body.get("ingests"),
            paths=body.get("paths"), discover=bool(body.get("discover", True)),
            dry_run=bool(body.get("dry_run", False)), catalog=catalog)
        if not ok:
            return jsonify({"error": "un altro job è in corso"}), 409
        return jsonify({"started": True, "project": project.slug}), 202

    # -- ingest documenti ----------------------------------------------------

    @app.post("/ingest")
    @require_admin
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
    @require_admin
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
    @require_admin
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

    def visible_tree(project, acc: Access) -> dict:
        tree = WikiWriter(project).tree()
        vis = acc.visible_pages(SearchIndex(project))
        if vis is None:
            return tree
        return {s: [e for e in entries if e["file"] in vis]
                for s, entries in tree.items()}

    @app.get("/wiki/tree")
    def wiki_tree():
        project = proj_arg()
        return jsonify({"project": project.slug,
                        "tree": visible_tree(project, access(project))})

    @app.get("/wiki/page")
    def wiki_page():
        """Con le ACL attive: source solo se visibili; entità/concetti
        ridotti ai segmenti che derivano da source visibili (il testo
        legacy non attribuibile resta all'admin); index.md generato per
        l'utente; overview, log, riunioni e sintesi solo all'admin. Una
        pagina non visibile risponde 404, come una inesistente."""
        project = proj_arg()
        rel = request.args.get("path", "")
        acc = access(project)
        not_found = (jsonify({"error": f"pagina non trovata: {rel}"}), 404)
        if not acc.unrestricted and rel == "index.md":
            lines = [f"# Indice — {project.name}", ""]
            for section, entries in visible_tree(project, acc).items():
                if entries:
                    lines += [f"## {SECTION_TITLES[section]}", ""]
                    lines += [f"- [{e['title']}]({e['file']})" for e in entries]
                    lines.append("")
            return jsonify({"path": rel, "frontmatter": {}, "body": "\n".join(lines)})
        try:
            fm, body = WikiWriter(project).read_page(rel)
        except (OSError, ValueError):      # inesistente, cartella, fuori dalla wiki
            return not_found
        if acc.unrestricted:
            return jsonify({"path": rel, "frontmatter": fm, "body": body})
        vis = acc.visible_pages(SearchIndex(project))
        if rel not in vis:
            return not_found
        if rel.split("/", 1)[0] in ("entities", "concepts"):
            sources = {p for p in vis if p.startswith("sources/")}
            body = filter_body(fm.get("title", rel), body, sources)
            if body is None:
                return not_found
            fm = {**fm, "mentions": [m for m in fm.get("mentions", []) if m in sources]}
        return jsonify({"path": rel, "frontmatter": fm, "body": body})

    @app.post("/wiki/refresh")
    @require_admin
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
        index = SearchIndex(project)
        try:
            limit = max(1, min(int(request.args.get("limit", 20)), 100))
        except ValueError:
            limit = 20
        hits = index.query(q, limit=limit, acl_ids=access(project).acl_ids(index))
        return jsonify({"project": project.slug, "hits": hits})

    @app.get("/graph")
    def graph_get():
        project = proj_arg()
        stats = build_graph(project)
        data = json.loads((project.graph_dir / "graph.json")
                          .read_text(encoding="utf-8"))
        acc = access(project)
        if acc.unrestricted:
            return jsonify({**stats, "graph": data,
                            "html_path": str(project.graph_dir / "graph.html")})
        vis = acc.visible_pages(SearchIndex(project))
        data = filter_graph(data, vis, {p for p in vis if p.startswith("sources/")})
        return jsonify({"nodes": len(data["nodes"]), "edges": len(data["edges"]),
                        "graph": data})

    @app.post("/catalog/drain")
    @require_admin
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
    @require_admin
    def skill_status():
        return jsonify(_run_ensure())

    @app.post("/skill/install")
    @require_admin
    def skill_install():
        return jsonify(_run_ensure())

    # -- profili auth (write-only) ---------------------------------------------

    # stato dell'ultimo login interattivo (il flusso apre il browser sulla
    # macchina del server e attende il callback OAuth / la cattura cookie)
    login_state: dict = {"running": False, "profile": None,
                         "ok": None, "error": None, "result": None}

    @app.post("/auth/login")
    @require_admin
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
    @require_admin
    def auth_login_status():
        return jsonify(login_state)

    @app.post("/auth/detect")
    @require_admin
    def auth_detect():
        from .oauth import detect_protection
        body = request.get_json(force=True)
        try:
            return jsonify(detect_protection(body["url"], cfg.user_agent))
        except Exception as e:  # noqa: BLE001
            return jsonify({"error": str(e)}), 502

    @app.get("/auth/profiles")
    @require_admin
    def auth_list():
        return jsonify(auth_store.names())

    @app.post("/auth/profiles")
    @require_admin
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
    @require_admin
    def auth_delete(name):
        auth_store.remove(name)
        return jsonify({"removed": name})

    # -- identità dell'utente che interroga l'API (per il filtro ACL) ----------

    @app.post("/identity/login")
    def identity_login():
        """Body {username, password} → {token, expires_in, roles}. Unico
        messaggio d'errore per ogni fallimento; troppi tentativi → 429."""
        if not identity_mode:
            return jsonify({"error": "nessun identity provider configurato"}), 400
        body = request.get_json(silent=True) or {}
        username = str(body.get("username", "")).strip()
        password = str(body.get("password", ""))
        ip = request.remote_addr or "?"
        wait = throttle.retry_after(username, ip)
        if wait:
            return (jsonify({"error": "troppi tentativi, riprova più tardi"}),
                    429, {"Retry-After": str(wait)})
        try:
            identity = identity_provider.login(username, password)
        except IdentityError:
            throttle.failure(username, ip)
            return jsonify({"error": LOGIN_FAILED}), 401
        throttle.success(username)
        token = sessions.create(identity)
        roles = entitlements.roles_for(identity)
        return jsonify({"token": token, "expires_in": sessions.idle_s,
                        "subject": identity.subject,
                        "display_name": identity.display_name,
                        "roles": sorted(roles)})

    @app.post("/identity/logout")
    def identity_logout():
        if g.session_token:
            sessions.revoke(g.session_token)
        return jsonify({"logged_out": True})

    @app.get("/identity/whoami")
    def identity_whoami():
        if g.identity is None:
            return jsonify({"identity_provider": cfg.identity_provider,
                            "authenticated": False, "admin": g.is_admin,
                            "roles": []})
        clearance = user_clearance(frozenset(g.user_roles or ()), cfg.default_clearance)
        return jsonify({"identity_provider": cfg.identity_provider,
                        "authenticated": True, "admin": g.is_admin,
                        "subject": g.identity.subject,
                        "display_name": g.identity.display_name,
                        "principal": g.identity.principal,
                        "clearance": next(k for k, v in LEVELS.items() if v == clearance),
                        "roles": sorted(g.user_roles or [])})

    # -- amministrazione ACL -------------------------------------------------------

    def _principals(body: dict) -> list[str]:
        return acl_admin.principals_from(body.get("principals"), body.get("users"),
                                         identity_provider if identity_mode else None)

    @app.get("/acl")
    @require_admin
    def acl_show():
        project = proj_arg()
        return jsonify(acl_admin.show(project, request.args.get("path")))

    @app.put("/acl/page")
    @require_admin
    def acl_page():
        """Body {project?, path, principals?, users?, classification?}:
        sostituisce i principal propri del documento."""
        body = request.get_json(force=True)
        project = proj_arg()
        try:
            return jsonify(acl_admin.set_page_acl(
                project, body["path"], _principals(body), body.get("classification")))
        except (KeyError, ValueError, FileNotFoundError, IdentityError) as e:
            return jsonify({"error": str(e)}), 400

    @app.put("/acl/collections/<coll_id>")
    @require_admin
    def acl_collection_set(coll_id):
        """Body {project?, principals?, users?, classification?, sources: [id]}."""
        body = request.get_json(force=True)
        project = proj_arg()
        try:
            return jsonify(acl_admin.set_collection(
                project, coll_id, principals=_principals(body),
                classification=body.get("classification", "internal"),
                sources=body.get("sources", [])))
        except (ValueError, IdentityError) as e:
            return jsonify({"error": str(e)}), 400

    @app.delete("/acl/collections/<coll_id>")
    @require_admin
    def acl_collection_delete(coll_id):
        try:
            return jsonify(acl_admin.delete_collection(proj_arg(), coll_id))
        except KeyError as e:
            return jsonify({"error": str(e)}), 404

    @app.put("/acl/gate")
    @require_admin
    def acl_gate():
        """Body {project?, principals?, users?}: vuoto = nessun gate."""
        body = request.get_json(force=True)
        try:
            return jsonify(acl_admin.set_gate(proj_arg(), _principals(body)))
        except (ValueError, IdentityError) as e:
            return jsonify({"error": str(e)}), 400

    @app.post("/identity/refresh")
    def identity_refresh():
        """Bust della cache dei ruoli per l'utente corrente: i permessi
        appena ottenuti valgono subito, senza aspettare il TTL."""
        if g.identity is None:
            return jsonify({"error": "nessuna identità autenticata"}), 400
        entitlements.invalidate(g.identity.subject)
        roles = entitlements.roles_for(g.identity)
        return jsonify({"subject": g.identity.subject, "roles": sorted(roles)})

    return app


def serve(cfg: Config) -> None:
    logging.basicConfig(level=logging.INFO)
    app = create_app(cfg)
    if cfg.api_tls_cert and cfg.api_tls_key:
        log.info("wikibridge API su https://%s:%d", cfg.api_host, cfg.api_port)
        app.run(host=cfg.api_host, port=cfg.api_port, threaded=True,
                ssl_context=(cfg.api_tls_cert, cfg.api_tls_key))
        return
    log.info("wikibridge API su http://%s:%d", cfg.api_host, cfg.api_port)
    try:
        from waitress import serve as waitress_serve
    except ImportError:
        app.run(host=cfg.api_host, port=cfg.api_port, threaded=True)
        return
    waitress_serve(app, host=cfg.api_host, port=cfg.api_port, threads=8)
