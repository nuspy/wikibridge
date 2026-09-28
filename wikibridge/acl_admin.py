"""Gestione delle ACL (usata da CLI e API admin).

- ACL di un documento: principal propri e classificazione nel frontmatter
  della source; l'indice aggiorna una riga di docs, senza reindicizzare
  il testo e senza cambiare la versione del contenuto.
- Collezioni e gate di progetto: acl.json (acl.py); nessuna scrittura
  sull'indice, effetto alla richiesta successiva.
"""
from __future__ import annotations

from .acl import LEVELS, AclPolicy
from .identity import IdentityProvider
from .projects import Project
from .wiki.search import SearchIndex
from .wiki.writer import WikiWriter


def principals_from(roles: list[str] | None, users: list[str] | None,
                    provider: IdentityProvider | None) -> list[str]:
    """Ruoli così come sono + username risolti nel loro principal
    "user:<objectGUID>" (stabile a rinomine dell'account)."""
    out = list(roles or [])
    for u in users or []:
        if u.startswith("user:"):
            out.append(u)
            continue
        if provider is None:
            raise ValueError("per assegnare permessi a un utente serve "
                             "un identity provider configurato")
        out.append(provider.find_user(u).principal)
    return sorted(set(out))


def set_page_acl(project: Project, rel_path: str, principals: list[str],
                 classification: str | None = None) -> dict:
    if classification is not None and classification not in LEVELS:
        raise ValueError(f"classificazione sconosciuta: {classification}")
    fields = {"acl_roles": sorted(set(principals))}
    if classification is not None:
        fields["acl_classification"] = classification
    fm = WikiWriter(project).update_source_fm(rel_path, **fields)
    SearchIndex(project).set_doc_acl(rel_path, fm)
    return {"path": rel_path, "acl_roles": fm["acl_roles"],
            "acl_classification": fm.get("acl_classification")}


def set_collection(project: Project, coll_id: str, *, principals: list[str],
                   classification: str, sources: list[str]) -> dict:
    policy = AclPolicy.load(project)
    policy.set_collection(coll_id, principals=principals,
                          classification=classification, sources=sources)
    policy.save()
    return {"collection": coll_id, **policy.collections[coll_id],
            "policy_version": policy.version}


def delete_collection(project: Project, coll_id: str) -> dict:
    policy = AclPolicy.load(project)
    if policy.collections.pop(coll_id, None) is None:
        raise KeyError(f"collezione inesistente: {coll_id}")
    policy.save()
    return {"deleted": coll_id, "policy_version": policy.version}


def set_gate(project: Project, principals: list[str]) -> dict:
    policy = AclPolicy.load(project)
    policy.gate = sorted(set(principals))
    policy.save()
    return {"gate": policy.gate, "policy_version": policy.version}


def show(project: Project, rel_path: str | None = None) -> dict:
    policy = AclPolicy.load(project)
    out = {"project": project.slug, "acl_enforcement": project.acl_enforcement,
           "policy_version": policy.version, "gate": policy.gate,
           "collections": policy.collections}
    if rel_path:
        fm, _ = WikiWriter(project).read_page(rel_path)
        coll = policy.collection_of(fm.get("import_id", ""))
        out["page"] = {"path": rel_path, "source": fm.get("import_id"),
                       "acl_roles": fm.get("acl_roles", []),
                       "acl_classification": fm.get("acl_classification"),
                       "collection": next((k for k, v in policy.collections.items()
                                           if v is coll), None)}
    return out
