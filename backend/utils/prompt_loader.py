"""
PromptLoader — loads agent system prompts from .md files at runtime.

Each agent calls:
    prompt = PromptLoader.get("financial")   # loads prompts/financial.md
    prompt = PromptLoader.get("planner")     # loads prompts/planner.md

The Planner can also call:
    manifest = PromptLoader.manifest()       # dict of agent_name -> first-line description
"""
import os
from pathlib import Path
from typing import Optional
from functools import lru_cache

from .config import settings


class PromptLoader:
    """Hot-reload friendly Markdown prompt loader."""

    _cache: dict[str, str] = {}

    @classmethod
    def get(cls, agent_name: str, use_cache: bool = False) -> str:
        """
        Load the system prompt for a given agent from its .md file.

        Args:
            agent_name: Name of the agent (e.g. 'financial', 'planner').
            use_cache: If True, return cached version. Default False for hot-reload.

        Returns:
            The full content of prompts/<agent_name>.md as a string.

        Raises:
            FileNotFoundError: If no .md file exists for this agent.
        """
        if use_cache and agent_name in cls._cache:
            return cls._cache[agent_name]

        prompt_path = settings.PROMPTS_DIR / f"{agent_name}.md"
        if not prompt_path.exists():
            raise FileNotFoundError(
                f"No system prompt found for agent '{agent_name}'. "
                f"Expected file: {prompt_path}"
            )

        content = prompt_path.read_text(encoding="utf-8")
        cls._cache[agent_name] = content
        return content

    @classmethod
    def manifest(cls) -> dict[str, str]:
        """
        Return a manifest of all available agents and their one-line descriptions.
        The Planner uses this to know which agents are available.

        Returns:
            dict mapping agent_name -> first non-empty line of description from .md
        """
        manifest: dict[str, str] = {}
        prompts_dir = settings.PROMPTS_DIR

        if not prompts_dir.exists():
            return manifest

        for md_file in sorted(prompts_dir.glob("*.md")):
            agent_name = md_file.stem
            try:
                content = md_file.read_text(encoding="utf-8")
                # Extract the first meaningful description line (skip # headers)
                description = ""
                for line in content.splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        description = line
                        break
                manifest[agent_name] = description
            except Exception:
                manifest[agent_name] = "(description unavailable)"

        return manifest

    @classmethod
    def clear_cache(cls) -> None:
        """Clear the prompt cache to force hot-reload."""
        cls._cache.clear()

    @classmethod
    def list_agents(cls) -> list[str]:
        """Return a list of all available agent names (from .md files in prompts/)."""
        prompts_dir = settings.PROMPTS_DIR
        if not prompts_dir.exists():
            return []
        return [f.stem for f in sorted(prompts_dir.glob("*.md"))]
