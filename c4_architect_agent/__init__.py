"""C4 Model Architecture Creator Multi-Agent System package entrypoint for Google ADK."""

from . import agent
from .agent import app, root_agent

__all__ = ["agent", "app", "root_agent"]
