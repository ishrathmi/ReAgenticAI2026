"""Where LitVault keeps its data (profiles/ and digests/).

Order: $LITVAULT_HOME, else the current folder if it already has a profiles/ folder, else ~/LitVault.
"""
import os


def default_workspace():
    env = os.environ.get("LITVAULT_HOME")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    if os.path.isdir(os.path.join(os.getcwd(), "profiles")):
        return os.getcwd()
    return os.path.join(os.path.expanduser("~"), "LitVault")


def ensure(path):
    for d in ("profiles", "digests"):
        os.makedirs(os.path.join(path, d), exist_ok=True)
    return path
