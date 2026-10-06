"""Print retrieval scores for on-topic and off-topic questions (used to pick thresholds).

    uv run python scripts/eval_rag.py
"""

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hyperion import rag  # noqa: E402

ON_TOPIC = [
    "What is HyperAI?",
    "What are Open Connectors?",
    "What is a DeviceNode?",
    "Explain the high level architecture of the open connectors",
    "Which fields are required in a native application profile?",
    "What does deliverable D4.3 cover?",
    "How do I deploy my first web server?",
    "What actions can Hyperion send to the IDE?",
    "What qos fields does a device app need?",
    "What is the design methodology of the architecture?",
    "What is the Application Profile Manager?",
    "how do I validate a file?",
    "What is a swarm?",
    "Which workload kinds exist for device apps?",
]
OFF_TOPIC = [
    "What is the weather today?",
    "Write me a poem about pizza",
    "Who won the world cup in 2018?",
    "What's 17 * 23?",
    "Tell me a joke",
    "What did I say my app was called?",
    "How do I cook lasagna?",
    "What is Kubernetes?",
    "Explain quantum computing",
    "Who is the president of France?",
    "What is Docker?",
]


async def main() -> None:
    for label, questions in (("on ", ON_TOPIC), ("off", OFF_TOPIC)):
        for question in questions:
            hits = await rag.retrieve(question)
            sources = " | ".join(f"{chunk.title[:24]}/{chunk.section[:18]}" for chunk, _ in hits[:3])
            print(f"{label} top={hits[0][1]:.3f} 4th={hits[-1][1]:.3f} {question[:50]:50s} {sources}")


if __name__ == "__main__":
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(main())
