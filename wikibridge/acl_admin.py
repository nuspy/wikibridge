"""Gestione delle ACL (usata da CLI e API admin).

- ACL di un documento: principal propri e classificazione nel frontmatter
  della source; l'indice aggiorna una riga di docs, senza reindicizzare
  il testo e senza cambiare la versione del contenuto.
- Collezioni e gate di progetto: acl.json (acl.py); nessuna scrittura
  sull'indice, effetto alla richiesta successiva.
"""
from __future__ import annotations

from .acl import ADMIN_ROLE, LEVELS, AclPolicy
from .filelock import file_lock
from .identity import IdentityProvider
from .projects import Project
from .wiki.search import SearchIndex
from .wiki.writer import WikiWriter


def principals_from(roles: list[str] | None, users: list[str] | None,
                    provider: IdentityProvider | None,
                    labels: dict[str, str] | None = None) -> list[str]:
    """Ruoli così come sono + username risolti nel loro principal
    "user:<objectGUID>" (stabile a rinomine dell'account). labels, se
    passato, riceve il nome leggibile di ogni persona risolta."""
    out = list(roles or [])
    for u in users or []:
        if u.startswith("user:"):
            out.append(u)
            continue
        if provider is None:
            raise ValueError("per assegnare permessi a un utente serve "
                             "un identity provider configurato")
        ident = provider.find_user(u)
        out.append(ident.principal)
        if labels is not None:
            name = ident.display_name or u
            labels[ident.principal] = name if name == u else f"{name} ({u})"
    return sorted(set(out))


def _save_labels(project: Project, labels: dict[str, str] | None) -> None:
    if not labels:
        return
    with _policy_lock(project):
        policy = AclPolicy.load(project)
        if all(policy.labels.get(k) == v for k, v in labels.items()):
            return
        policy.labels.update(labels)
        policy.save()


def set_page_acl(project: Project, rel_path: str, principals: list[str],
                 classification: str | None = None,
                 labels: dict[str, str] | None = None) -> dict:
    if classification is not None and classification not in LEVELS:
        raise ValueError(f"classificazione sconosciuta: {classification}")
    fields = {"acl_roles": sorted(set(principals))}
    if classification is not None:
        fields["acl_classification"] = classification
    fm = WikiWriter(project).update_source_fm(rel_path, **fields)
    SearchIndex(project).set_doc_acl(rel_path, fm)
    _save_labels(project, labels)
    return {"path": rel_path, "acl_roles": fm["acl_roles"],
            "acl_classification": fm.get("acl_classification")}


def preview_page(project: Project, rel_path: str, principals: list[str],
                 classification: str | None) -> dict:
    """Come preview_collection, per i principal propri di un documento."""
    fm, _ = WikiWriter(project).read_page(rel_path)
    old_p, new_p = set(fm.get("acl_roles") or []), set(principals)
    old_c = fm.get("acl_classification") or "internal"
    new_c = classification or old_c
    return {"path": rel_path,
            "added_principals": sorted(new_p - old_p),
            "removed_principals": sorted(old_p - new_p),
            "classification": [old_c, new_c], "documents_affected": 1,
            # togliere tutti i principal propri lascia decidere alla
            # collezione: può allargare anche senza aggiungere nessuno
            "widening": bool(new_p - old_p) or (bool(old_p) and not new_p)
                        or LEVELS.get(new_c, 3) < LEVELS.get(old_c, 1)}


def preview_enforcement(project: Project, enabled: bool) -> dict:
    """Spegnere il filtro rende ogni documento visibile a chiunque entri
    nel progetto: è l'allargamento più ampio possibile."""
    return {"acl_enforcement": [project.acl_enforcement, enabled],
            "documents_affected": sum(_docs_by_source(project).values()),
            "widening": project.acl_enforcement and not enabled}


def _docs_by_source(project: Project) -> dict[str, int]:
    """Numero di documenti indicizzati per sorgente (per le anteprime)."""
    import json as _json
    import sqlite3
    idx = SearchIndex(project)
    idx.acl_sets()                                   # assicura lo schema
    counts: dict[str, int] = {}
    with sqlite3.connect(idx.db_path) as conn:
        for key, n in conn.execute(
                "SELECT a.key, COUNT(*) FROM docs d JOIN acl_sets a "
                "ON a.acl_id = d.acl_id GROUP BY a.key"):
            src = _json.loads(key)[0]
            counts[src] = counts.get(src, 0) + n
    return counts


def preview_collection(project: Project, coll_id: str, *, principals: list[str],
                       classification: str, sources: list[str]) -> dict:
    """Cosa cambierebbe: chi guadagna o perde accesso e su quanti documenti.
    widening = qualcuno potrebbe vedere più di prima (serve conferma)."""
    old = AclPolicy.load(project).collections.get(coll_id) or {}
    old_p, new_p = set(old.get("principals", [])), set(principals)
    old_s, new_s = set(old.get("sources", [])), set(sources)
    docs = _docs_by_source(project)
    removed_sources = sorted(old_s - new_s)
    lowered = bool(old) and LEVELS.get(classification, 3) < LEVELS.get(
        old.get("classification", "internal"), 1)
    # anche creare una collezione allarga: i documenti delle sue sorgenti
    # senza restrizioni proprie passano da "solo admin" ai suoi principal
    return {"collection": coll_id, "exists": bool(old),
            "added_principals": sorted(new_p - old_p),
            "removed_principals": sorted(old_p - new_p),
            "removed_sources": removed_sources,
            "classification": [old.get("classification"), classification],
            "documents_affected": sum(docs.get(s, 0) for s in old_s | new_s),
            "widening": bool(new_p - old_p) or lowered or bool(removed_sources)}


def preview_gate(project: Project, principals: list[str]) -> dict:
    old = set(AclPolicy.load(project).gate)
    new = set(principals)
    return {"added_principals": sorted(new - old), "removed_principals": sorted(old - new),
            "documents_affected": sum(_docs_by_source(project).values()),
            "widening": bool(old) and (not new or bool(new - old))}


def _policy_lock(project: Project):
    """acl.json si legge, si modifica e si salva sotto un solo lock: due
    amministratori (o API e CLI) non si cancellano le modifiche a vicenda."""
    return file_lock(project.root / "acl.json.lock")


def set_collection(project: Project, coll_id: str, *, principals: list[str],
                   classification: str, sources: list[str],
                   labels: dict[str, str] | None = None) -> dict:
    with _policy_lock(project):
        policy = AclPolicy.load(project)
        policy.set_collection(coll_id, principals=principals,
                              classification=classification, sources=sources)
        policy.labels.update(labels or {})
        policy.save()
    return {"collection": coll_id, **policy.collections[coll_id],
            "policy_version": policy.version}


def delete_collection(project: Project, coll_id: str) -> dict:
    with _policy_lock(project):
        policy = AclPolicy.load(project)
        if policy.collections.pop(coll_id, None) is None:
            raise KeyError(f"collezione inesistente: {coll_id}")
        policy.save()
    return {"deleted": coll_id, "policy_version": policy.version}


def set_gate(project: Project, principals: list[str],
             labels: dict[str, str] | None = None) -> dict:
    with _policy_lock(project):
        policy = AclPolicy.load(project)
        policy.gate = sorted(set(principals))
        policy.labels.update(labels or {})
        policy.save()
    return {"gate": policy.gate, "policy_version": policy.version}


def _is_role(p: str) -> bool:
    return not p.startswith(("user:", "acl:", "clearance:")) and p != ADMIN_ROLE


def show(project: Project, rel_path: str | None = None,
         known_roles: list[str] | tuple = ()) -> dict:
    """known_roles: ruoli che il server conosce dalla configurazione (mappe
    dei gruppi), uniti a quelli già usati: la GUI li propone come scelta."""
    policy = AclPolicy.load(project)
    used = set(policy.gate)
    for coll in policy.collections.values():
        used |= set(coll.get("principals", []))
    out = {"project": project.slug, "acl_enforcement": project.acl_enforcement,
           "policy_version": policy.version, "gate": policy.gate,
           "collections": policy.collections, "labels": policy.labels,
           "levels": sorted(LEVELS, key=LEVELS.get),
           "known_roles": sorted({r for r in (*known_roles, *used) if _is_role(r)})}
    if rel_path:
        fm, _ = WikiWriter(project).read_page(rel_path)
        coll = policy.collection_of(fm.get("import_id", ""))
        out["page"] = {"path": rel_path, "source": fm.get("import_id"),
                       "acl_roles": fm.get("acl_roles", []),
                       "acl_source_clauses": fm.get("acl_source_clauses", []),
                       "acl_classification": fm.get("acl_classification"),
                       "collection": next((k for k, v in policy.collections.items()
                                           if v is coll), None)}
    return out
