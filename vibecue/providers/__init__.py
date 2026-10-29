"""Adapters feed normalized local events into the same VibeCoding Go UI.

Only advertise integrations that are implemented. Future tools should provide
their own event/metadata reader rather than reusing Codex transcript parsing.
"""

PROVIDERS = {
    'codex': {
        'name': 'Codex',
        'status': 'available',
        'source': 'local-transcripts',
        'scope': 'Local main-thread sessions; permission requests need trusted hooks.',
    },
}
