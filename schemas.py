"""Model-facing schemas for the Tern runner tools."""

TERN_CLAIM_NEXT = {
    "name": "tern_claim_next",
    "description": "Claim the next ready Tern delegation for one explicitly configured organization and project route. The plugin verifies the local checkout and keeps attempt and lease credentials private.",
    "parameters": {
        "type": "object",
        "properties": {
            "organization_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 160,
                "description": "Exact Tern organization ID supplied by the scheduler.",
            },
            "project_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 160,
                "description": "Exact Tern project ID supplied by the scheduler.",
            },
        },
        "required": ["organization_id", "project_id"],
        "additionalProperties": False,
    },
}

TERN_PROGRESS = {
    "name": "tern_progress",
    "description": "Report bounded progress for the currently claimed Tern run and renew its lease.",
    "parameters": {
        "type": "object",
        "properties": {
            "message": {"type": "string", "minLength": 1, "maxLength": 2000, "description": "Concrete progress update for the Tern activity log."},
            "phase": {"type": "string", "minLength": 1, "maxLength": 120, "description": "Short work phase such as inspect, implement, or verify."},
            "percent": {"type": "integer", "minimum": 0, "maximum": 100},
            "known_gap": {"type": "string", "description": "Optional limitation discovered during the run."},
        },
        "required": ["message"],
        "additionalProperties": False,
    },
}

TERN_FINISH = {
    "name": "tern_finish",
    "description": "Finish the currently claimed Tern run. Call exactly once after work and proportional verification, including any honest gaps.",
    "parameters": {
        "type": "object",
        "properties": {
            "outcome": {"type": "string", "enum": ["succeeded", "needs_review", "failed", "blocked"]},
            "summary": {"type": "string", "minLength": 1, "maxLength": 4000, "description": "Concise outcome visible in Tern."},
            "work_performed": {"type": "string", "maxLength": 12000, "description": "What was actually changed or investigated."},
            "known_gaps": {"type": "array", "maxItems": 20, "items": {"type": "string", "minLength": 1, "maxLength": 500}},
            "artifacts": {
                "type": "array",
                "description": "Canonical Tern artifacts only; omit rather than inventing links.",
                "maxItems": 50,
                "items": {"type": "object"},
            },
        },
        "required": ["outcome", "summary", "work_performed"],
        "additionalProperties": False,
    },
}

TERN_RUN_STATUS = {
    "name": "tern_run_status",
    "description": "Inspect whether this Hermes process currently owns a Tern run and whether its lease is healthy.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}
