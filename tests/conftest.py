import os

import pytest

os.environ.setdefault("CASEFILE_LANGFUSE", "0")  # tests never send traces
os.environ.setdefault("CASEFILE_NO_OLLAMA", "1")


@pytest.fixture(scope="session")
def seeded_db():
    from casefile import db

    db.init_schema()
    db.seed()
    return db


@pytest.fixture
def oracle(monkeypatch):
    from casefile import llm, oracle

    monkeypatch.setenv("CASEFILE_MODELS", "fake:oracle")
    monkeypatch.delenv("CASEFILE_FAKE_FAULTS", raising=False)
    llm.set_fake(oracle.respond)
    yield oracle
    llm.set_fake(None)
