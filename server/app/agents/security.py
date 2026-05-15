"""Security dimension agent (LLM layer of the three-tier security pass).

Layers 1 (external scanners) and 2 (regex rules) run elsewhere; this agent
handles what regex can't catch.
"""

from __future__ import annotations

from .base import DimensionAgent


class SecurityAgent(DimensionAgent):
    dimension = "security"
    prompt_intro = """You are the security dimension of a doc-audit bot.

What to look for (high-precision, low-recall is the right tradeoff):
  * Permission-loosening advice: prose or examples that tell the reader to
    disable auth, chmod 777 a sensitive path, run as root, etc.
  * Insecure credential handling described in text: writing tokens to disk,
    embedding keys in shell history, copying secrets into PR descriptions.
  * Bypass-of-control suggestions: "to skip the validation step, set X=1",
    "comment out the signature check during local testing".
  * Unsafe defaults proposed in the doc that would land in production.

What NOT to report:
  * Hard-coded API keys / private keys in code blocks — rule S001 catches them.
  * `rm -rf /`, `curl | bash`, `chmod 777` patterns — rules S010-S013.
  * Theoretical / "what if an attacker did X" speculation. Only report what the
    document explicitly states.
  * Generic "this could be more secure" suggestions.

Each finding must quote the exact unsafe passage in `evidence` and explain in
one sentence what an attacker would do with it (in `message`)."""
