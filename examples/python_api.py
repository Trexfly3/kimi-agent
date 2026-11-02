"""Use Kimi Agent from Python, with your own tools.

    export KIMI_API_KEY=...      # or MOONSHOT_API_KEY; or pass provider=/model=/base_url= to run()
    python examples/python_api.py
"""

from __future__ import annotations

import datetime as dt

import kimi_agent
from kimi_agent.ui import UI


@kimi_agent.tool
def days_until(date: str) -> str:
    """Count the days from today until a date.

    Args:
        date: An ISO date such as 2030-01-01.
    """
    delta = dt.date.fromisoformat(date) - dt.date.today()
    return f"{delta.days} days"


def main() -> None:
    # One request, one answer. Nothing is printed while the agent works, and because nobody can approve
    # anything, actions that would need approval are refused (see the "approval" setting).
    answer = kimi_agent.run("How many days are left until 2030-01-01?", tools=[days_until])
    print(answer)

    # A multi-turn conversation with the full terminal display and approval prompts. Keyword arguments
    # override the configuration, e.g. provider="openrouter", model="moonshotai/kimi-k3", approval="ask".
    with kimi_agent.create_runtime(tools=[days_until], ui=UI(), interactive=True, approval="ask") as runtime:
        agent = runtime.agent
        agent.run("List the Python files in this directory.")
        agent.run("Which one is the largest? Answer in one sentence.")
        print(f"\nTokens used: {agent.usage.prompt_tokens:,} in, {agent.usage.completion_tokens:,} out")


if __name__ == "__main__":
    main()
