"""Smoke test: the src-layout package installs and imports cleanly."""


def test_package_imports():
    import stratechery_archive

    assert stratechery_archive.__version__
