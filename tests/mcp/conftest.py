"""Shared fixtures for the MCP unit tests.

A Carve-shaped OpenAPI fragment (path/query/body params, the streaming endpoint,
and every naming-override case) so the generator/adapter tests read against one
representative document instead of duplicating fragments.
"""

from __future__ import annotations

from typing import Any

import pytest


def _op(**kwargs: Any) -> dict[str, Any]:
    return kwargs


def sample_openapi() -> dict[str, Any]:
    """A representative subset of Carve's REST OpenAPI 3.1 document."""
    return {
        "openapi": "3.1.0",
        "paths": {
            "/api/v1/plans": {
                "get": _op(
                    summary="List plans",
                    parameters=[
                        {
                            "name": "cursor",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "limit",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "integer"},
                        },
                    ],
                ),
                "post": _op(
                    description="Create a plan",
                    requestBody={
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/PlanRequestIn"}
                            }
                        }
                    },
                ),
            },
            "/api/v1/plans/{plan_id}": {
                "get": _op(
                    summary="Show a plan",
                    parameters=[
                        {
                            "name": "plan_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                )
            },
            "/api/v1/builds": {
                "post": _op(
                    summary="Run a build",
                    requestBody={
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/BuildRequestIn"}
                            }
                        }
                    },
                )
            },
            "/api/v1/builds/latest/{pipeline_name}/{target}": {
                "get": _op(
                    summary="Latest build",
                    parameters=[
                        {"name": "pipeline_name", "in": "path", "required": True,
                         "schema": {"type": "string"}},
                        {"name": "target", "in": "path", "required": True,
                         "schema": {"type": "string"}},
                    ],
                )
            },
            "/api/v1/runs": {
                "post": _op(
                    summary="Trigger a run",
                    requestBody={
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/RunTriggerIn"}
                            }
                        }
                    },
                )
            },
            "/api/v1/runs/{run_id}/resume": {
                "post": _op(
                    summary="Resume a run",
                    parameters=[
                        {"name": "run_id", "in": "path", "required": True,
                         "schema": {"type": "string"}}
                    ],
                )
            },
            "/api/v1/runs/{run_id}/logs": {
                "get": _op(
                    summary="Run logs",
                    parameters=[
                        {"name": "run_id", "in": "path", "required": True,
                         "schema": {"type": "string"}},
                        {"name": "since_id", "in": "query", "required": False,
                         "schema": {"type": "integer"}},
                    ],
                )
            },
            "/api/v1/runs/{run_id}/stream": {
                "get": _op(
                    summary="Live run stream",
                    parameters=[
                        {"name": "run_id", "in": "path", "required": True,
                         "schema": {"type": "string"}}
                    ],
                )
            },
            "/api/v1/memory/{kind}": {
                "get": _op(
                    summary="Show memory",
                    parameters=[
                        {"name": "kind", "in": "path", "required": True,
                         "schema": {"type": "string"}}
                    ],
                )
            },
            "/api/v1/memory/decisions": {
                "post": _op(
                    summary="Append a decision",
                    requestBody={
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/DecisionIn"}
                            }
                        }
                    },
                )
            },
        },
        "components": {
            "schemas": {
                "PlanRequestIn": {
                    "type": "object",
                    "required": ["goal"],
                    "properties": {
                        "goal": {"type": "string"},
                        "pipeline_name": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    },
                },
                "BuildRequestIn": {
                    "type": "object",
                    "required": ["plan_id"],
                    "properties": {
                        "plan_id": {"type": "string"},
                        "force": {"type": "boolean", "default": False},
                    },
                },
                "RunTriggerIn": {
                    "type": "object",
                    "required": ["pipeline_name"],
                    "properties": {
                        "pipeline_name": {"type": "string"},
                        "target": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    },
                },
                "DecisionIn": {
                    "type": "object",
                    "required": ["title", "body"],
                    "properties": {
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                        "reviewers": {"type": "array", "items": {"type": "string"}},
                    },
                },
            }
        },
    }


@pytest.fixture
def openapi_fragment() -> dict[str, Any]:
    return sample_openapi()
