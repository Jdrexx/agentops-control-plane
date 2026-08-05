from __future__ import annotations

from typing import Any

WORKFLOW_TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "research-brief",
        "name": "Research brief",
        "description": "Generate a concise brief and require human approval before release.",
        "steps": [
            {
                "name": "Research",
                "tool": "llm",
                "config": {
                    "provider": "openai",
                    "prompt": "Create a sourced research brief for: {value}",
                },
            },
            {
                "name": "Review",
                "tool": "approval",
                "config": {"prompt": "Approve this research brief?"},
            },
        ],
    },
    {
        "id": "support-response",
        "name": "Customer support response",
        "description": "Draft, review, and format a customer-safe response.",
        "steps": [
            {
                "name": "Draft response",
                "tool": "llm",
                "config": {
                    "provider": "openai",
                    "system": "Be accurate, concise, and never invent account details.",
                    "prompt": "Draft a support response to: {value}",
                },
            },
            {
                "name": "Human review",
                "tool": "approval",
                "config": {"prompt": "Approve the customer response?"},
            },
            {
                "name": "Finalize",
                "tool": "template",
                "config": {"template": "APPROVED RESPONSE\n\n{value}"},
            },
        ],
    },
    {
        "id": "document-extraction",
        "name": "Document extraction",
        "description": "Extract structured JSON from incoming document text.",
        "steps": [
            {
                "name": "Extract fields",
                "tool": "llm",
                "config": {
                    "provider": "openai",
                    "system": "Return valid JSON only.",
                    "prompt": "Extract names, dates, amounts, and action items from: {value}",
                },
            },
        ],
    },
    {
        "id": "code-review",
        "name": "Code review",
        "description": "Review a patch for correctness, security, and missing tests.",
        "steps": [
            {
                "name": "Review patch",
                "tool": "llm",
                "config": {
                    "provider": "openai",
                    "system": "Prioritize actionable correctness and security findings.",
                    "prompt": "Review this patch:\n{value}",
                },
            },
            {
                "name": "Maintainer approval",
                "tool": "approval",
                "config": {"prompt": "Accept this code review?"},
            },
        ],
    },
]
