"""Keep test-generated files short-lived and out of the working tree."""

import shutil
import os

import pytest


# Tests must never connect to the development or production database, even if
# a developer has DATABASE_URL exported in the shell or .env.
os.environ["DATABASE_URL"] = "sqlite:///:memory:"


@pytest.fixture(autouse=True)
def remove_test_files(request):
    """Delete each test's temporary directory immediately after it finishes."""
    yield
    temp_path = request.node.funcargs.get("tmp_path")
    if temp_path is not None:
        shutil.rmtree(temp_path, ignore_errors=True)
