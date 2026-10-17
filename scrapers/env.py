"""Read API keys from the project's .env file (see .env.example)."""
import os

ENV_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")


def load_env(path=ENV_FILE):
    """Set KEY=value lines from a .env file (see .env.example) as environment variables.
    Blank values, comments and variables already set in the shell are left alone."""
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except OSError:
        return
    for line in lines:
        key, sep, value = line.removeprefix("export ").partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if sep and key and value and not key.startswith("#") and key not in os.environ:
            os.environ[key] = value
