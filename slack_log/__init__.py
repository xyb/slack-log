"""slack-log: a Slack workspace archive as a searchable web service."""

# Kept in sync with pyproject.toml at release time. The container runs the
# package off PYTHONPATH (not pip-installed), so importlib.metadata can't see
# it — a literal is the one value that's always available.
__version__ = "0.11.0"
