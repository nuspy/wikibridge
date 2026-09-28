import pytest

from wikibridge.config import Config
from wikibridge.projects import ProjectStore


@pytest.fixture
def cfg(tmp_path):
    c = Config()
    c.data_dir = str(tmp_path / "data")
    c.secrets_dir = str(tmp_path / "secrets")
    c.brain_mode = "none"
    c.rate_limit_rps = 1000.0
    return c


@pytest.fixture
def project(cfg):
    return ProjectStore(cfg).get("default")


def source_pages(project) -> list[str]:
    return sorted(p.name for p in (project.wiki_dir / "sources").glob("*.md"))
