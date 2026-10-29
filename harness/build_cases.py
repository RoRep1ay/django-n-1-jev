"""Generate benchmark cases: correct base endpoints plus single-edit mutations.

Each case is written to cases/<id>.py and described in cases/manifest.json.
`intended` is the author's guess; the runtime oracle (harness/oracle.py) sets the real label.
"""
import json
from pathlib import Path

CASES_DIR = Path(__file__).resolve().parent.parent / 'cases'

LOAN_BASE = '''from django.db.models import Count, Prefetch
from rest_framework import generics, serializers

from library import services
from library.models import Fine, Loan, Member


class MemberSerializer(serializers.ModelSerializer):
    class Meta:
        model = Member
        fields = ['id', 'name', 'country']


class LoanSerializer(serializers.ModelSerializer):
    book_title = serializers.CharField(source='book.title')
    author_name = serializers.CharField(source='book.author.name')
    branch_name = serializers.CharField(source='branch.name')
    member = MemberSerializer()
    labels = serializers.SlugRelatedField(slug_field='name', many=True, read_only=True)
    # EXTRA_FIELDS

    class Meta:
        model = Loan
        fields = ['id', 'borrowed_on', 'due_on', 'book_title', 'author_name', 'branch_name', 'member', 'labels']  # EXTRA_NAMES
    # EXTRA_METHODS


class View(generics.ListAPIView):
    serializer_class = LoanSerializer

    def get_queryset(self):
        return (
            Loan.objects
            .select_related('book__author', 'branch', 'member')
            .prefetch_related('labels')
            .order_by('id')
        )
'''

BOOK_BASE = '''from django.db.models import Count, Prefetch, Q
from rest_framework import generics, serializers

from library.models import Author, Book, Loan


class AuthorSerializer(serializers.ModelSerializer):
    publisher_name = serializers.CharField(source='publisher.name')

    class Meta:
        model = Author
        fields = ['id', 'name', 'publisher_name']


class LoanBriefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Loan
        fields = ['id', 'borrowed_on']


class BookSerializer(serializers.ModelSerializer):
    author = AuthorSerializer()
    series_name = serializers.CharField(source='series.name')
    loan_count = serializers.IntegerField()
    # EXTRA_FIELDS

    class Meta:
        model = Book
        fields = ['id', 'title', 'author', 'series_name', 'loan_count']  # EXTRA_NAMES


class View(generics.ListAPIView):
    serializer_class = BookSerializer

    def get_queryset(self):
        return (
            Book.objects
            .select_related('author__publisher', 'series')
            .annotate(loan_count=Count('loans'))
            .order_by('id')
        )
'''

AUTHOR_BASE = '''from rest_framework import generics, serializers

from library.models import Author, Book, Series


class SeriesSerializer(serializers.ModelSerializer):
    class Meta:
        model = Series
        fields = ['id', 'name']


class AuthorSerializer(serializers.ModelSerializer):
    series = SeriesSerializer(many=True)
    book_count = serializers.SerializerMethodField()
    # EXTRA_FIELDS

    class Meta:
        model = Author
        fields = ['id', 'name', 'series', 'book_count']  # EXTRA_NAMES

    def get_book_count(self, obj):
        return len(obj.books.all())
    # EXTRA_METHODS


class View(generics.ListAPIView):
    serializer_class = AuthorSerializer

    def get_queryset(self):
        return Author.objects.prefetch_related('series', 'books').order_by('id')
'''

BASES = {'loan': LOAN_BASE, 'book': BOOK_BASE, 'author': AUTHOR_BASE}


def add_field(declaration: str, name: str, method: str | None = None) -> list[tuple[str, str]]:
    edits = [
        ('    # EXTRA_FIELDS', f'    {declaration}'),
        ("]  # EXTRA_NAMES", f", '{name}']"),
    ]
    if method:
        edits.append(('    # EXTRA_METHODS', '\n' + method))
    return edits


# (id, base, category, intended_has_n_plus_one, edits)
CASES: list[tuple[str, str, str, bool, list[tuple[str, str]]]] = [
    # --- loan endpoint ---
    ('loan_base', 'loan', 'control', False, []),
    ('loan_drop_member_select', 'loan', 'fk-nested', True,
     [("'book__author', 'branch', 'member'", "'book__author', 'branch'")]),
    ('loan_shorten_author_select', 'loan', 'fk-deep-source', True,
     [("'book__author', 'branch'", "'book', 'branch'")]),
    ('loan_drop_branch_select', 'loan', 'fk-source', True,
     [("'book__author', 'branch', 'member'", "'book__author', 'member'")]),
    ('loan_drop_labels_prefetch', 'loan', 'm2m', True,
     [(".prefetch_related('labels')\n", '')]),
    ('loan_add_series_source', 'loan', 'fk-deep-source', True,
     add_field("series_name = serializers.CharField(source='book.series.name')", 'series_name')),
    ('loan_add_book_id', 'loan', 'fk-id-safe', False,
     add_field("book_id = serializers.IntegerField()", 'book_id')),
    ('loan_add_pure_property', 'loan', 'property-safe', False,
     add_field('loan_days = serializers.IntegerField(read_only=True)', 'loan_days')),
    ('loan_add_aggregate_property', 'loan', 'property-query', True,
     add_field('total_fines = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)', 'total_fines')),
    ('loan_aggregate_property_with_prefetch', 'loan', 'property-query', True,
     add_field('total_fines = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)', 'total_fines')
     + [(".prefetch_related('labels')", ".prefetch_related('labels', 'fines')")]),
    ('loan_service_all_with_prefetch', 'loan', 'service-safe', False,
     add_field('fined = serializers.SerializerMethodField()', 'fined',
               '    def get_fined(self, obj):\n        return services.get_fine_amount(obj)')
     + [(".prefetch_related('labels')", ".prefetch_related('labels', 'fines')")]),
    ('loan_service_all_without_prefetch', 'loan', 'service-query', True,
     add_field('fined = serializers.SerializerMethodField()', 'fined',
               '    def get_fined(self, obj):\n        return services.get_fine_amount(obj)')),
    ('loan_service_bypasses_prefetch', 'loan', 'service-query', True,
     add_field('fined = serializers.SerializerMethodField()', 'fined',
               '    def get_fined(self, obj):\n        return services.get_live_fine_amount(obj)')
     + [(".prefetch_related('labels')", ".prefetch_related('labels', 'fines')")]),
    ('loan_service_pure', 'loan', 'service-safe', False,
     add_field('display = serializers.SerializerMethodField()', 'display',
               '    def get_display(self, obj):\n        return services.get_display_label(obj)')),
    ('loan_count_with_prefetch', 'loan', 'method-safe', False,
     add_field('fine_count = serializers.SerializerMethodField()', 'fine_count',
               '    def get_fine_count(self, obj):\n        return obj.fines.count()')
     + [(".prefetch_related('labels')", ".prefetch_related('labels', 'fines')")]),
    ('loan_count_without_prefetch', 'loan', 'method-query', True,
     add_field('fine_count = serializers.SerializerMethodField()', 'fine_count',
               '    def get_fine_count(self, obj):\n        return obj.fines.count()')),
    ('loan_exists_with_prefetch', 'loan', 'method-safe', False,
     add_field('has_labels = serializers.SerializerMethodField()', 'has_labels',
               '    def get_has_labels(self, obj):\n        return obj.labels.exists()')),
    ('loan_filter_after_prefetch', 'loan', 'filter-after-prefetch', True,
     add_field('unwaived_fine_total = serializers.SerializerMethodField()', 'unwaived_fine_total',
               '    def get_unwaived_fine_total(self, obj):\n'
               '        return sum(f.amount for f in obj.fines.filter(is_waived=False))')
     + [(".prefetch_related('labels')", ".prefetch_related('labels', 'fines')")]),
    ('loan_filtered_prefetch_to_attr', 'loan', 'filter-after-prefetch-safe', False,
     add_field('unwaived_fine_total = serializers.SerializerMethodField()', 'unwaived_fine_total',
               '    def get_unwaived_fine_total(self, obj):\n'
               '        return sum(f.amount for f in obj.unwaived_fines)')
     + [(".prefetch_related('labels')",
         ".prefetch_related(\n                'labels',\n"
         "                Prefetch('fines', queryset=Fine.objects.filter(is_waived=False), to_attr='unwaived_fines'),\n"
         "            )")]),
    ('loan_to_attr_mismatch', 'loan', 'to-attr-mismatch', True,
     add_field('unwaived_fine_total = serializers.SerializerMethodField()', 'unwaived_fine_total',
               '    def get_unwaived_fine_total(self, obj):\n'
               '        return sum(f.amount for f in obj.fines.all() if not f.is_waived)')
     + [(".prefetch_related('labels')",
         ".prefetch_related(\n                'labels',\n"
         "                Prefetch('fines', queryset=Fine.objects.filter(is_waived=False), to_attr='unwaived_fines'),\n"
         "            )")]),
    ('loan_count_annotated', 'loan', 'annotate-safe', False,
     add_field('fine_count = serializers.IntegerField()', 'fine_count')
     + [(".prefetch_related('labels')", ".prefetch_related('labels')\n            .annotate(fine_count=Count('fines'))")]),
    ('loan_extra_select', 'loan', 'control', False,
     [("'book__author', 'branch', 'member'", "'book__author', 'book__series', 'branch', 'member'")]),
    ('loan_prefetch_fk_instead_of_select', 'loan', 'prefetch-fk-safe', False,
     [(".select_related('book__author', 'branch', 'member')\n            .prefetch_related('labels')",
       ".select_related('branch', 'member')\n            .prefetch_related('labels', 'book__author')")]),
    ('loan_get_queryset_keeps_class_attr', 'loan', 'queryset-inheritance-safe', False,
     [("    def get_queryset(self):\n        return (\n            Loan.objects\n",
       "    queryset = Loan.objects.select_related('book__author', 'branch', 'member').prefetch_related('labels')\n\n"
       "    def get_queryset(self):\n        return (\n            super().get_queryset()\n"),
      (".select_related('book__author', 'branch', 'member')\n            .prefetch_related('labels')\n            .order_by",
       ".exclude(status=Loan.Status.LOST)\n            .order_by")]),
    ('loan_get_queryset_ignores_class_attr', 'loan', 'queryset-inheritance', True,
     [("    def get_queryset(self):\n        return (\n            Loan.objects\n",
       "    queryset = Loan.objects.select_related('book__author', 'branch', 'member').prefetch_related('labels')\n\n"
       "    def get_queryset(self):\n        return (\n            Loan.objects\n"),
      (".select_related('book__author', 'branch', 'member')\n            .prefetch_related('labels')\n            .order_by",
       ".exclude(status=Loan.Status.LOST)\n            .order_by")]),
    # --- book endpoint ---
    ('book_base', 'book', 'control', False, []),
    ('book_shorten_publisher_select', 'book', 'fk-nested-deep', True,
     [("'author__publisher', 'series'", "'author', 'series'")]),
    ('book_drop_series_select', 'book', 'fk-source', True,
     [("'author__publisher', 'series'", "'author__publisher'")]),
    ('book_property_instead_of_annotation', 'book', 'property-query', True,
     add_field("open_count = serializers.IntegerField(source='open_loan_count')", 'open_count')),
    ('book_filtered_annotation', 'book', 'annotate-safe', False,
     add_field('open_count = serializers.IntegerField()', 'open_count')
     + [(".annotate(loan_count=Count('loans'))",
         ".annotate(\n                loan_count=Count('loans'),\n"
         "                open_count=Count('loans', filter=~Q(loans__status=Loan.Status.RETURNED)),\n"
         "            )")]),
    ('book_nested_reverse_without_prefetch', 'book', 'reverse-nested', True,
     add_field('loans = LoanBriefSerializer(many=True)', 'loans')),
    ('book_nested_reverse_with_prefetch', 'book', 'reverse-nested-safe', False,
     add_field('loans = LoanBriefSerializer(many=True)', 'loans')
     + [(".annotate(loan_count", ".prefetch_related('loans')\n            .annotate(loan_count")]),
    ('book_nested_reverse_prefetch_missing_child_fk', 'book', 'reverse-nested-child-fk', True,
     add_field('loans = LoanBriefSerializer(many=True)', 'loans')
     + [(".annotate(loan_count", ".prefetch_related('loans')\n            .annotate(loan_count"),
        ("fields = ['id', 'borrowed_on']", "fields = ['id', 'borrowed_on', 'member_name']"),
        ("class LoanBriefSerializer(serializers.ModelSerializer):\n",
         "class LoanBriefSerializer(serializers.ModelSerializer):\n"
         "    member_name = serializers.CharField(source='member.name')\n\n")]),
    ('book_nested_reverse_prefetch_with_child_select', 'book', 'reverse-nested-child-fk-safe', False,
     add_field('loans = LoanBriefSerializer(many=True)', 'loans')
     + [(".annotate(loan_count",
         ".prefetch_related(Prefetch('loans', queryset=Loan.objects.select_related('member')))\n"
         "            .annotate(loan_count"),
        ("fields = ['id', 'borrowed_on']", "fields = ['id', 'borrowed_on', 'member_name']"),
        ("class LoanBriefSerializer(serializers.ModelSerializer):\n",
         "class LoanBriefSerializer(serializers.ModelSerializer):\n"
         "    member_name = serializers.CharField(source='member.name')\n\n")]),
    # --- author endpoint ---
    ('author_base', 'author', 'control', False, []),
    ('author_drop_books_prefetch', 'author', 'method-query', True,
     [("prefetch_related('series', 'books')", "prefetch_related('series')")]),
    ('author_drop_series_prefetch', 'author', 'reverse-nested', True,
     [("prefetch_related('series', 'books')", "prefetch_related('books')")]),
    ('author_count_with_prefetch', 'author', 'method-safe', False,
     [('return len(obj.books.all())', 'return obj.books.count()')]),
    ('author_order_by_after_prefetch', 'author', 'filter-after-prefetch', True,
     add_field('book_titles = serializers.SerializerMethodField()', 'book_titles',
               "    def get_book_titles(self, obj):\n        return [b.title for b in obj.books.order_by('title')]")),
    ('author_sorted_in_python', 'author', 'filter-after-prefetch-safe', False,
     add_field('book_titles = serializers.SerializerMethodField()', 'book_titles',
               '    def get_book_titles(self, obj):\n        return sorted(b.title for b in obj.books.all())')),
    ('author_nested_book_series_missing', 'author', 'reverse-nested-child-fk', True,
     add_field('book_series = serializers.SerializerMethodField()', 'book_series',
               '    def get_book_series(self, obj):\n        return [b.series.name for b in obj.books.all()]')),
    ('author_nested_book_series_prefetched', 'author', 'reverse-nested-child-fk-safe', False,
     add_field('book_series = serializers.SerializerMethodField()', 'book_series',
               '    def get_book_series(self, obj):\n        return [b.series.name for b in obj.books.all()]')
     + [("prefetch_related('series', 'books')", "prefetch_related('series', 'books__series')")]),
]


def add_method_field(name: str, body: str) -> list[tuple[str, str]]:
    return add_field(f'{name} = serializers.SerializerMethodField()', name,
                     f'    def get_{name}(self, obj):\n        {body}')


LOAN_ONLY = (".select_related('book__author', 'branch', 'member')\n"
             "            .only({fields})\n"
             "            .prefetch_related('labels')")
ONLY_FIELDS = ("'id', 'borrowed_on', 'book__title', 'book__author__name', 'branch__name', "
               "'member__name', 'member__country'")

# Holdout split: contenders (the skill, Jev prompts) must not be tuned against these.
HOLDOUT_CASES: list[tuple[str, str, str, bool, list[tuple[str, str]]]] = [
    ('ho_method_fk_chain_unloaded', 'loan', 'method-fk-chain', True,
     add_method_field('series', 'return obj.book.series.name')),
    ('ho_method_fk_chain_loaded', 'loan', 'method-fk-chain-safe', False,
     add_method_field('country', 'return obj.book.author.country')),
    ('ho_to_representation_unloaded', 'loan', 'to-representation', True,
     [('    # EXTRA_METHODS',
       '\n    def to_representation(self, instance):\n'
       '        data = super().to_representation(instance)\n'
       "        data['series'] = instance.book.series.name\n"
       '        return data')]),
    ('ho_to_representation_loaded', 'loan', 'to-representation-safe', False,
     [('    # EXTRA_METHODS',
       '\n    def to_representation(self, instance):\n'
       '        data = super().to_representation(instance)\n'
       "        data['country'] = instance.book.author.country\n"
       '        return data')]),
    ('ho_string_related_unloaded', 'loan', 'related-field', True,
     add_field("series = serializers.StringRelatedField(source='book.series')", 'series')),
    ('ho_pk_related_many_unprefetched', 'loan', 'm2m', True,
     [("labels = serializers.SlugRelatedField(slug_field='name', many=True, read_only=True)",
       'labels = serializers.PrimaryKeyRelatedField(many=True, read_only=True)'),
      (".prefetch_related('labels')\n", '')]),
    ('ho_only_defers_rendered_field', 'loan', 'deferred-field', True,
     [(".select_related('book__author', 'branch', 'member')\n            .prefetch_related('labels')",
       LOAN_ONLY.format(fields=ONLY_FIELDS))]),
    ('ho_only_keeps_rendered_fields', 'loan', 'deferred-field-safe', False,
     [(".select_related('book__author', 'branch', 'member')\n            .prefetch_related('labels')",
       LOAN_ONLY.format(fields=ONLY_FIELDS + ", 'due_on'"))]),
    ('ho_filtered_prefetch_no_to_attr', 'loan', 'filtered-prefetch-safe', False,
     [(".prefetch_related('labels')", ".prefetch_related(Prefetch('labels', queryset=Label.objects.filter(name__startswith='r')))"),
      ('from library.models import Fine, Loan, Member', 'from library.models import Fine, Label, Loan, Member')]),
    ('ho_reverse_count_through_fk', 'loan', 'method-reverse-through-fk', True,
     add_method_field('book_loan_count', 'return obj.book.loans.count()')),
    ('ho_context_precomputed', 'loan', 'context-bulk-safe', False,
     add_method_field('fined', "return self.context['fines_by_loan'].get(obj.pk)")
     + [('from django.db.models import Count, Prefetch', 'from django.db.models import Count, Prefetch, Sum'),
        ('    serializer_class = LoanSerializer\n',
         '    serializer_class = LoanSerializer\n\n'
         '    def get_serializer_context(self):\n'
         '        context = super().get_serializer_context()\n'
         "        context['fines_by_loan'] = dict(\n"
         "            Fine.objects.filter(is_waived=False).values_list('loan_id').annotate(total=Sum('amount'))\n"
         '        )\n'
         '        return context\n')]),
    ('ho_first_on_prefetched', 'loan', 'method-query', True,
     add_method_field('first_label', 'label = obj.labels.first()\n        return label.name if label else None')),
    ('ho_slice_prefetched', 'loan', 'method-safe', False,
     add_method_field('first_label', 'return [label.name for label in obj.labels.all()[:1]]')),
    ('ho_fk_id_through_loaded_fk', 'book', 'fk-id-safe', False,
     add_field("publisher_id = serializers.IntegerField(source='author.publisher_id')", 'publisher_id')),
    ('ho_two_level_reverse_unprefetched', 'author', 'reverse-two-level', True,
     add_method_field('loan_total', 'return sum(len(b.loans.all()) for b in obj.books.all())')),
    ('ho_two_level_reverse_prefetched', 'author', 'reverse-two-level-safe', False,
     add_method_field('loan_total', 'return sum(len(b.loans.all()) for b in obj.books.all())')
     + [("prefetch_related('series', 'books')", "prefetch_related('series', 'books__loans')")]),
]


def build_source(base: str, edits: list[tuple[str, str]]) -> str:
    source = BASES[base]
    for old, new in edits:
        if source.count(old) != 1:
            raise ValueError(f'edit anchor must occur exactly once: {old!r}')
        source = source.replace(old, new)
    lines = [line for line in source.splitlines() if not line.strip().startswith('# EXTRA_')]
    return '\n'.join(line.replace('  # EXTRA_NAMES', '') for line in lines) + '\n'


def write_cases() -> None:
    CASES_DIR.mkdir(exist_ok=True)
    for stale in CASES_DIR.glob('*.py'):
        stale.unlink()
    (CASES_DIR / '__init__.py').write_text('')
    manifest = []
    for split, cases in (('dev', CASES), ('holdout', HOLDOUT_CASES)):
        for case_id, base, category, intended, edits in cases:
            (CASES_DIR / f'{case_id}.py').write_text(build_source(base, edits))
            manifest.append({'id': case_id, 'split': split, 'base': base, 'category': category, 'intended': intended})
    (CASES_DIR / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(f'wrote {len(manifest)} cases to {CASES_DIR}')


if __name__ == '__main__':
    write_cases()
