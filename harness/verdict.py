"""Three-state verdict shared by every contender: is serving the endpoint N+1 — false, maybe or true.

Each verdict also has a score (false 0, maybe 0.5, true 1) so the probability-based metrics (thresholds, AUC) still
apply: a contender's probability is its expected score. The skill's marks and Jev's Choice name a verdict directly;
an expected score averaged over repeated calls (the CI check) gets its verdict from the bands below.
"""
from collections.abc import Iterable
from enum import Enum

MAYBE_FROM = 0.3
TRUE_FROM = 0.7


class Verdict(str, Enum):
    FALSE = 'false'
    MAYBE = 'maybe'
    TRUE = 'true'


SCORE = {Verdict.FALSE: 0.0, Verdict.MAYBE: 0.5, Verdict.TRUE: 1.0}


def get_verdict(probability: float) -> Verdict:
    if probability >= TRUE_FROM:
        return Verdict.TRUE
    if probability >= MAYBE_FROM:
        return Verdict.MAYBE
    return Verdict.FALSE


def get_most_severe(verdicts: Iterable[Verdict]) -> Verdict:
    return max(verdicts, key=SCORE.__getitem__)
