"""The config.env file format shared by the runtime and both installers."""


def env_values(text):
    """Read literal values, removing one matching pair of surrounding quotes.

    This is a file parser, not a shell: escapes, variables, command substitution,
    and inline comments remain literal. Environment overrides are applied by
    callers after parsing and are not normalized here.
    """
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values
