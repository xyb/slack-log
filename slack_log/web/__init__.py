"""Web — the serving layer.

  app   the FastAPI application; depends only on an ArchiveStore
  auth  optional OIDC SSO middleware + access logging
  sync  the in-process refresh manager
"""
