"""Document-level graded relevance metrics; duplicate retrieved documents count once."""

import math


def metrics(ranking: list[str], relevance: dict[str, int], k: int) -> dict[str, float]:
    if k < 1 or any(grade < 0 for grade in relevance.values()):
        raise ValueError("Invalid evaluation configuration")
    unique = list(dict.fromkeys(ranking))[:k]
    relevant = {key for key, grade in relevance.items() if grade > 0}
    recall = len(set(unique) & relevant) / len(relevant) if relevant else 0.0
    reciprocal = next(
        (1.0 / rank for rank, key in enumerate(unique, 1) if key in relevant),
        0.0,
    )
    dcg = sum(
        (2 ** relevance.get(key, 0) - 1) / math.log2(rank + 1) for rank, key in enumerate(unique, 1)
    )
    ideal = sum(
        (2**grade - 1) / math.log2(rank + 1)
        for rank, grade in enumerate(sorted(relevance.values(), reverse=True)[:k], 1)
    )
    return {"recall": recall, "mrr": reciprocal, "ndcg": dcg / ideal if ideal else 0.0}
