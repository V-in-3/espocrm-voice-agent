TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_screen",
            "description": "Read the current state of the contact form as an accessibility snapshot.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fill_field",
            "description": "Fill one field of the open contact form.",
            "parameters": {
                "type": "object",
                "properties": {
                    "field": {
                        "type": "string",
                        "enum": ["firstName", "lastName", "email", "phone"],
                    },
                    "value": {"type": "string"},
                },
                "required": ["field", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save",
            "description": "Save the contact form. Returns the new record id, or fails if required fields are missing.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]
