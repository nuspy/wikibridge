"""Permessi NTFS dei file locali (Windows).

La DACL di un file contiene già i permessi ereditati dalle cartelle
(Windows li calcola e li scrive sul file), quindi non serve risalire
l'albero: chi può LEGGERE il file è l'unione delle voci "consenti" con un
diritto di lettura. Diventa una clausola: {"groups": [...], "users": [...]},
nello stesso formato degli altri adapter, tradotta da source_acl.py.

Non rappresentabile nel modello di wikibridge: le voci "nega" (deny). Un
file con un deny di lettura è trattato come permessi non leggibili (solo
admin) e compare nel report, invece di rischiare di aprirlo a chi è negato.

Senza pywin32 (o fuori da Windows) i permessi non si leggono: fail-closed.
"""
from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

from .adapters.base import AclUnreadable

log = logging.getLogger("wikibridge.ntfs")

# diritti che permettono di leggere il contenuto
_READ_MASK = 0x0001 | 0x80000000 | 0x10000000      # READ_DATA, GENERIC_READ, GENERIC_ALL
# identità di sistema e amministrazione: vedono comunque tutto, non sono
# destinatari da tradurre (l'admin di wikibridge vede tutto per suo conto)
_SKIP = {"S-1-5-18", "S-1-5-32-544", "S-1-3-0", "S-1-3-4", "S-1-5-19", "S-1-5-20"}
# gruppi "tutti" di Windows: vanno mappati esplicitamente (source_group_map),
# es. "Authenticated Users" -> "dipendenti"
BROAD = {"S-1-1-0": "Everyone", "S-1-5-11": "Authenticated Users",
         "S-1-5-32-545": "Users"}


def _account(sid) -> tuple[str, int]:
    import win32security
    try:
        name, domain, kind = win32security.LookupAccountSid(None, sid)
        return (f"{domain}\\{name}" if domain else name), kind
    except win32security.error:
        return win32security.ConvertSidToStringSid(sid), 0


def file_read_acl(path: str | Path) -> list[dict]:
    """[{"groups": [...], "users": [{"username": ...}]}] per un file."""
    try:
        import ntsecuritycon  # noqa: F401
        import win32security
    except ImportError as e:
        raise AclUnreadable("pywin32 non installato: permessi NTFS non leggibili") from e
    try:
        sd = win32security.GetFileSecurity(
            str(path), win32security.DACL_SECURITY_INFORMATION)
        dacl = sd.GetSecurityDescriptorDacl()
    except win32security.error as e:
        raise AclUnreadable(f"DACL di {path} non leggibile: {e}") from e
    if dacl is None:                    # DACL nulla = accesso a chiunque
        return [{"groups": ["Everyone"], "users": []}]
    groups, users = set(), set()
    for i in range(dacl.GetAceCount()):
        (ace_type, _flags), mask, sid = dacl.GetAce(i)[:3]
        if not mask & _READ_MASK:
            continue
        sid_str = win32security.ConvertSidToStringSid(sid)
        if sid_str in _SKIP:
            continue
        if ace_type == win32security.ACCESS_DENIED_ACE_TYPE:
            raise AclUnreadable(f"{Path(path).name}: voce 'nega' per {_account(sid)[0]} "
                                "(non rappresentabile: file solo admin)")
        if ace_type != win32security.ACCESS_ALLOWED_ACE_TYPE:
            continue
        if sid_str in BROAD:
            groups.add(BROAD[sid_str])
            continue
        name, kind = _account(sid)
        if kind == 1:                   # SidTypeUser
            users.add(name.split("\\")[-1])
        else:
            groups.add(name)
    return [{"groups": sorted(groups), "users": [{"username": u} for u in sorted(users)]}]


def data_dir_warnings(path: str | Path) -> list[str]:
    """Chi, oltre al servizio, può leggere la cartella dei dati: in
    multi-utente chiunque la legga scavalca le ACL (vedi documentation.md)."""
    path = Path(path)
    if not path.exists():
        return []
    if os.name != "nt":
        mode = path.stat().st_mode
        return ([f"{path} è leggibile da tutti gli utenti del sistema (permessi "
                 f"{stat.filemode(mode)})"] if mode & stat.S_IROTH else [])
    try:
        acl = file_read_acl(path)
    except AclUnreadable as e:
        return [f"permessi di {path} non verificabili: {e}"]
    broad = sorted(set(acl[0]["groups"]) & set(BROAD.values()))
    return ([f"{path} è leggibile da {', '.join(broad)}: con le ACL attive "
             "chiunque acceda al disco scavalca i permessi"] if broad else [])
