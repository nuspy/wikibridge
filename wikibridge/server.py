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
from datetime import datetime, timezone
from pathlib import Path

from urllib.parse import urlencode, urlparse

from flask import Flask, abort, g, jsonify, redirect, request
from markupsafe import escape
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix

from .auth import AuthProfileStore
from .catalog import drain_pending, make_catalog_hook
from .config import Config, REPO_DIR
from .convert.docs import ingest_path
from .entitlements import EntitlementCache
from .identity import (LOGIN_FAILED, AccountInactive, IdentityError,
                       build_identity_provider)
from . import acl_admin
from .acl import ADMIN_ROLE, LEVELS, Access, user_clearance
from .audit import AuditLog
from .rag import build_context, intersect
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
_PUBLIC = {("GET", "/health"), ("POST", "/identity/login"),
           ("GET", "/identity/oidc/login"), ("GET", "/identity/oidc/callback"),
           ("POST", "/identity/oidc/token"),
           ("GET", "/identity/saml/login"), ("POST", "/identity/saml/acs"),
           ("GET", "/identity/saml/metadata")}
# navigazione del browser durante il login SSO: non può portare header
# personalizzati, quindi è esente dalla chiave applicazione (il token di
# sessione viene consegnato solo a un return_to ammesso)
_BROWSER_FLOW = {"/identity/oidc/login", "/identity/oidc/callback",
                 "/identity/saml/login", "/identity/saml/acs",
                 "/identity/saml/metadata"}
_SSO_LOGIN = {"oidc": "/identity/oidc/login", "saml": "/identity/saml/login"}


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
    audit = AuditLog(cfg)

    def audit_event(action: str, **fields) -> None:
        """Evento di audit con chi (utente della sessione o "local" in
        single-user) e da dove. Segna la richiesta come già registrata."""
        ident = g.get("identity")
        fields.setdefault("user", ident.subject if ident else
                          (None if identity_mode else "local"))
        audit.write(action, ip=request.remote_addr, **fields)
        g.audited = True

    @app.after_request
    def audit_writes(resp):
        """Ogni richiesta che modifica qualcosa e che non ha già un evento
        dedicato: metodo, percorso, esito. Mai il corpo (password, segreti)."""
        if (request.method not in ("GET", "HEAD", "OPTIONS")
                and not g.get("audited")):
            audit_event("api", method=request.method, path=request.path,
                        status=resp.status_code,
                        project=request.args.get("project"))
        return resp

    def expire_claims(subject: str) -> None:
        """OIDC: i gruppi vanno riletti dall'IdP, non dal token in memoria."""
        fn = getattr(identity_provider, "expire_claims", None)
        if fn:
            fn(subject)

    # -- modifiche nel directory: revoche e nuovi permessi prima del TTL ------
    sync_state: dict = {"last_check": None, "last_change": None,
                        "cleared": 0, "error": None}
    role_map_missing: list[str] = []

    def sync_groups() -> dict:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            changed = identity_provider.groups_changed()
        except IdentityError as e:
            sync_state.update(last_check=now, error=str(e))
            log.warning("controllo modifiche del directory fallito: %s", e)
            return {"changed": False, "error": str(e)}
        sync_state.update(last_check=now, error=None)
        if changed:
            sync_state.update(last_change=now, cleared=entitlements.clear())
            log.info("modifiche nel directory: ruoli in cache invalidati (%d)",
                     sync_state["cleared"])
        return {"changed": changed, "cleared": sync_state["cleared"] if changed else 0}

    deployment_warnings: list[str] = []
    if identity_mode:
        # in multi-utente chi legge data/ dal disco scavalca le ACL
        from .ntfs_acl import data_dir_warnings
        deployment_warnings[:] = data_dir_warnings(cfg.data_dir)
        for w in deployment_warnings:
            log.warning("deployment: %s", w)
        sync_groups()                          # fissa il punto di partenza
        try:
            role_map_missing[:] = identity_provider.check_role_map()
        except IdentityError as e:
            log.warning("verifica identity_role_map non riuscita: %s", e)
        for dn in role_map_missing:
            log.warning("identity_role_map: il gruppo %r non esiste nel directory "
                        "(ruolo %r mai assegnato)", dn, cfg.identity_role_map.get(dn))
        if cfg.ldap_change_poll_s > 0:
            def poll_loop(stop: threading.Event):
                while not stop.wait(cfg.ldap_change_poll_s):
                    sync_groups()
            app.config["wb_poll_stop"] = stop = threading.Event()
            threading.Thread(target=poll_loop, args=(stop,), daemon=True,
                             name="wikibridge-ldap-poll").start()

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
        if cfg.api_auth_token and request.path not in _BROWSER_FLOW and not _same(
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
        try:
            roles = entitlements.roles_for(identity)
        except AccountInactive as e:
            sessions.revoke_subject(identity.subject)
            entitlements.invalidate(identity.subject)
            log.info("sessioni revocate per %s: account non più attivo",
                     identity.subject)
            audit_event("session_revoked", user=identity.subject, reason=str(e))
            return jsonify({"error": "account non più attivo: sessione revocata"}), 401
        except IdentityError:
            # directory irraggiungibile: niente ruoli = niente accesso, ma
            # senza disconnettere nessuno (errore temporaneo)
            return jsonify({"error": "directory non raggiungibile, riprova"}), 503
        g.identity = identity
        g.session_token = token
        g.user_roles = roles
        g.is_admin = ADMIN_ROLE in roles
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
        # "multi_user" dice ai client se la conoscenza va chiesta via API
        # (filtrata) invece di leggere la wiki dal disco
        login = {"ldap": "password", "oidc": "sso",
                 "saml": "sso"}.get(cfg.identity_provider)
        info = {"ok": True, "multi_user": identity_mode, "login": login}
        if login == "sso":
            info["sso_login"] = _SSO_LOGIN[cfg.identity_provider]
        if not g.get("is_admin"):
            return jsonify(info)
        return jsonify({**info, "active_project": store.active_slug(),
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
        """Body {enabled, dry_run?, confirm?}. Spegnere il filtro rende tutto
        visibile a chiunque entri nel progetto: anteprima e conferma."""
        body = request.get_json(force=True)
        if "enabled" not in body:
            return jsonify({"error": "campo 'enabled' mancante"}), 400
        try:
            preview = acl_admin.preview_enforcement(store.get(slug), bool(body["enabled"]))
        except KeyError as e:
            return jsonify({"error": str(e)}), 404
        if body.get("dry_run"):
            return jsonify({"preview": preview})
        blocked = needs_confirm(preview, body)
        if blocked:
            return blocked
        p = store.set_acl_enforcement(slug, bool(body["enabled"]))
        audit_event("acl_enforcement", project=slug, preview=preview)
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
            adapter_name=body.get("adapter", "auto"), catalog=catalog,
            permissions=bool(body.get("permissions", False)))
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
            dry_run=bool(body.get("dry_run", False)), catalog=catalog,
            permissions=bool(body.get("permissions", False)))
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
                          catalog=make_catalog_hook(cfg),
                          permissions=bool(body.get("permissions", False)))
        if not ok:
            return jsonify({"error": "un altro job è in corso"}), 409
        return jsonify({"started": True}), 202

    # -- riunioni → wiki -------------------------------------------------------

    def resolve_participants(names: list[str]) -> tuple[list, list]:
        """(identità riconosciute, nomi non riconosciuti) dal directory."""
        found, unknown = [], []
        for name in names[:50]:
            try:
                found.append(identity_provider.find_user(name))
            except IdentityError:
                unknown.append(name)
        return found, unknown

    @app.post("/meetings")
    def meetings_push():
        """Qualunque utente autenticato che vede il progetto può salvarci una
        riunione. Con un identity provider la pagina è visibile a chi l'ha
        salvata e ai partecipanti riconosciuti dal directory (più l'admin)."""
        body = request.get_json(force=True)
        project = proj_arg()
        participants = [str(p) for p in body.get("participants", []) if str(p).strip()]
        acl_roles: list[str] = []
        if identity_mode and g.identity is not None:
            found, _ = resolve_participants(participants)
            acl_roles = [g.identity.principal] + [i.principal for i in found]
        writer = WikiWriter(project)
        path = writer.write_meeting(
            meeting_id=body.get("meeting_id", ""),
            title=body.get("title") or "Riunione",
            date=body.get("date", ""),
            source=body.get("source", "meetbot"),
            participants=participants,
            summary=body.get("summary", ""),
            highlights=body.get("highlights", []),
            agreements=body.get("agreements", []),
            tasks=body.get("tasks", []),
            transcript_path=body.get("transcript_path", ""),
            acl_roles=acl_roles)
        rel = f"meetings/{path.name}"
        fm, page_body = writer.read_page(rel)
        SearchIndex(project).upsert_page(rel, "meetings", fm, page_body)
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

        def seen(outcome: str):
            # "denied" = esiste ma non è visibile (al client risponde 404
            # come una pagina inesistente: la distinzione resta nell'audit)
            audit_event("page", project=project.slug, path=rel, outcome=outcome)

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
            seen("not_found")
            return not_found
        if acc.unrestricted:
            seen("allowed")
            return jsonify({"path": rel, "frontmatter": fm, "body": body})
        vis = acc.visible_pages(SearchIndex(project))
        if rel not in vis:
            seen("denied")
            return not_found
        if rel.split("/", 1)[0] in ("entities", "concepts"):
            sources = {p for p in vis if p.startswith("sources/")}
            body = filter_body(fm.get("title", rel), body, sources)
            if body is None:
                seen("denied")
                return not_found
            fm = {**fm, "mentions": [m for m in fm.get("mentions", []) if m in sources]}
        seen("allowed")
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
        acl_ids = access(project).acl_ids(index)
        hits = index.query(q, limit=limit, acl_ids=acl_ids)
        audit_event("search", project=project.slug, **audit.query_field(q),
                    filtered=acl_ids is not None, paths=[h["path"] for h in hits])
        return jsonify({"project": project.slug, "hits": hits})

    def history_allowed(project, rel: str) -> bool:
        """Lo storico di un documento si vede con le ACL ATTUALI del
        documento (mai con quelle che aveva una versione vecchia: sarebbe un
        leak). Un documento cancellato non ha più ACL: solo l'admin."""
        acc = access(project)
        if acc.unrestricted:
            return True
        vis = acc.visible_pages(SearchIndex(project))
        return rel in vis

    @app.get("/history")
    def history_versions():
        """?path=: tutte le versioni dei chunk del documento."""
        project = proj_arg()
        rel = request.args.get("path", "")
        index = SearchIndex(project)
        if not history_allowed(project, rel) or not index.has_history(rel):
            audit_event("history", project=project.slug, path=rel, outcome="denied")
            return jsonify({"error": f"storico non trovato: {rel}"}), 404
        audit_event("history", project=project.slug, path=rel, outcome="allowed")
        return jsonify({"path": rel, "versions": index.history(rel)})

    @app.get("/history/document")
    def history_document():
        """?path=&at=ISO: il documento com'era in quel momento."""
        project = proj_arg()
        rel, at = request.args.get("path", ""), request.args.get("at", "")
        if not at:
            return jsonify({"error": "parametro at (data ISO) mancante"}), 400
        index = SearchIndex(project)
        if not history_allowed(project, rel) or not index.has_history(rel):
            audit_event("history", project=project.slug, path=rel, at=at,
                        outcome="denied")
            return jsonify({"error": f"storico non trovato: {rel}"}), 404
        try:
            doc = index.document_at(rel, at)
        except ValueError:
            return jsonify({"error": "data non valida (formato ISO, es. 2026-09-01)"}), 400
        audit_event("history", project=project.slug, path=rel, at=at, outcome="allowed")
        return jsonify(doc)

    @app.post("/syntheses")
    def syntheses_save():
        """Body {project?, title, body, sources: [path]}: salva una sintesi
        che, con le ACL attive, vede solo chi vede tutte le sue fonti. Chi la
        scrive deve poter vedere ogni fonte dichiarata (altrimenti 400, senza
        dire quali esistono)."""
        body = request.get_json(force=True)
        project = proj_arg()
        title = str(body.get("title", "")).strip()
        sources = [str(s) for s in body.get("sources") or []]
        if not title or not sources:
            return jsonify({"error": "title e sources sono obbligatori"}), 400
        vis = access(project).visible_pages(SearchIndex(project))
        if vis is not None and not set(sources) <= vis:
            return jsonify({"error": "una o più fonti non sono accessibili"}), 400
        writer = WikiWriter(project)
        try:
            path = writer.write_synthesis(title=title, body=str(body.get("body", "")),
                                          sources=sources)
        except (ValueError, OSError) as e:
            return jsonify({"error": str(e)}), 400
        rel = f"syntheses/{path.name}"
        fm, page_body = writer.read_page(rel)
        SearchIndex(project).upsert_page(rel, "syntheses", fm, page_body)
        audit_event("synthesis", project=project.slug, path=rel, paths=sources)
        return jsonify({"path": rel, "derived_from": fm["derived_from"]}), 201

    @app.post("/rag/context")
    def rag_context():
        """Contesto RAG per l'inferenza, già filtrato per chi lo chiede.
        Body {project?, query, limit?, max_chars?, participants?, unresolved?}:
        con i partecipanti (username o email) il contesto è l'intersezione di
        ciò che vedono tutti. unresolved: "public" (default, per ciò che viene
        detto a tutti i presenti: un non riconosciuto riduce al solo public)
        o "ignore" (per ciò che vedranno solo i riconosciuti, es. la pagina
        della riunione)."""
        body = request.get_json(force=True)
        project = proj_arg()
        query = str(body.get("query", "")).strip()
        if not query:
            return jsonify({"error": "query mancante"}), 400
        try:
            limit = max(1, min(int(body.get("limit", 8)), 30))
            max_chars = max(500, min(int(body.get("max_chars", 12000)), 100_000))
        except (TypeError, ValueError):
            return jsonify({"error": "limit/max_chars non validi"}), 400
        index = SearchIndex(project)
        acc = access(project)
        sets = [acc.acl_ids(index)]
        participants = [str(p) for p in body.get("participants") or [] if str(p).strip()]
        resolved, unresolved = [], []
        if participants and acc.enforce:
            def ids_for(principals: frozenset[str]):
                return Access(project, principals=principals,
                              is_admin=ADMIN_ROLE in principals, enforce=True,
                              default_clearance=cfg.default_clearance).acl_ids(index)
            found, unresolved = resolve_participants(participants)
            for ident in found:
                sets.append(ids_for(entitlements.roles_for(ident)))
                resolved.append(ident.username)
            if unresolved and body.get("unresolved", "public") != "ignore":
                sets.append(ids_for(frozenset()))       # solo contenuti public
        ctx = build_context(project, index, intersect(sets), query,
                            limit=limit, max_chars=max_chars)
        policy = ("acl non attive" if not acc.enforce else
                  "intersezione partecipanti" if participants else "utente")
        audit_event("rag_context", project=project.slug, **audit.query_field(query),
                    policy=policy, participants_resolved=resolved,
                    participants_unresolved=len(unresolved),
                    paths=[b["path"] for b in ctx["blocks"]])
        return jsonify({"project": project.slug, "policy": policy,
                        "participants": {"resolved": resolved,
                                         "unresolved": unresolved}, **ctx})

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
        if cfg.identity_provider in _SSO_LOGIN:
            return jsonify({"error": "login via SSO: GET "
                            f"{_SSO_LOGIN[cfg.identity_provider]}"}), 400
        body = request.get_json(silent=True) or {}
        username = str(body.get("username", "")).strip()
        password = str(body.get("password", ""))
        ip = request.remote_addr or "?"
        wait = throttle.retry_after(username, ip)
        if wait:
            audit_event("login_throttled", username=username)
            return (jsonify({"error": "troppi tentativi, riprova più tardi"}),
                    429, {"Retry-After": str(wait)})
        try:
            identity = identity_provider.login(username, password)
            entitlements.invalidate(identity.subject)   # ruoli freschi a ogni login
            roles = entitlements.roles_for(identity)
        except IdentityError:
            throttle.failure(username, ip)
            audit_event("login_failed", username=username)
            return jsonify({"error": LOGIN_FAILED}), 401
        throttle.success(username)
        token = sessions.create(identity)
        audit_event("login", user=identity.subject, method="password",
                    roles=sorted(roles))
        return jsonify({"token": token, "expires_in": sessions.idle_s,
                        "subject": identity.subject,
                        "display_name": identity.display_name,
                        "roles": sorted(roles)})

    # -- SSO OIDC --------------------------------------------------------------

    def _page(title: str, text: str, status: int, token: str = ""):
        body = (f"<!doctype html><html lang='it'><head><meta charset='utf-8'>"
                f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
                f"<title>wikibridge</title></head><body style='font-family:sans-serif;"
                f"max-width:40rem;margin:2rem auto;padding:0 1rem;line-height:1.5'>"
                f"<h1 style='font-size:1.4rem'>{escape(title)}</h1><p>{escape(text)}</p>"
                + (f"<p>Token di sessione:</p><code style='word-break:break-all'>"
                   f"{escape(token)}</code>" if token else "")
                + "</body></html>")
        return body, status, {"Content-Type": "text/html; charset=utf-8",
                              "Cache-Control": "no-store"}

    def _open_session(identity) -> str:
        entitlements.invalidate(identity.subject)       # ruoli freschi
        entitlements.roles_for(identity)
        return sessions.create(identity)

    @app.get("/identity/oidc/login")
    def oidc_login():
        if cfg.identity_provider != "oidc":
            abort(404)
        try:
            url = identity_provider.authorization_url(request.args.get("return_to", ""))
        except IdentityError as e:
            return _page("Login non avviato", str(e), 400)
        return redirect(url)

    @app.get("/identity/oidc/callback")
    def oidc_callback():
        if cfg.identity_provider != "oidc":
            abort(404)
        if request.args.get("error"):
            return _page("Login non riuscito",
                         request.args.get("error_description") or request.args["error"], 400)
        try:
            identity, return_to = identity_provider.complete(
                request.args.get("code", ""), request.args.get("state", ""))
            token = _open_session(identity)
        except IdentityError as e:
            log.info("login SSO fallito: %s", e)
            audit_event("login_failed", method="sso", reason=str(e)[:200])
            return _page("Login non riuscito", LOGIN_FAILED, 401)
        audit_event("login", user=identity.subject, method="sso")
        return _deliver(token, return_to)

    def _deliver(token: str, return_to: str):
        """Consegna il token di sessione alla fine di un login SSO."""
        if not return_to:
            return _page("Login completato", "Puoi chiudere questa scheda.", 200, token)
        params = urlencode({"token": token, "expires_in": sessions.idle_s})
        # loopback (app locale): in query, la legge il suo mini-server;
        # altrove nel fragment, che non arriva ai server né ai loro log
        loopback = urlparse(return_to).hostname in _LOOPBACK
        sep = ("&" if "?" in return_to else "?") if loopback else "#"
        resp = redirect(f"{return_to}{sep}{params}")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    # -- SSO SAML --------------------------------------------------------------

    @app.get("/identity/saml/login")
    def saml_login():
        if cfg.identity_provider != "saml":
            abort(404)
        try:
            url = identity_provider.authorization_url(request.args.get("return_to", ""))
        except IdentityError as e:
            return _page("Login non avviato", str(e), 400)
        return redirect(url)

    @app.post("/identity/saml/acs")
    def saml_acs():
        """L'IdP consegna qui (POST dal browser) la risposta firmata."""
        if cfg.identity_provider != "saml":
            abort(404)
        try:
            identity, return_to = identity_provider.complete(request.form.to_dict())
            token = _open_session(identity)
        except IdentityError as e:
            log.info("login SAML fallito: %s", e)
            audit_event("login_failed", method="saml", reason=str(e)[:200])
            return _page("Login non riuscito", LOGIN_FAILED, 401)
        audit_event("login", user=identity.subject, method="saml")
        # 303: il browser segue il redirect con un GET, senza ripostare il form
        resp = _deliver(token, return_to)
        if not isinstance(resp, tuple):          # redirect, non la pagina col token
            resp.status_code = 303
        return resp

    @app.get("/identity/saml/metadata")
    def saml_metadata():
        if cfg.identity_provider != "saml":
            abort(404)
        return identity_provider.sp_metadata(), 200, {
            "Content-Type": "application/samlmetadata+xml"}

    @app.post("/identity/oidc/token")
    def oidc_token_exchange():
        """Body {id_token, refresh_token?}: per i client che hanno già fatto
        l'SSO con lo stesso client_id."""
        if cfg.identity_provider != "oidc":
            abort(404)
        body = request.get_json(silent=True) or {}
        try:
            identity = identity_provider.from_tokens(str(body.get("id_token", "")),
                                                     body.get("refresh_token"))
            token = _open_session(identity)
        except IdentityError as e:
            log.info("scambio id_token rifiutato: %s", e)
            audit_event("login_failed", method="id_token", reason=str(e)[:200])
            return jsonify({"error": LOGIN_FAILED}), 401
        audit_event("login", user=identity.subject, method="id_token")
        return jsonify({"token": token, "expires_in": sessions.idle_s,
                        "subject": identity.subject,
                        "display_name": identity.display_name,
                        "roles": sorted(entitlements.roles_for(identity))})

    @app.post("/identity/logout")
    def identity_logout():
        if g.session_token:
            sessions.revoke(g.session_token)
        audit_event("logout")
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

    def _principals(body: dict, labels: dict | None = None) -> list[str]:
        return acl_admin.principals_from(body.get("principals"), body.get("users"),
                                         identity_provider if identity_mode else None,
                                         labels)

    @app.get("/acl")
    @require_admin
    def acl_show():
        """Politica del progetto (e del documento con ?path=), con i nomi
        leggibili delle persone e i ruoli che la GUI può proporre."""
        project = proj_arg()
        known = [*(cfg.identity_role_map or {}).values(),
                 *(cfg.source_group_map or {}).values()]
        try:
            return jsonify(acl_admin.show(project, request.args.get("path"), known))
        except (OSError, ValueError) as e:
            return jsonify({"error": f"documento non leggibile: {e}"}), 404

    @app.put("/acl/page")
    @require_admin
    def acl_page():
        """Body {project?, path, principals?, users?, classification?,
        dry_run?, confirm?}: sostituisce i principal propri del documento."""
        body = request.get_json(force=True)
        project = proj_arg()
        labels: dict = {}
        try:
            principals = _principals(body, labels)
            preview = with_labels(acl_admin.preview_page(
                project, body["path"], principals, body.get("classification")), labels)
            if body.get("dry_run"):
                return jsonify({"preview": preview})
            blocked = needs_confirm(preview, body)
            if blocked:
                return blocked
            result = acl_admin.set_page_acl(
                project, body["path"], principals, body.get("classification"),
                labels=labels)
        except (KeyError, ValueError, OSError, IdentityError) as e:
            return jsonify({"error": str(e)}), 400
        audit_event("acl_page", project=project.slug, preview=preview, **result)
        return jsonify({**result, "preview": preview})

    def with_labels(preview: dict, labels: dict) -> dict:
        """Nomi leggibili di chi compare nell'anteprima: le persone appena
        indicate per username non sono ancora salvate in acl.json."""
        project = proj_arg()
        known = {**acl_admin.AclPolicy.load(project).labels, **labels}
        shown = set(preview.get("added_principals", [])) | set(
            preview.get("removed_principals", []))
        preview["labels"] = {p: known[p] for p in shown if p in known}
        return preview

    def needs_confirm(preview: dict, body: dict):
        """Chi allarga l'accesso deve vedere prima l'effetto: 409 con
        l'anteprima finché non ripete la richiesta con "confirm": true."""
        if preview["widening"] and not body.get("confirm"):
            return jsonify({"error": "la modifica allarga l'accesso: ripeti con "
                                     "\"confirm\": true", "preview": preview}), 409
        return None

    @app.put("/acl/collections/<coll_id>")
    @require_admin
    def acl_collection_set(coll_id):
        """Body {project?, principals?, users?, classification?, sources: [id],
        confirm?}. "dry_run": true restituisce solo l'anteprima."""
        body = request.get_json(force=True)
        project = proj_arg()
        labels: dict = {}
        try:
            args = dict(principals=_principals(body, labels),
                        classification=body.get("classification", "internal"),
                        sources=body.get("sources", []))
            preview = with_labels(
                acl_admin.preview_collection(project, coll_id, **args), labels)
            if body.get("dry_run"):
                return jsonify({"preview": preview})
            blocked = needs_confirm(preview, body)
            if blocked:
                return blocked
            result = acl_admin.set_collection(project, coll_id, **args, labels=labels)
        except (ValueError, IdentityError) as e:
            return jsonify({"error": str(e)}), 400
        audit_event("acl_collection", project=project.slug, collection=coll_id,
                    preview=preview)
        return jsonify({**result, "preview": preview})

    @app.delete("/acl/collections/<coll_id>")
    @require_admin
    def acl_collection_delete(coll_id):
        """Eliminare una collezione toglie una restrizione ai suoi documenti:
        anteprima e conferma come per le modifiche che allargano."""
        body = request.get_json(silent=True) or {}
        project = proj_arg()
        preview = with_labels(acl_admin.preview_collection(
            project, coll_id, principals=[], classification="internal", sources=[]), {})
        if not preview["exists"]:
            return jsonify({"error": f"collezione inesistente: {coll_id}"}), 404
        blocked = needs_confirm(preview, body)
        if blocked:
            return blocked
        result = acl_admin.delete_collection(project, coll_id)
        audit_event("acl_collection_delete", project=project.slug,
                    collection=coll_id, preview=preview)
        return jsonify(result)

    @app.put("/acl/gate")
    @require_admin
    def acl_gate():
        """Body {project?, principals?, users?, dry_run?, confirm?}: vuoto =
        nessun gate."""
        body = request.get_json(force=True)
        project = proj_arg()
        labels: dict = {}
        try:
            principals = _principals(body, labels)
            preview = with_labels(acl_admin.preview_gate(project, principals), labels)
            if body.get("dry_run"):
                return jsonify({"preview": preview})
            blocked = needs_confirm(preview, body)
            if blocked:
                return blocked
            result = acl_admin.set_gate(project, principals, labels=labels)
        except (ValueError, IdentityError) as e:
            return jsonify({"error": str(e)}), 400
        audit_event("acl_gate", project=project.slug, preview=preview)
        return jsonify(result)

    # -- audit ---------------------------------------------------------------------

    @app.get("/audit")
    @require_admin
    def audit_search():
        """?since=ISO&user=&action=&path=&limit=: eventi più recenti prima.
        path: chi ha visto (o provato ad aprire) quel documento."""
        a = request.args
        try:
            limit = max(1, min(int(a.get("limit", 200)), 5000))
        except ValueError:
            limit = 200
        return jsonify({"events": audit.search(
            since=a.get("since", ""), user=a.get("user", ""),
            action=a.get("action", ""), path=a.get("path", ""), limit=limit)})

    @app.get("/audit/verify")
    @require_admin
    def audit_verify():
        return jsonify(audit.verify())

    @app.post("/identity/invalidate")
    @require_admin
    def identity_invalidate():
        """Body {username?, revoke_sessions?}: con username invalida i ruoli
        in cache di quell'utente (e ne chiude le sessioni, es. dipendente
        uscito); senza, invalida i ruoli di tutti."""
        body = request.get_json(silent=True) or {}
        username = str(body.get("username", "")).strip()
        if not username:
            n = entitlements.clear()
            identity_provider.clear_cache()
            return jsonify({"invalidated": n, "scope": "tutti"})
        try:
            ident = identity_provider.find_user(username)
        except IdentityError as e:
            return jsonify({"error": str(e)}), 404
        entitlements.invalidate(ident.subject)
        expire_claims(ident.subject)
        revoked = (sessions.revoke_subject(ident.subject)
                   if body.get("revoke_sessions") else 0)
        audit_event("invalidate", target=ident.subject, sessions_revoked=revoked)
        return jsonify({"invalidated": 1, "subject": ident.subject,
                        "sessions_revoked": revoked})

    @app.post("/identity/sync-groups")
    @require_admin
    def identity_sync_groups():
        """Controlla subito le modifiche a gruppi e account nel directory
        (per un cron, o se ldap_change_poll_s è 0)."""
        return jsonify(sync_groups())

    @app.get("/identity/status")
    @require_admin
    def identity_status():
        return jsonify({"identity_provider": cfg.identity_provider,
                        "cached_users": len(entitlements),
                        "sessions": sessions.count(),
                        "entitlement_cache_ttl_s": cfg.entitlement_cache_ttl_s,
                        "change_poll_s": cfg.ldap_change_poll_s,
                        "sync": sync_state,
                        "role_map_missing": role_map_missing,
                        "deployment_warnings": deployment_warnings})

    @app.post("/identity/refresh")
    def identity_refresh():
        """Bust della cache dei ruoli per l'utente corrente: i permessi
        appena ottenuti valgono subito, senza aspettare il TTL."""
        if g.identity is None:
            return jsonify({"error": "nessuna identità autenticata"}), 400
        entitlements.invalidate(g.identity.subject)
        expire_claims(g.identity.subject)
        try:
            roles = entitlements.roles_for(g.identity)
        except AccountInactive:
            sessions.revoke_subject(g.identity.subject)
            return jsonify({"error": "account non più attivo: sessione revocata"}), 401
        except IdentityError:
            return jsonify({"error": "directory non raggiungibile, riprova"}), 503
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
