"""Ground truth: an endpoint has N+1 iff its query count grows with the number of rows."""
import importlib
import json
import os
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'bench.settings')
django.setup()

from django.core.management import call_command  # noqa: E402
from django.db import connection, transaction  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402
from rest_framework.test import APIRequestFactory  # noqa: E402

from library.models import (  # noqa: E402
    Author, Book, Branch, Fine, Label, Loan, Member, Publisher, Series,
)

ROOT = Path(__file__).resolve().parent.parent
SMALL_UNITS, LARGE_UNITS = 5, 10


def seed(units: int) -> None:
    """Each unit is a full chain: 1 publisher, 1 author, 2 series, 2 books, 2 loans."""
    labels = Label.objects.bulk_create([Label(name='renewed'), Label(name='interlibrary')])
    for i in range(units):
        publisher = Publisher.objects.create(name=f'publisher {i}')
        author = Author.objects.create(publisher=publisher, name=f'author {i}')
        for j in range(2):
            series = Series.objects.create(author=author, name=f'series {i}-{j}')
            book = Book.objects.create(author=author, series=series, title=f'book {i}-{j}')
            loan = Loan.objects.create(
                book=book,
                member=Member.objects.create(name=f'member {i}-{j}', email=f'm{i}{j}@example.com', country='JP'),
                branch=Branch.objects.create(name=f'branch {i}-{j}'),
                borrowed_on=date(2026, 1, 1),
                due_on=date(2026, 1, 1) + timedelta(days=14),
            )
            loan.labels.set(labels)
            Fine.objects.bulk_create([
                Fine(loan=loan, amount=Decimal('100')),
                Fine(loan=loan, amount=Decimal('50'), is_waived=True),
            ])


def count_queries(case_id: str, units: int) -> int:
    view = importlib.import_module(f'cases.{case_id}').View.as_view()
    with transaction.atomic():
        seed(units)
        with CaptureQueriesContext(connection) as captured:
            response = view(APIRequestFactory().get('/'))
        if response.status_code != 200:
            raise RuntimeError(f'{case_id}: HTTP {response.status_code} {response.data}')
        transaction.set_rollback(True)
    return len(captured)


def label_cases() -> list[dict]:
    call_command('migrate', run_syncdb=True, verbosity=0)
    manifest = json.loads((ROOT / 'cases' / 'manifest.json').read_text())
    for case in manifest:
        small = count_queries(case['id'], SMALL_UNITS)
        large = count_queries(case['id'], LARGE_UNITS)
        case.update(queries_small=small, queries_large=large, has_n_plus_one=large > small)
    return manifest


if __name__ == '__main__':
    labels = label_cases()
    (ROOT / 'results').mkdir(exist_ok=True)
    (ROOT / 'results' / 'labels.json').write_text(json.dumps(labels, indent=2) + '\n')
    for case in labels:
        flag = '' if case['has_n_plus_one'] == case['intended'] else '   <-- differs from intended'
        print(f"{case['id']:<48} {case['queries_small']:>3} -> {case['queries_large']:>3}  "
              f"n+1={case['has_n_plus_one']!s:<5}{flag}")
    print(f"positives: {sum(c['has_n_plus_one'] for c in labels)} / {len(labels)}")
