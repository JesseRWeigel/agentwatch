"""Must be REJECTED: a computed import evades a plain AST import scan."""
import importlib
mod = importlib.import_module("agent" + "watch.sessions")
