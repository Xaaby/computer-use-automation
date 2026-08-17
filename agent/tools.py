"""
agent/tools.py
The 8 tools Claude can call during discovery.
Format: Bedrock converse() toolSpec format — NOT Anthropic SDK format.
Sent as toolConfig={"tools": DISCOVERY_TOOLS} in every bedrock.converse() call.
"""

DISCOVERY_TOOLS = [
    {
        "toolSpec": {
            "name": "click",
            "description": (
                "Click an element identified by its ARIA snapshot ref. "
                "Set risk_level='irreversible_commit' ONLY for final transaction submission buttons. "
                "Set risk_level='requires_confirmation' for form submissions that change data. "
                "Default is 'safe' for navigation and read-only actions."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["ref"],
                    "properties": {
                        "ref": {
                            "type": "string",
                            "description": "Element ref from aria_snapshot e.g. e17"
                        },
                        "risk_level": {
                            "type": "string",
                            "enum": ["safe", "requires_confirmation", "irreversible_commit"],
                            "description": "Risk classification for this action"
                        }
                    }
                }
            }
        }
    },
    {
        "toolSpec": {
            "name": "fill",
            "description": (
                "Fill an input field. Use '$inputs.param_name' syntax to reference "
                "a capability input parameter (e.g. '$inputs.member_id'). "
                "Use a literal string for fixed values."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["ref", "value"],
                    "properties": {
                        "ref": {"type": "string", "description": "Element ref from aria_snapshot"},
                        "value": {
                            "type": "string",
                            "description": "Literal value or $inputs.param_name reference"
                        }
                    }
                }
            }
        }
    },
    {
        "toolSpec": {
            "name": "press",
            "description": "Press a keyboard key or combination",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["key"],
                    "properties": {
                        "key": {
                            "type": "string",
                            "description": "Key or combo: Enter, Tab, Escape, Control+a"
                        }
                    }
                }
            }
        }
    },
    {
        "toolSpec": {
            "name": "navigate",
            "description": "Navigate to a URL. Only URLs on the policy allowlist are permitted.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["url"],
                    "properties": {
                        "url": {"type": "string", "description": "Full URL to navigate to"}
                    }
                }
            }
        }
    },
    {
        "toolSpec": {
            "name": "read",
            "description": "Read and extract the text content of an element for the capability output.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["ref", "output_name"],
                    "properties": {
                        "ref": {"type": "string", "description": "Element ref from aria_snapshot"},
                        "output_name": {
                            "type": "string",
                            "description": "Key to store this value under in outputs e.g. savings_balance"
                        }
                    }
                }
            }
        }
    },
    {
        "toolSpec": {
            "name": "observe_screenshot",
            "description": (
                "Capture a screenshot when the ARIA accessibility tree is insufficient. "
                "Screenshots are expensive — prefer ARIA tree navigation. "
                "Use only when you cannot identify an element from the ARIA snapshot."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {}
                }
            }
        }
    },
    {
        "toolSpec": {
            "name": "done",
            "description": "Signal goal completion with all extracted output values.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["outputs", "success_description"],
                    "properties": {
                        "outputs": {
                            "type": "object",
                            "description": "All extracted output key-value pairs"
                        },
                        "success_description": {
                            "type": "string",
                            "description": "Human-readable description of what was accomplished"
                        }
                    }
                }
            }
        }
    },
    {
        "toolSpec": {
            "name": "escalate",
            "description": (
                "Signal that automation cannot safely proceed and needs human help. "
                "Use when: stuck in a loop, facing a risky action needing approval, "
                "or in an unexpected UI state after multiple attempts."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["reason"],
                    "properties": {
                        "reason": {
                            "type": "string",
                            "description": "Why escalation is needed"
                        },
                        "stuck_description": {
                            "type": "string",
                            "description": "Detailed description of the current blocked state"
                        }
                    }
                }
            }
        }
    }
]
