"""CI check: score the list endpoints a change can affect with Jev, on the base revision and on the working tree, and
report the endpoints whose N+1 verdict got worse.

    python -m harness.ci_check --base origin/main --dry-run       # affected endpoints and what each request would send
    python -m harness.ci_check --base origin/main --show-state    # dry run that also prints every state in full
    python -m harness.ci_check --base origin/main                 # score base and head, print a markdown report
    python -m harness.ci_check --base origin/main --fail-on-true  # also exit 1 when an endpoint newly reaches "true"

Endpoints come from the URLconf: every DRF view with a list action. An endpoint depends on the files of its view and
serializer classes (nested serializers and project base classes included), the models along its read and prefetch
paths, and the project modules its view and serializer methods reference. It is affected when one of those files
changed, and Jev sees those files in full plus the resolved loading plan, so a serializer-only change still puts the
unchanged queryset in context. Each endpoint is asked the benchmark's Jev questions, averaged over --repeat calls.

The base revision is scored in a throwaway `git archive` snapshot with this harness copied over it, so the base needs
no copy of the check.
"""
import argparse
import inspect
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from types import ModuleType

import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'bench.settings')
django.setup()

from django.db.models import Model, QuerySet  # noqa: E402
from django.http import HttpResponseBase  # noqa: E402
from django.urls import URLPattern, URLResolver, get_resolver  # noqa: E402
from rest_framework import serializers  # noqa: E402
from rest_framework.generics import GenericAPIView  # noqa: E402
from rest_framework.mixins import ListModelMixin  # noqa: E402
from rest_framework.test import APIRequestFactory  # noqa: E402
from typesafe_sdk import Choice, TypeSafeClient  # noqa: E402

from harness.jev_check import (  # noqa: E402
    ask, build_field_facts, build_queryset_facts, build_resolved_facts, build_resolved_questions, get_relation_hops,
    load_api_key,
)
from harness.verdict import MAYBE_FROM, SCORE, Verdict, get_verdict  # noqa: E402

HARNESS_DIR = Path(__file__).resolve().parent
ROOT = HARNESS_DIR.parent
ENV_DIR = Path(sys.prefix).resolve()
WORKERS = 8

ViewFunction = Callable[..., HttpResponseBase]


@dataclass
class Endpoint:
    id: str
    route: str
    files: list[str]
    state: str
    questions: dict[str, Choice]


@dataclass
class SkippedEndpoint:
    id: str
    route: str
    reason: str


@dataclass
class EndpointScore:
    probability: float
    verdict: Verdict
    field_probabilities: dict[str, float]


def get_view_id(view_class: type) -> str:
    return f'{view_class.__module__}:{view_class.__qualname__}'


def get_project_file(obj: object) -> str | None:
    """The repo-relative source file of a class, function or module, or None when it lives outside the project."""
    try:
        source = inspect.getsourcefile(obj)
    except TypeError:  # builtins have no source file
        return None
    if source is None:
        return None
    path = Path(source).resolve()
    if not path.is_relative_to(ROOT) or path.is_relative_to(ENV_DIR):
        return None
    return str(path.relative_to(ROOT))


def is_list_view(callback: ViewFunction) -> bool:
    view_class = getattr(callback, 'cls', None)
    if not (isinstance(view_class, type) and issubclass(view_class, GenericAPIView)
            and issubclass(view_class, ListModelMixin)):
        return False
    actions = getattr(callback, 'actions', None)
    return actions is None or 'list' in actions.values()


def iter_list_views(patterns: Iterable[URLPattern | URLResolver], prefix: str = '') -> Iterator[tuple[str, ViewFunction]]:
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            yield from iter_list_views(pattern.url_patterns, prefix + str(pattern.pattern))
        elif is_list_view(pattern.callback):
            yield prefix + str(pattern.pattern), pattern.callback


def build_view(callback: ViewFunction) -> GenericAPIView:
    """The view as DRF's dispatch would set it up for an anonymous GET without URL kwargs."""
    view = callback.cls(**callback.initkwargs)
    if (actions := getattr(callback, 'actions', None)) is not None:
        view.action_map = actions
    view.args, view.kwargs, view.format_kwarg = (), {}, None
    view.request = view.initialize_request(APIRequestFactory().get('/'))
    return view


def get_serializers(serializer: serializers.Serializer) -> Iterator[serializers.Serializer]:
    yield serializer
    for field in serializer.fields.values():
        if isinstance(field, serializers.ListSerializer):
            yield from get_serializers(field.child)
        elif isinstance(field, serializers.Serializer):
            yield from get_serializers(field)


def get_models_on_path(model: type[Model], path: str) -> list[type[Model]]:
    return [related_model for _, related_model in get_relation_hops(model, path)]


def get_referenced_objects(classes: Iterable[type]) -> Iterator[object]:
    """The globals that the methods defined on these classes refer to, such as a services module."""
    for cls in classes:
        for value in vars(cls).values():
            if inspect.isfunction(value):
                yield from inspect.getclosurevars(value).globals.values()


def get_dependency_files(view: GenericAPIView, queryset: QuerySet, serializer: serializers.Serializer) -> list[str]:
    """Project files the endpoint depends on: models first, then referenced helpers, serializers and the view."""
    serializer_classes = [base for s in get_serializers(serializer) for base in type(s).__mro__]
    view_classes = list(type(view).__mro__)
    project_classes = [cls for cls in serializer_classes + view_classes if get_project_file(cls)]

    queryset_facts = build_queryset_facts(queryset)
    paths = [fact.reads for fact in build_field_facts(queryset.model, serializer)] + queryset_facts.select_related
    paths += [path for fact in queryset_facts.prefetch_related for path in (fact.path, *fact.select_related)]
    models = [queryset.model, *(model for path in paths for model in get_models_on_path(queryset.model, path))]

    helpers = [obj for obj in get_referenced_objects(project_classes)
               if isinstance(obj, ModuleType) or inspect.isclass(obj) or inspect.isfunction(obj)]
    files = (get_project_file(obj) for obj in [*models, *helpers, *serializer_classes, *view_classes])
    return list(dict.fromkeys(file for file in files if file))


def build_endpoint(route: str, callback: ViewFunction) -> Endpoint:
    view = build_view(callback)
    # Fields bind without a context, and get_serializer_context may itself query the database.
    queryset, serializer = view.get_queryset(), view.get_serializer_class()()
    view_name = type(view).__name__
    files = get_dependency_files(view, queryset, serializer)
    sections = [f'# ===== file: {path} =====\n{(ROOT / path).read_text()}' for path in files]
    sections.append(build_resolved_facts(queryset, serializer, view_name, get_project_file(type(view))))
    return Endpoint(id=get_view_id(type(view)), route=f'/{route}', files=files, state='\n\n'.join(sections),
                    questions=build_resolved_questions(view_name, serializer))


def discover_endpoints() -> tuple[list[Endpoint], list[SkippedEndpoint]]:
    endpoints: dict[str, Endpoint] = {}
    skipped: dict[str, SkippedEndpoint] = {}
    for route, callback in iter_list_views(get_resolver().url_patterns):
        view_id = get_view_id(callback.cls)
        if view_id in endpoints or view_id in skipped:
            continue
        try:
            endpoints[view_id] = build_endpoint(route, callback)
        except Exception as error:  # view code is arbitrary: e.g. get_queryset needs a URL kwarg or a real user
            skipped[view_id] = SkippedEndpoint(view_id, f'/{route}', f'{type(error).__name__}: {error}')
    return list(endpoints.values()), list(skipped.values())


def git(*args: str) -> str:
    return subprocess.run(['git', *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()


def get_changed_files(merge_base: str) -> set[str]:
    """Files that differ between the merge base and the working tree, untracked files included."""
    changed = git('diff', '--name-only', '--relative', merge_base).splitlines()
    return set(changed + git('ls-files', '--others', '--exclude-standard').splitlines())


def score_endpoints(endpoints: list[Endpoint], repeat: int) -> dict[str, EndpointScore]:
    jobs = [endpoint for endpoint in endpoints for _ in range(repeat)]
    with TypeSafeClient(api_key=load_api_key()) as client, ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(lambda endpoint: ask(client, endpoint.state, endpoint.questions), jobs))
    scores = {}
    for i, endpoint in enumerate(endpoints):
        runs = results[i * repeat:(i + 1) * repeat]
        probability = statistics.mean(run.probability for run in runs)
        scores[endpoint.id] = EndpointScore(
            probability=probability,
            verdict=get_verdict(probability),
            field_probabilities={name: statistics.mean(run.field_probabilities[name] for run in runs)
                                 for name in endpoint.questions},
        )
    return scores


def score_base(merge_base: str, endpoint_ids: list[str], repeat: int) -> dict[str, EndpointScore]:
    """Score the endpoints as the base revision defines them; an endpoint the base lacks is left out."""
    with tempfile.TemporaryDirectory(prefix='n1-ci-base-') as tmp:
        archive = subprocess.run(['git', 'archive', merge_base], cwd=ROOT, capture_output=True, check=True).stdout
        subprocess.run(['tar', '-x', '-C', tmp], input=archive, check=True)
        shutil.copytree(HARNESS_DIR, Path(tmp) / 'harness', dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('__pycache__'))
        completed = subprocess.run(
            [sys.executable, '-m', 'harness.ci_check', '--repeat', str(repeat), '--score', *endpoint_ids],
            cwd=tmp, env={**os.environ, 'TYPESAFE_API_KEY': load_api_key()}, capture_output=True, text=True,
        )
    if completed.returncode:
        raise RuntimeError(f'scoring the base revision failed:\n{completed.stderr}')
    return {endpoint_id: EndpointScore(probability=raw['probability'], verdict=Verdict(raw['verdict']),
                                       field_probabilities=raw['field_probabilities'])
            for endpoint_id, raw in json.loads(completed.stdout).items()}


def is_worse(head: EndpointScore, base: EndpointScore | None) -> bool:
    if base is None:
        return head.verdict != Verdict.FALSE
    return SCORE[head.verdict] > SCORE[base.verdict]


def format_score(score: EndpointScore | None) -> str:
    return 'new' if score is None else f'{score.verdict.value} ({score.probability:.2f})'


def build_report(changed: set[str], endpoints: list[Endpoint], skipped: list[SkippedEndpoint], repeat: int,
                 base: dict[str, EndpointScore], head: dict[str, EndpointScore]) -> str:
    worse = sorted((e for e in endpoints if is_worse(head[e.id], base.get(e.id))),
                   key=lambda e: head[e.id].probability, reverse=True)
    kept = [e for e in endpoints if e not in worse]
    lines = ['## N+1 check', '',
             f'{len(changed)} changed files affect {len(endpoints)} list endpoints, each scored by Jev on the base and '
             f'on this change (mean of {repeat} calls). **{len(worse)} got worse.**']

    def add_table(rows: list[Endpoint]) -> None:
        lines.extend(['', '| endpoint | route | base | head |', '|---|---|---|---|'])
        lines.extend(f'| `{e.id}` | `{e.route}` | {format_score(base.get(e.id))} | {format_score(head[e.id])} |'
                     for e in rows)

    if worse:
        add_table(worse)
    for endpoint in worse:
        base_fields = base[endpoint.id].field_probabilities if endpoint.id in base else {}
        flagged = sorted(((name, p) for name, p in head[endpoint.id].field_probabilities.items() if p >= MAYBE_FROM),
                         key=lambda item: item[1], reverse=True)
        lines.extend(['', f'### `{endpoint.id}`: {format_score(head[endpoint.id])}',
                      'Fields Jev flags: ' + ', '.join(
                          f'`{name}` {p:.2f}' + (f' (base {base_fields[name]:.2f})' if name in base_fields else '')
                          for name, p in flagged),
                      'Changed files it depends on: ' + ', '.join(f'`{f}`' for f in endpoint.files if f in changed)])
    if kept:
        lines.extend(['', f'<details><summary>{len(kept)} endpoints kept their verdict</summary>'])
        add_table(kept)
        lines.extend(['', '</details>'])
    if skipped:
        lines.extend(['', f'<details><summary>{len(skipped)} list endpoints could not be resolved statically, so they '
                      'were not checked</summary>', ''])
        lines.extend(f'- `{s.id}` (`{s.route}`): {s.reason}' for s in skipped)
        lines.extend(['', '</details>'])
    return '\n'.join(lines) + '\n'


def print_dry_run(changed: set[str], endpoints: list[Endpoint], affected: list[Endpoint],
                  skipped: list[SkippedEndpoint], show_state: bool) -> None:
    print(f'{len(changed)} changed files: {", ".join(sorted(changed))}')
    print(f'{len(affected)} of {len(endpoints)} list endpoints affected')
    for endpoint in affected:
        print(f'\n{endpoint.id}  {endpoint.route}  state {len(endpoint.state)} chars, '
              f'{len(endpoint.questions)} questions: {", ".join(endpoint.questions)}')
        print('  files: ' + ', '.join(f + (' (changed)' if f in changed else '') for f in endpoint.files))
        if show_state:
            print(endpoint.state)
    for endpoint in skipped:
        print(f'\nskipped {endpoint.id}  {endpoint.route}: {endpoint.reason}')


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', default='origin/main', help='branch or commit the change is compared against')
    parser.add_argument('--repeat', type=int, default=3, help='Jev calls per endpoint, averaged')
    parser.add_argument('--dry-run', action='store_true', help='list affected endpoints without calling Jev')
    parser.add_argument('--show-state', action='store_true', help='dry run that also prints every state')
    parser.add_argument('--fail-on-true', action='store_true', help='exit 1 when an endpoint newly reaches "true"')
    parser.add_argument('--score', nargs='+', metavar='ENDPOINT_ID', help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.score:  # internal: score_base runs this inside the base snapshot
        endpoints = [e for e in discover_endpoints()[0] if e.id in args.score]
        print(json.dumps({endpoint_id: asdict(score)
                          for endpoint_id, score in score_endpoints(endpoints, args.repeat).items()}))
        return

    merge_base = git('merge-base', args.base, 'HEAD')
    changed = get_changed_files(merge_base)
    endpoints, skipped = discover_endpoints()
    affected = [e for e in endpoints if changed.intersection(e.files)]
    if args.dry_run or args.show_state:
        print_dry_run(changed, endpoints, affected, skipped, args.show_state)
        return
    if not affected:
        print('## N+1 check\n\nNo list endpoint depends on the changed files.')
        return

    head = score_endpoints(affected, args.repeat)
    base = score_base(merge_base, [e.id for e in affected], args.repeat)
    print(build_report(changed, affected, skipped, args.repeat, base, head), end='')
    if args.fail_on_true and any(head[e.id].verdict == Verdict.TRUE and is_worse(head[e.id], base.get(e.id))
                                 for e in affected):
        sys.exit(1)


if __name__ == '__main__':
    main()
