"""Suite-wide isolation: never write a workspace registry into the repository.

The source registry is a person's data — which domains their runs have seen and
promoted. Tests exercise discovery, which observes as it goes, so without this
the suite leaves `source_registry.json` in the repo root and a careless `git add`
commits it. Pointing the registry at a temporary directory keeps that impossible.
"""
import pytest


@pytest.fixture(autouse=True)
def _isolated_source_registry(tmp_path_factory, monkeypatch):
    from harness_fleet.registry import REGISTRY_ENV

    monkeypatch.setenv(REGISTRY_ENV, str(tmp_path_factory.mktemp("registry") / "source_registry.json"))
