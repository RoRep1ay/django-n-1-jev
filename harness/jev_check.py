"""Jev contender: one Choice (false / maybe / true) per top-level serializer field, with the effective queryset and
serializer read paths worked out in advance and added to the state. A field's probability is the expected verdict
score over Jev's answer probabilities.
"""
import importlib
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from django.core.exceptions import FieldDoesNotExist
from django.db.models import Model, Prefetch, QuerySet
from rest_framework import generics, serializers
from typesafe_sdk import Choice, ChoiceAnswer, TypeSafeClient

from harness.verdict import SCORE, Verdict, get_most_severe

ROOT = Path(__file__).resolve().parent.parent
MODEL = os.environ.get('JEV_MODEL', 'jev-latest')
USD_PER_INPUT_TOKEN = 0.042 / 1_000_000

TRUE_CRITERION = (
    'Serializing a row touches a relation, deferred column or model property that the queryset does not already '
    'load (select_related, prefetch_related, Prefetch, annotate, only), or re-queries a prefetched relation '
    '(filter, exclude, order_by, aggregate, first), so queries are issued once per row. Check each read one hop at '
    'a time: every path in its relations_traversed must appear in loaded_relations, and loading a does not load '
    'a.b. The same holds for code that loops over a prefetched collection and reads a relation of each item, such '
    'as b.series for b in obj.books.all(), which needs books.series in loaded_relations. Reading a column listed in '
    'deferred_columns also queries once per row.')
MAYBE_CRITERION = (
    'The code does not settle it: something read while serializing a row could be served from what the queryset '
    'loads or could hit the database, and both readings are plausible.')
FALSE_CRITERION = (
    'Everything read while serializing a row is already in memory from the queryset, so the total number of '
    'queries stays constant no matter how many rows the endpoint returns. Reading a prefetched relation through '
    'all(), iteration, len(), count(), exists() or slicing is served from the prefetch cache, and a nested prefetch '
    'lookup such as a__b also caches b for every a.')


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
    loaded_relations: dict[str, str]
    deferred_columns: list[str]


@dataclass
class FieldFact:
    key: str
    field_class: str
    reads: str
    relations_traversed: list[str]


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


def build_loaded_relations(select_related: list[str], prefetches: list[PrefetchFact]) -> dict[str, str]:
    """Every relation path the plan keeps in memory, mapped to what loads it. Prefetching a.b also loads a, and a
    Prefetch with to_attr stores its last hop under the to_attr name instead of the relation's own name."""
    loaded = dict.fromkeys(select_related, 'select_related')
    for prefetch in prefetches:
        *parents, last = prefetch.path.split('.')
        for i in range(len(parents)):
            loaded.setdefault('.'.join(parents[:i + 1]), f'prefetch_related, as a step of {prefetch.path}')
        target = '.'.join([*parents, prefetch.to_attr or last])
        loaded.setdefault(target, 'prefetch_related, a list under to_attr' if prefetch.to_attr else 'prefetch_related')
        for path in prefetch.select_related:
            loaded.setdefault(target + path.removeprefix(prefetch.path),
                              f'select_related inside the {prefetch.path} Prefetch')
    return loaded


def build_deferred_columns(model: type[Model], select_related: list[str], only: list[str]) -> list[str]:
    """The columns of the row model and its select_related models that only() leaves out, so reading one costs a
    query. The primary key and the foreign key behind each select_related join are always loaded."""
    tables = [('', model), *((f'{path}.', get_relation_hops(model, path)[-1][1]) for path in select_related)]
    return [prefix + field.name for prefix, table in tables for field in table._meta.concrete_fields
            if not (field.primary_key or prefix + field.name in only or prefix + field.name in select_related)]


def build_queryset_facts(queryset: QuerySet) -> QuerysetFacts:
    """The loading plan of the queryset the view actually serves; dotted paths, read from Django's query object."""
    deferred, is_defer = queryset.query.deferred_loading
    deferred = sorted(name.replace('__', '.') for name in deferred)
    only = [] if is_defer else deferred
    select_related = get_select_related_paths(queryset.query.select_related or {})
    prefetch_related = [build_prefetch_fact(lookup) for lookup in queryset._prefetch_related_lookups]
    return QuerysetFacts(
        select_related=select_related,
        prefetch_related=prefetch_related,
        annotations=list(queryset.query.annotations),
        only=only,
        defer=deferred if is_defer else [],
        loaded_relations=build_loaded_relations(select_related, prefetch_related),
        deferred_columns=(deferred if is_defer
                          else build_deferred_columns(queryset.model, select_related, only) if only else []),
    )


def get_relation_hops(model: type[Model], path: str) -> list[tuple[str, type[Model]]]:
    """Each relation a dotted read path walks through, as (path up to that hop, related model), outermost first. The
    walk stops at the first step that is a column, property, annotation or method."""
    hops = []
    names = path.replace('[]', '').split('.')
    for i, name in enumerate(names):
        try:
            field = model._meta.get_field(name)
        except FieldDoesNotExist:
            break
        if not field.is_relation or name != field.name:  # a column, or a foreign key's own column such as book_id
            break
        model = field.related_model
        hops.append(('.'.join(names[:i + 1]), model))
    return hops


def build_field_facts(model: type[Model], serializer: serializers.Serializer, key_prefix: str = '',
                      read_prefix: str = '') -> list[FieldFact]:
    """Every output key, nested ones included, with the attribute path it reads from each row of `model` and the
    relations that path walks through."""
    facts = []
    for name, field in serializer.fields.items():
        source = f'{field.method_name}(obj)' if isinstance(field, serializers.SerializerMethodField) else field.source
        key, reads = key_prefix + name, read_prefix + source
        facts.append(FieldFact(key=key, field_class=type(field).__name__, reads=reads,
                               relations_traversed=[path for path, _ in get_relation_hops(model, reads)]))
        if isinstance(field, serializers.ListSerializer):
            facts += build_field_facts(model, field.child, f'{key}[].', f'{reads}[].')
        elif isinstance(field, serializers.Serializer):
            facts += build_field_facts(model, field, f'{key}.', f'{reads}.')
    return facts


def build_resolved_facts(queryset: QuerySet, serializer: serializers.Serializer, view_name: str, source: str) -> str:
    """The state section holding the served queryset's loading plan and every output key's read path."""
    facts = {
        f'queryset_served (from {view_name}().get_queryset())': asdict(build_queryset_facts(queryset)),
        'serializer_fields': [asdict(fact) for fact in build_field_facts(queryset.model, serializer)],
    }
    return (f'# ===== resolved: loading plan and read paths, computed by the harness from {source} '
            f'(facts, not a verdict) =====\n{json.dumps(facts, indent=2)}')


def build_resolved_state(case_id: str) -> str:
    view_class = get_view_class(case_id)
    facts = build_resolved_facts(view_class().get_queryset(), view_class.serializer_class(), 'View', 'endpoint.py')
    return f'{build_state(case_id)}\n\n{facts}'


def build_question(question: str) -> Choice:
    return Choice(instructions=question, criteria={
        Verdict.TRUE.value: TRUE_CRITERION,
        Verdict.MAYBE.value: MAYBE_CRITERION,
        Verdict.FALSE.value: FALSE_CRITERION,
    })


def ask(client: TypeSafeClient, state: str, questions: dict[str, Choice]) -> JevResult:
    started = time.perf_counter()
    response = client.system_one(state=state, questions=questions, model=MODEL)
    latency_seconds = time.perf_counter() - started
    verdicts: dict[str, Verdict] = {}
    verdict_probabilities: dict[str, dict[Verdict, float]] = {}
    for name in questions:
        answer: ChoiceAnswer = response.answers[name]
        verdicts[name] = Verdict(answer.choice)
        verdict_probabilities[name] = {Verdict(choice): p for choice, p in answer.probabilities.items()}
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


def build_resolved_questions(view_name: str, serializer: serializers.Serializer) -> dict[str, Choice]:
    """One question per top-level field, plus one for the keys a `to_representation` override adds, which no declared
    field covers."""
    serializer_name = type(serializer).__name__
    questions = {
        name: build_question(
            f'Does producing the `{name}` field of `{serializer_name}` for each row returned by `{view_name}` execute '
            f'at least one extra database query per row, given the queryset that `{view_name}.get_queryset` builds?')
        for name in serializer.fields
    }
    if 'to_representation' in vars(type(serializer)):
        questions['to_representation'] = build_question(
            f'Do the keys that `{serializer_name}.to_representation` adds beyond the declared fields execute at least '
            f'one extra database query per row returned by `{view_name}`?')
    return questions


def check_resolved(client: TypeSafeClient, case_id: str) -> JevResult:
    return ask(client, build_resolved_state(case_id),
               build_resolved_questions('View', get_view_class(case_id).serializer_class()))
