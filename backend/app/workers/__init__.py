"""Durable standalone worker entry points.

Workers are deliberately not started by FastAPI. Run them as separate processes so web worker
restarts cannot duplicate scheduler loops or interrupt outbox leases.
"""
