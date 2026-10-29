# N+1 Bench

Can a reviewer spot an **N+1 query problem** in a Django REST Framework list endpoint just by reading the code?

This repo has 58 small endpoints. Some have an N+1 problem and some look like they do but don't. Two static
reviewers are scored against them:

- **Jev**: the `jev-latest` model, called through the [TypeSafe SDK](https://github.com/typesafe-ai/typesafe-sdk-python)
- **Claude**: Claude Code following the bundled [`n+1-audit` skill](.agents/skills/n+1-audit/SKILL.md)

Neither reviewer can run the code. Ground truth is **measured**: each endpoint is served with 5 and then 10 units of
seed data, and if the SQL query count grows, the endpoint has an N+1.

> A demo showcase: the domain is made up, and the code is written to be benchmarked, not reused.

**Domain:** a small library app. Publishers have authors, authors write books (grouped into series), members borrow
books as loans from a branch, and loans can carry labels and fines.

## Results

Each reviewer judged all 58 cases 3 times.

| | Jev | Claude Opus | Claude Sonnet |
|---|---|---|---|
| correct attempts | 159 / 174 | 174 / 174 | 174 / 174 |
| precision | 0.96 | 1.00 | 1.00 |
| recall | 0.90 | 1.00 | 1.00 |
| F1 | 0.93 | 1.00 | 1.00 |
| F1 on the holdout | 0.93 | 1.00 | 1.00 |
| median time per call | 0.3 s | 14.6 s | 11.8 s |
| cost of all 174 calls | $0.03 | $14.95 | $7.56 |

- **Claude** got every case right on every attempt, with both models.
- **Jev** is about 40× faster and 250–500× cheaper. It misses some FK chains that are only partly loaded, and
  it gives a false alarm when an FK is loaded with `prefetch_related` instead of `select_related`.
- **Scoring:** precision, recall and F1 count a case as flagged when the mean of its 3 attempts is 0.5 or more.
- **Jev variant:** Jev is shown with its best setup. It is asked one question per serializer field, and its prompt
  includes the loading plan that the harness reads from Django's query object (`jev_resolved_choice`). The five
  other Jev setups score between 0.67 and 0.90 F1.
- **Caveat:** the skill's examples use the same library models as the test cases, which probably makes the cases
  easier for Claude than they'd be in an unfamiliar codebase.

The full reports, including every Jev variant, are produced by `python -m harness.run_benchmark` (see
[Run it](#run-it)).

## The tests

Each case takes one of three correct endpoints and makes a single small edit:

- **Queries:** the measured query count at 5 → 10 units of seed data.
- **Jev and Claude columns:** ✅ means right on all 3 attempts, ❌ means wrong on all 3, and a fraction like 2/3
  counts the correct attempts. For Claude, ✅ means both Opus and Sonnet were right on every attempt.

### Loan endpoint

The base endpoint returns each loan with its book title, author, branch, member and labels.

| case | change | queries | N+1 | Jev | Claude |
|---|---|---|---|---|---|
| `loan_base` | the correct endpoint | 2 → 2 | | ✅ | ✅ |
| `loan_drop_member_select` | drop `member` from `select_related` | 12 → 22 | ✔ | ✅ | ✅ |
| `loan_shorten_author_select` | `select_related('book__author')` becomes `('book')` | 12 → 22 | ✔ | 2/3 | ✅ |
| `loan_drop_branch_select` | drop `branch` from `select_related` | 12 → 22 | ✔ | ✅ | ✅ |
| `loan_drop_labels_prefetch` | drop `prefetch_related('labels')` | 11 → 21 | ✔ | ✅ | ✅ |
| `loan_add_series_source` | add a field with `source='book.series.name'` | 12 → 22 | ✔ | ✅ | ✅ |
| `loan_add_book_id` | add `book_id` (the FK column itself) | 2 → 2 | | ✅ | ✅ |
| `loan_add_pure_property` | add `loan_days`, a date-arithmetic property | 2 → 2 | | ✅ | ✅ |
| `loan_add_aggregate_property` | add `total_fines`, a property that aggregates fines | 12 → 22 | ✔ | ✅ | ✅ |
| `loan_aggregate_property_with_prefetch` | same, with `fines` prefetched (the aggregate still queries) | 13 → 23 | ✔ | ✅ | ✅ |
| `loan_service_all_with_prefetch` | a service reads `fines.all()`, with fines prefetched | 3 → 3 | | ✅ | ✅ |
| `loan_service_all_without_prefetch` | the same service without the prefetch | 12 → 22 | ✔ | ✅ | ✅ |
| `loan_service_bypasses_prefetch` | a service runs its own `Fine.objects.filter`, ignoring the prefetch | 13 → 23 | ✔ | ✅ | ✅ |
| `loan_service_pure` | a service that never touches the database | 2 → 2 | | ✅ | ✅ |
| `loan_count_with_prefetch` | `obj.fines.count()`, with fines prefetched | 3 → 3 | | ✅ | ✅ |
| `loan_count_without_prefetch` | `obj.fines.count()`, without the prefetch | 12 → 22 | ✔ | ✅ | ✅ |
| `loan_exists_with_prefetch` | `obj.labels.exists()` on prefetched labels | 2 → 2 | | ✅ | ✅ |
| `loan_filter_after_prefetch` | `obj.fines.filter(...)` after prefetching fines | 13 → 23 | ✔ | ✅ | ✅ |
| `loan_filtered_prefetch_to_attr` | a filtered `Prefetch(to_attr=...)`, read through that attribute | 3 → 3 | | ✅ | ✅ |
| `loan_to_attr_mismatch` | the same `Prefetch`, but the field reads `fines.all()` | 13 → 23 | ✔ | ✅ | ✅ |
| `loan_count_annotated` | the fine count comes from `annotate(Count(...))` | 2 → 2 | | ✅ | ✅ |
| `loan_extra_select` | an extra, unused `select_related` | 2 → 2 | | ✅ | ✅ |
| `loan_prefetch_fk_instead_of_select` | an FK chain loaded with `prefetch_related` instead | 4 → 4 | | ❌ | ✅ |
| `loan_get_queryset_keeps_class_attr` | `get_queryset` builds on `super()` | 2 → 2 | | ✅ | ✅ |
| `loan_get_queryset_ignores_class_attr` | `get_queryset` ignores the eager-loaded class `queryset` | 51 → 101 | ✔ | ✅ | ✅ |

### Book endpoint

The base endpoint returns each book with its nested author, the publisher name, the series name and an annotated
loan count.

| case | change | queries | N+1 | Jev | Claude |
|---|---|---|---|---|---|
| `book_base` | the correct endpoint | 1 → 1 | | ✅ | ✅ |
| `book_shorten_publisher_select` | `select_related('author__publisher')` becomes `('author')` | 11 → 21 | ✔ | ❌ | ✅ |
| `book_drop_series_select` | drop `series` from `select_related` | 11 → 21 | ✔ | ✅ | ✅ |
| `book_property_instead_of_annotation` | a field reads the `open_loan_count` property | 11 → 21 | ✔ | ✅ | ✅ |
| `book_filtered_annotation` | the same count as a filtered `Count` annotation | 1 → 1 | | ✅ | ✅ |
| `book_nested_reverse_without_prefetch` | nest the book's `loans` without a prefetch | 11 → 21 | ✔ | ✅ | ✅ |
| `book_nested_reverse_with_prefetch` | nest `loans` with `prefetch_related('loans')` | 2 → 2 | | ✅ | ✅ |
| `book_nested_reverse_prefetch_missing_child_fk` | loans are prefetched, but each loan reads `member.name` | 12 → 22 | ✔ | 2/3 | ✅ |
| `book_nested_reverse_prefetch_with_child_select` | the prefetch adds `select_related('member')` | 2 → 2 | | ✅ | ✅ |

### Author endpoint

The base endpoint returns each author with nested series and a book count.

| case | change | queries | N+1 | Jev | Claude |
|---|---|---|---|---|---|
| `author_base` | the correct endpoint | 3 → 3 | | ✅ | ✅ |
| `author_drop_books_prefetch` | drop the `books` prefetch that the count relies on | 7 → 12 | ✔ | ✅ | ✅ |
| `author_drop_series_prefetch` | drop the `series` prefetch that the nested serializer relies on | 7 → 12 | ✔ | ✅ | ✅ |
| `author_count_with_prefetch` | `books.count()` on prefetched books | 3 → 3 | | ✅ | ✅ |
| `author_order_by_after_prefetch` | `books.order_by('title')` after prefetching books | 8 → 13 | ✔ | ✅ | ✅ |
| `author_sorted_in_python` | `sorted(...)` over `books.all()` | 3 → 3 | | ✅ | ✅ |
| `author_nested_book_series_missing` | read `book.series.name` for each prefetched book | 13 → 23 | ✔ | ❌ | ✅ |
| `author_nested_book_series_prefetched` | the same, with `books__series` prefetched | 4 → 4 | | ✅ | ✅ |

### Holdout

These 16 cases use the same endpoints, but reviewers must not be tuned against them.

| case | endpoint | change | queries | N+1 | Jev | Claude |
|---|---|---|---|---|---|---|
| `ho_method_fk_chain_unloaded` | loan | a method field reads `book.series.name` (not loaded) | 12 → 22 | ✔ | ✅ | ✅ |
| `ho_method_fk_chain_loaded` | loan | a method field reads `book.author.country` (loaded) | 2 → 2 | | ✅ | ✅ |
| `ho_to_representation_unloaded` | loan | `to_representation` adds `book.series.name` | 12 → 22 | ✔ | ✅ | ✅ |
| `ho_to_representation_loaded` | loan | `to_representation` adds `book.author.country` | 2 → 2 | | ✅ | ✅ |
| `ho_string_related_unloaded` | loan | `StringRelatedField(source='book.series')` | 12 → 22 | ✔ | ✅ | ✅ |
| `ho_pk_related_many_unprefetched` | loan | `PrimaryKeyRelatedField(many=True)` on labels, with no prefetch | 11 → 21 | ✔ | ✅ | ✅ |
| `ho_only_defers_rendered_field` | loan | `.only()` leaves out `due_on`, which is rendered | 12 → 22 | ✔ | 1/3 | ✅ |
| `ho_only_keeps_rendered_fields` | loan | `.only()` keeps every rendered field | 2 → 2 | | 1/3 | ✅ |
| `ho_filtered_prefetch_no_to_attr` | loan | a filtered `Prefetch` on labels without `to_attr` | 2 → 2 | | ✅ | ✅ |
| `ho_reverse_count_through_fk` | loan | `obj.book.loans.count()` | 12 → 22 | ✔ | ✅ | ✅ |
| `ho_context_precomputed` | loan | fines are totalled once in the serializer context | 3 → 3 | | ✅ | ✅ |
| `ho_first_on_prefetched` | loan | `labels.first()` on prefetched labels | 12 → 22 | ✔ | ✅ | ✅ |
| `ho_slice_prefetched` | loan | `labels.all()[:1]` on prefetched labels | 2 → 2 | | ✅ | ✅ |
| `ho_fk_id_through_loaded_fk` | book | `author.publisher_id` | 1 → 1 | | ✅ | ✅ |
| `ho_two_level_reverse_unprefetched` | author | count loans across books, with only `books` prefetched | 13 → 23 | ✔ | ✅ | ✅ |
| `ho_two_level_reverse_prefetched` | author | the same, with `books__loans` prefetched | 4 → 4 | | ✅ | ✅ |

## Run it

You need Python 3.11.

```sh
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python -m harness.oracle                                # measure every case; no API key needed
cp .env.example .env                                    # set TYPESAFE_API_KEY
python -m harness.run_benchmark --skip-skill --repeat 3 # Jev only; a few cents
python -m harness.run_benchmark --repeat 3              # Jev and Claude (needs the `claude` CLI); about $23
```

The cases are generated from `harness/build_cases.py` (`python -m harness.build_cases`), and the domain lives in
`library/`. `harness/ci_check.py` runs the Jev check on a git diff instead of the benchmark cases.

## License

[MIT](LICENSE)
