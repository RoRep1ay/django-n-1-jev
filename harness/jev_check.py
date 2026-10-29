"""Jev contenders: one question per endpoint ("whole"), one per top-level serializer field ("fields"), or one per
top-level field with the effective queryset and serializer read paths worked out in advance ("resolved").

Each scope is asked two ways:
- noul: a yes/no Noul; its probability falls into a verdict band (harness/verdict.py).
- choice: a Choice between false / maybe / true; Jev names the verdict.
Either way the probability is the expected verdict score over Jev's answer probabilities.
"""
import importlib
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from django.db.models import Prefetch, QuerySet
from rest_framework import generics, serializers
from typesafe_sdk import Choice, ChoiceAnswer, Noul, NoulAnswer, NoulCriteria, TypeSafeClient

from harness.verdict import SCORE, Verdict, get_most_severe, get_verdict

ROOT = Path(__file__).resolve().parent.parent
MODEL = os.environ.get('JEV_MODEL', 'jev-latest')
USD_PER_INPUT_TOKEN = 0.042 / 1_000_000

QuestionKind = Literal['noul', 'choice']

TRUE_CRITERION = (
    'Serializing a row touches a relation, deferred column or model property that the queryset does not already '
    'load (select_related, prefetch_related, Prefetch, annotate, only), or re-queries a prefetched relation '
    '(filter, exclude, order_by, aggregate, first), so queries are issued once per row.')
MAYBE_CRITERION = (
    'The code does not settle it: something read while serializing a row could be served from what the queryset '
    'loads or could hit the database, and both readings are plausible.')
FALSE_CRITERION = (
    'Everything read while serializing a row is already in memory from the queryset, so the total number of '
    'queries stays constant no matter how many rows the endpoint returns.')

CACHE_AWARE_FALSE_CRITERION = FALSE_CRITERION + (
    ' Reading a prefetched relation through all(), iteration, len(), count(), exists() or slicing is served from the '
    'prefetch cache, and a nested prefetch lookup such as a__b also caches b for every a.')


@dataclass
class PrefetchFact:
    path: str
    to_attr: str | None
    select_related: list[str]


@dataclass
class QuerysetFacts:
    select_related: list[str]
    prefetch_related: list[PrefetchFact]
    annotations: list[str]
    only: list[str]
    defer: list[str]


@dataclass
class FieldFact:
    key: str
    field_class: str
    reads: str


@dataclass
class JevResult:
    probability: float
    verdict: Verdict
    field_probabilities: dict[str, float]
    field_verdicts: dict[str, Verdict]
    verdict_probabilities: dict[str, dict[Verdict, float]]
    latency_seconds: float
    input_tokens: int


def load_api_key() -> str:
    if key := os.environ.get('TYPESAFE_API_KEY'):
        return key
    env_file = ROOT / '.env'
    for line in env_file.read_text().splitlines() if env_file.exists() else []:
        name, _, value = line.partition('=')
        if name.strip().removeprefix('export ').strip() == 'TYPESAFE_API_KEY':
            return value.strip().strip('"\'')
    raise RuntimeError('TYPESAFE_API_KEY is not set in the environment or .env')


def get_view_class(case_id: str) -> type[generics.GenericAPIView]:
    return importlib.import_module(f'cases.{case_id}').View


def build_state(case_id: str) -> str:
    files = {'library/models.py': 'library/models.py', 'library/services.py': 'library/services.py',
             'endpoint.py': f'cases/{case_id}.py'}
    return '\n\n'.join(f'# ===== file: {shown} =====\n{(ROOT / path).read_text()}' for shown, path in files.items())


def get_select_related_paths(tree: dict[str, dict], prefix: str = '') -> list[str]:
    return [path for name, subtree in tree.items()
            for path in (prefix + name, *get_select_related_paths(subtree, f'{prefix}{name}.'))]


def build_prefetch_fact(lookup: str | Prefetch) -> PrefetchFact:
    if isinstance(lookup, str):
        return PrefetchFact(path=lookup.replace('__', '.'), to_attr=None, select_related=[])
    path = lookup.prefetch_through.replace('__', '.')
    tree = lookup.queryset.query.select_related if lookup.queryset is not None else {}
    return PrefetchFact(path=path, to_attr=lookup.to_attr,
                        select_related=get_select_related_paths(tree or {}, f'{path}.'))


def build_queryset_facts(queryset: QuerySet) -> QuerysetFacts:
    """The loading plan of the queryset the view actually serves; dotted paths, read from Django's query object."""
    deferred, is_defer = queryset.query.deferred_loading
    deferred = sorted(name.replace('__', '.') for name in deferred)
    return QuerysetFacts(
        select_related=get_select_related_paths(queryset.query.select_related or {}),
        prefetch_related=[build_prefetch_fact(lookup) for lookup in queryset._prefetch_related_lookups],
        annotations=list(queryset.query.annotations),
        only=[] if is_defer else deferred,
        defer=deferred if is_defer else [],
    )


def build_field_facts(serializer: serializers.Serializer, key_prefix: str = '', read_prefix: str = '') -> list[FieldFact]:
    """Every output key, nested ones included, with the attribute path it reads from each row."""
    facts = []
    for name, field in serializer.fields.items():
        source = f'{field.method_name}(obj)' if isinstance(field, serializers.SerializerMethodField) else field.source
        key, reads = key_prefix + name, read_prefix + source
        facts.append(FieldFact(key=key, field_class=type(field).__name__, reads=reads))
        if isinstance(field, serializers.ListSerializer):
            facts += build_field_facts(field.child, f'{key}[].', f'{reads}[].')
        elif isinstance(field, serializers.Serializer):
            facts += build_field_facts(field, f'{key}.', f'{reads}.')
    return facts


def build_resolved_facts(queryset: QuerySet, serializer: serializers.Serializer, view_name: str, source: str) -> str:
    """The state section holding the served queryset's loading plan and every output key's read path."""
    facts = {
        f'queryset_served (from {view_name}().get_queryset())': asdict(build_queryset_facts(queryset)),
        'serializer_fields': [asdict(fact) for fact in build_field_facts(serializer)],
    }
    return (f'# ===== resolved: loading plan and read paths, computed by the harness from {source} '
            f'(facts, not a verdict) =====\n{json.dumps(facts, indent=2)}')


def build_resolved_state(case_id: str) -> str:
    view_class = get_view_class(case_id)
    facts = build_resolved_facts(view_class().get_queryset(), view_class.serializer_class(), 'View', 'endpoint.py')
    return f'{build_state(case_id)}\n\n{facts}'


def build_question(kind: QuestionKind, claim: str, question: str, false_criterion: str = FALSE_CRITERION) -> Noul | Choice:
    """A Noul is asked as a claim to judge; a Choice as a question to answer."""
    if kind == 'noul':
        return Noul(instructions=claim, criteria=NoulCriteria(true=TRUE_CRITERION, false=false_criterion))
    return Choice(instructions=question, criteria={
        Verdict.TRUE.value: TRUE_CRITERION,
        Verdict.MAYBE.value: MAYBE_CRITERION,
        Verdict.FALSE.value: false_criterion,
    })


def get_verdict_probabilities(answer: NoulAnswer | ChoiceAnswer) -> tuple[Verdict, dict[Verdict, float]]:
    if isinstance(answer, NoulAnswer):
        return get_verdict(answer.noul), {Verdict.TRUE: answer.noul, Verdict.FALSE: 1 - answer.noul}
    return Verdict(answer.choice), {Verdict(choice): p for choice, p in answer.probabilities.items()}


def ask(client: TypeSafeClient, state: str, questions: dict[str, Noul | Choice]) -> JevResult:
    started = time.perf_counter()
    response = client.system_one(state=state, questions=questions, model=MODEL)
    latency_seconds = time.perf_counter() - started
    verdicts, verdict_probabilities = {}, {}
    for name in questions:
        verdicts[name], verdict_probabilities[name] = get_verdict_probabilities(response.answers[name])
    probabilities = {name: sum(SCORE[v] * p for v, p in choices.items())
                     for name, choices in verdict_probabilities.items()}
    return JevResult(
        probability=max(probabilities.values()),
        verdict=get_most_severe(verdicts.values()),
        field_probabilities=probabilities,
        field_verdicts=verdicts,
        verdict_probabilities=verdict_probabilities,
        latency_seconds=latency_seconds,
        input_tokens=response.usage.input_tokens,
    )


def check_whole(client: TypeSafeClient, case_id: str, kind: QuestionKind) -> JevResult:
    question = build_question(
        kind,
        claim=('Serving the `View` list endpoint in endpoint.py executes extra database queries for every row it '
               'returns, so the total query count grows with the number of rows (an N+1 query problem).'),
        question=('Does serving the `View` list endpoint in endpoint.py execute extra database queries for every row '
                  'it returns, so the total query count grows with the number of rows (an N+1 query problem)?'),
    )
    return ask(client, build_state(case_id), {'n_plus_one': question})


def build_field_questions(view_name: str, serializer: serializers.Serializer, kind: QuestionKind,
                          false_criterion: str) -> dict[str, Noul | Choice]:
    serializer_name = type(serializer).__name__
    return {
        name: build_question(
            kind,
            claim=(f'Producing the `{name}` field of `{serializer_name}` for each row returned by `{view_name}` '
                   f'executes at least one extra database query per row, given the queryset that '
                   f'`{view_name}.get_queryset` builds.'),
            question=(f'Does producing the `{name}` field of `{serializer_name}` for each row returned by '
                      f'`{view_name}` execute at least one extra database query per row, given the queryset that '
                      f'`{view_name}.get_queryset` builds?'),
            false_criterion=false_criterion,
        )
        for name in serializer.fields
    }


def build_resolved_questions(view_name: str, serializer: serializers.Serializer,
                             kind: QuestionKind) -> dict[str, Noul | Choice]:
    """One question per top-level field, with the prefetch-cache rule in the false criterion, plus one for the keys a
    `to_representation` override adds, which no declared field covers."""
    questions = build_field_questions(view_name, serializer, kind, CACHE_AWARE_FALSE_CRITERION)
    serializer_name = type(serializer).__name__
    if 'to_representation' in vars(type(serializer)):
        questions['to_representation'] = build_question(
            kind,
            claim=(f'The keys that `{serializer_name}.to_representation` adds beyond the declared fields '
                   f'execute at least one extra database query per row returned by `{view_name}`.'),
            question=(f'Do the keys that `{serializer_name}.to_representation` adds beyond the declared '
                      f'fields execute at least one extra database query per row returned by `{view_name}`?'),
            false_criterion=CACHE_AWARE_FALSE_CRITERION,
        )
    return questions


def check_fields(client: TypeSafeClient, case_id: str, kind: QuestionKind) -> JevResult:
    questions = build_field_questions('View', get_view_class(case_id).serializer_class(), kind, FALSE_CRITERION)
    return ask(client, build_state(case_id), questions)


def check_resolved(client: TypeSafeClient, case_id: str, kind: QuestionKind) -> JevResult:
    """Fields, plus the worked-out loading plan in the state (see build_resolved_questions for the questions)."""
    questions = build_resolved_questions('View', get_view_class(case_id).serializer_class(), kind)
    return ask(client, build_resolved_state(case_id), questions)
