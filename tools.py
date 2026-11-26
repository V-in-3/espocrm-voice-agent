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
                        "enum": [
                            "firstName", "lastName", "email", "phone",
                            "addressStreet", "addressCity", "addressState",
                            "addressPostalCode", "addressCountry",
                        ],
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
            "name": "link_account",
            "description": "Link an existing account by its full name. Never invent an account name.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clear_account",
            "description": "Remove the linked account only when the user explicitly requests it.",
            "parameters": {"type": "object", "properties": {}},
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
