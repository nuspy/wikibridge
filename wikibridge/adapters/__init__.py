"""Adapter delle sorgenti wiki. detect_adapter sceglie quello giusto."""
from __future__ import annotations

from .base import SourceAdapter


def detect_adapter(url: str, session, limits, *, forced: str = "auto") -> SourceAdapter:
    """Istanzia l'adapter per l'URL (sniffing) o quello forzato."""
    from .generic import GenericAdapter
    registry: dict[str, type[SourceAdapter]] = {"generic": GenericAdapter}
    try:
        from .mediawiki import MediaWikiAdapter
        registry["mediawiki"] = MediaWikiAdapter
    except ImportError:
        pass
    try:
        from .confluence import ConfluenceAdapter
        registry["confluence"] = ConfluenceAdapter
    except ImportError:
        pass
    try:
        from .notion import NotionAdapter
        registry["notion"] = NotionAdapter
    except ImportError:
        pass

    if forced != "auto":
        return registry[forced](session, limits)

    for name in ("notion", "confluence", "mediawiki"):  # i più specifici prima
        cls = registry.get(name)
        if cls and cls.detect(url, session):
            return cls(session, limits)
    return GenericAdapter(session, limits)
