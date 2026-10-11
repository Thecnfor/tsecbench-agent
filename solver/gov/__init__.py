"""Governance layer (governance thin version) — session / profiles / planner / prior.

Borrowed from the OpenHands agent-server service division (conversation / event-stream,
agent-profiles), but self-developed as a thin version: within the constraints of one question with deadline 120~1200s,
the governance layer only allows 1 additional LLM planning call, everything else is deterministic code.
"""
