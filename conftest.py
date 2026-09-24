"""Puts the repository root on sys.path so `import src...` works under pytest.

An empty file would do it - pytest adds a conftest's directory to sys.path in
the default import mode - but an empty file invites deletion by anyone tidying
up, and the suite stops collecting the moment it goes.
"""
