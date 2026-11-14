"""Shared boundaries between actual feedback and quoted/injected instructions."""

CONTEXT_PATTERN = (
    r"(?ism)<system-reminder>.*?</system-reminder>"
    r"|<environment_context>.*?</environment_context>"
    r"|<user_instructions>.*?</user_instructions>|<instructions>.*?</instructions>"
    r"|<local-command-std(?:out|err)>.*?</local-command-std(?:out|err)>"
)
OWN_TEXT_PATTERN = CONTEXT_PATTERN + r"|```.*?```|`[^`\n]*`|^\s*>[^\n]*(?:\n|$)"
CLAUSE_PHRASES = ("that's wrong", "that is wrong")
CORRECTION_CLAUSE = (
    r"(?i)(?:^|[.!?,;:\n])[\"'“”]*\s*"
    r"(?:(?:no|actually|well)\s*,?\s*)?(that(?: is|'s) wrong)\b"
)
RECOVERY_RETRY = (
    r"(?i)\b(?:i|we)(?:'ve| have)?\s+(?:(?:just|now|already)\s+)*"
    r"(?:cleaned up|restarted|reconnected|logged in|unlocked)\b"
    r"|connection.*try again|\brelock and try again\b"
)
