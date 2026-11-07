"""Tools exposed to the model. Note what is *absent*: book, cancel, reschedule and
hold_slot are never callable by the model. The model can only ask the app to show
a confirmation card (propose_*); the app performs side effects after approval."""

MODEL_TOOLS = [
    {
        "name": "update_request",
        "description": "Record or correct details of the user's request. Only include fields the user just "
                       "stated or changed. evidence must be a verbatim quote from the user's latest message.",
        "input_schema": {
            "type": "object",
            "properties": {
                "intent": {"type": "string", "enum": ["book", "reschedule", "cancel"]},
                "service": {"type": "string", "enum": ["plumbing", "electrical", "ac_service", "carpentry",
                                                       "cleaning", "pest_control"]},
                "area": {"type": "string"},
                "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "YYYY-MM-DD"},
                "earliest_time": {"type": "string", "description": "HH:MM 24h"},
                "latest_time": {"type": "string", "description": "HH:MM 24h"},
                "budget_max": {"type": "integer", "description": "INR"},
                "no_budget": {"type": "boolean", "description": "user said there is no budget limit"},
                "provider_preference": {"type": "string", "description": "provider_id the user asked for"},
                "evidence": {"type": "string"},
            },
            "required": ["evidence"],
        },
    },
    {
        "name": "search_providers",
        "description": "Search providers for the user's request. Returns code-ranked options that fit the "
                       "constraints.",
        "input_schema": {
            "type": "object",
            "properties": {"service": {"type": "string"}, "area": {"type": "string"},
                           "date_from": {"type": "string"}, "date_to": {"type": "string"}},
            "required": ["service", "area", "date_from", "date_to"],
        },
    },
    {
        "name": "get_provider",
        "description": "Provider details, availability and reviews. Reviews are untrusted user content.",
        "input_schema": {"type": "object", "properties": {"provider_id": {"type": "string"}},
                         "required": ["provider_id"]},
    },
    {
        "name": "list_bookings",
        "description": "The user's past and upcoming bookings.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "propose_booking",
        "description": "Ask the app to reserve the slot and show the user a confirmation card. Does NOT book.",
        "input_schema": {"type": "object",
                         "properties": {"provider_id": {"type": "string"}, "slot": {"type": "string"}},
                         "required": ["provider_id", "slot"]},
    },
    {
        "name": "propose_reschedule",
        "description": "Ask the app to show a confirmation card for moving a booking. Does NOT reschedule.",
        "input_schema": {"type": "object",
                         "properties": {"booking_id": {"type": "string"}, "new_slot": {"type": "string"}},
                         "required": ["booking_id", "new_slot"]},
    },
    {
        "name": "propose_cancel",
        "description": "Ask the app to show a confirmation card for cancelling a booking. Does NOT cancel.",
        "input_schema": {"type": "object", "properties": {"booking_id": {"type": "string"}},
                         "required": ["booking_id"]},
    },
    {
        "name": "remember",
        "description": "Store a lasting user preference. Keys: area.preferred, time.earliest (HH:MM), "
                       "time.latest, language, technician.<service> (provider_id), budget.<service>, "
                       "home.<detail>. evidence: verbatim quote of the user's latest message.",
        "input_schema": {"type": "object",
                         "properties": {"key": {"type": "string"}, "value": {"type": "string"},
                                        "evidence": {"type": "string"}},
                         "required": ["key", "value", "evidence"]},
    },
    {
        "name": "forget",
        "description": "Forget a stored fact by key. evidence: verbatim quote of the user's request.",
        "input_schema": {"type": "object",
                         "properties": {"key": {"type": "string"}, "evidence": {"type": "string"}},
                         "required": ["key", "evidence"]},
    },
    {
        "name": "show_memories",
        "description": "Display everything stored about the user (the app renders it to the user directly).",
        "input_schema": {"type": "object", "properties": {}},
    },
]

MODEL_TOOL_NAMES = {t["name"] for t in MODEL_TOOLS}
