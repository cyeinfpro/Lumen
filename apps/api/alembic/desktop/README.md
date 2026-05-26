# Lumen Desktop Alembic Chain

This migration chain is intentionally separate from the Docker/Postgres chain.
It creates the minimum SQLite schema used by the desktop runtime: local user,
conversations, messages, generations, images, image variants, shares, system
prompts, system settings, memory, outbox, and audit events.

Docker deployments must continue to use `apps/api/alembic/versions`.
