"""Retrieval: find past questions with checked SQL that look like a new question.

Each example in examples/examples.json is embedded once with Gemini's
embedding model and cached in data/. For a new question, the agent embeds
it, ranks the examples by cosine similarity, and shows the model the
closest few as patterns to adapt. None of the examples repeats a test
question in evals/questions.json, so the test score stays honest.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from google import genai
from google.genai import errors, types

HERE = Path(__file__).parent
EXAMPLES_PATH = HERE / "examples" / "examples.json"
CACHE_PATH = HERE / "data" / "example_embeddings.json"
EMBED_MODEL = "gemini-embedding-001"
TOP_K = 3
# Related taxi questions score about 0.78 to 0.84; an unrelated one ("What is
# the capital of France?") scored 0.58. Below this, an example misleads more than it helps.
MIN_SCORE = 0.65


def load_examples() -> list[dict]:
    return json.loads(EXAMPLES_PATH.read_text())


def embed(client: genai.Client, texts: list[str], task_type: str) -> np.ndarray:
    response = client.models.embed_content(
        model=EMBED_MODEL,
        contents=texts,
        config=types.EmbedContentConfig(task_type=task_type),
    )
    vectors = np.array([e.values for e in response.embeddings], dtype=float)
    return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)


def example_vectors(client: genai.Client, examples: list[dict]) -> np.ndarray:
    """Embeddings for every example, from the cache when the questions haven't changed."""
    questions = [e["question"] for e in examples]
    if CACHE_PATH.exists():
        cache = json.loads(CACHE_PATH.read_text())
        if cache.get("model") == EMBED_MODEL and cache.get("questions") == questions:
            return np.array(cache["vectors"])
    vectors = embed(client, questions, "RETRIEVAL_DOCUMENT")
    CACHE_PATH.parent.mkdir(exist_ok=True)
    CACHE_PATH.write_text(json.dumps({"model": EMBED_MODEL, "questions": questions, "vectors": vectors.tolist()}))
    return vectors


def similar_examples(question: str, client: genai.Client, k: int = TOP_K) -> list[dict]:
    """The k closest examples with their similarity score. Empty if embedding fails.

    Retrieval only helps; it is never required, so an API error here returns
    nothing and the agent answers without examples.
    """
    try:
        examples = load_examples()
        vectors = example_vectors(client, examples)
        query = embed(client, [question], "RETRIEVAL_QUERY")[0]
    except errors.APIError:
        return []
    scores = vectors @ query
    ranked = np.argsort(scores)[::-1][:k]
    return [{**examples[i], "score": round(float(scores[i]), 3)} for i in ranked if scores[i] >= MIN_SCORE]


def format_examples(examples: list[dict]) -> str:
    lines = ["Similar questions with checked SQL. Use them as patterns; the new question may need different columns or filters."]
    for e in examples:
        lines.append(f"Q: {e['question']}\nSQL: {e['sql']}")
    return "\n\n".join(lines)
