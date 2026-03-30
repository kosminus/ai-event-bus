"""Output Parser — validates and parses structured LLM responses.

LLMs must return JSON matching the AgentResponseOutput schema.
Malformed output goes to system.parse_failure.
"""

from __future__ import annotations

import json
import logging
import re

from aiventbus.models import AgentResponseOutput

logger = logging.getLogger(__name__)


class OutputParser:
    """Parses structured JSON output from LLM agents."""

    def parse(self, raw_text: str) -> AgentResponseOutput | None:
        """Attempt to parse LLM output as structured JSON.

        Returns AgentResponseOutput on success, None on failure.
        """
        text = raw_text.strip()

        # Try direct JSON parse
        parsed = self._try_parse_json(text)
        if parsed:
            return parsed

        # Try extracting JSON from markdown code blocks
        parsed = self._try_extract_code_block(text)
        if parsed:
            return parsed

        # Try finding JSON object in the text
        parsed = self._try_find_json_object(text)
        if parsed:
            return parsed

        logger.warning("Failed to parse LLM output as structured JSON")
        return None

    def _try_parse_json(self, text: str) -> AgentResponseOutput | None:
        try:
            data = json.loads(text)
            return self._validate(data)
        except (json.JSONDecodeError, Exception):
            return None

    def _try_extract_code_block(self, text: str) -> AgentResponseOutput | None:
        pattern = r"```(?:json)?\s*\n?(.*?)\n?```"
        matches = re.findall(pattern, text, re.DOTALL)
        for match in matches:
            try:
                data = json.loads(match.strip())
                return self._validate(data)
            except (json.JSONDecodeError, Exception):
                continue
        return None

    def _try_find_json_object(self, text: str) -> AgentResponseOutput | None:
        # Find the first { and last }
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            data = json.loads(text[start : end + 1])
            return self._validate(data)
        except (json.JSONDecodeError, Exception):
            return None

    def _validate(self, data: dict) -> AgentResponseOutput | None:
        """Validate parsed data against the expected schema."""
        if not isinstance(data, dict):
            return None
        # Ensure required fields
        if "type" not in data:
            data["type"] = "analysis"
        if "summary" not in data:
            data["summary"] = data.get("message", data.get("response", "No summary"))
        try:
            return AgentResponseOutput(**data)
        except Exception as e:
            logger.warning("Validation failed: %s", e)
            return None
