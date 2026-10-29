---
name: n+1-audit
description: Use before merging any change that touches a Django view + serializer pair, or when reviewing a PR that adds/modifies a list endpoint. Cross-checks every relation accessed in the response against the queryset's select_related/prefetch_related. Pre-merge audit, not a fix-up for already-slow code.
---

# n+1-audit

A pre-merge audit: walk every relation the response touches, verify it's eager-loaded. Use when you're about to ship, not when something is already broken.

## When to Use

- About to merge a PR that adds a list/detail endpoint
- About to merge a PR that adds fields to an existing serializer
- Reviewing a PR with new `SerializerMethodField` or new `source='fk.field'`
- A view's queryset just changed and serializers downstream should be re-checked

## The Audit Procedure

### Step 1 — Identify the surface

For the changed view, list:

1. **The view's queryset** — including all `select_related` / `prefetch_related` calls
2. **The serializer used** — top-level + nested
3. **Any `SerializerMethodField`** — what queryset/property each one calls
4. **Template / response transformations** — if rendering happens outside the serializer

### Step 2 — Walk every field

For each field in the serializer output, classify:

| Field type | What to check |
|---|---|
| `ModelField` direct (`name`, `created_at`) | ✅ no relation, skip |
| `source='fk.field'` | ❓ Is `fk` in `select_related`? |
| `source='fk.fk2.field'` | ❓ Is `fk__fk2` in `select_related`? |
| Nested serializer on FK | ❓ Same — needs `select_related('fk')` |
| Nested serializer on reverse/M2M | ❓ Needs `prefetch_related('related_set')` |
| `SerializerMethodField` calling `obj.related.filter(...)` | ❓ Needs `Prefetch('related', queryset=...)` |
| `SerializerMethodField` calling a service/util | ❓ Trace the util — may itself do queries |

**Mark each field**: ✅ covered, ❌ missing eager-load, ⚠️ needs verification.

### Step 3 — Verify with query count test

Add or update a test that asserts an upper bound on query count:

```python
@pytest.mark.django_db
def test_list_view_query_count(client, django_assert_num_queries):
    # Seed enough rows that N+1 would show up
    LoanFactory.create_batch(10)

    with django_assert_num_queries(8):  # tune to actual count after fix
        response = client.get('/api/loans/')

    assert response.status_code == 200
```

**Rule**: query count must NOT scale with the number of rows in the response. If 10 rows = 18 queries and 100 rows = 108 queries, you have N+1. The audit isn't done until the test proves linearity is broken.

## Common Patterns to Check

### Pattern A — Nested serializer on FK

```python
# Serializer
class LoanSerializer(ModelSerializer):
    book = BookSerializer(read_only=True)  # nested

# View queryset MUST have:
queryset = Loan.objects.select_related('book')
```

If `BookSerializer` itself has nested fields, chain: `select_related('book__author')`.

### Pattern B — Reverse relation in response

```python
class AuthorSerializer(ModelSerializer):
    books = BookSerializer(many=True, read_only=True)  # reverse FK

# View queryset MUST have:
queryset = Author.objects.prefetch_related('books')
```

### Pattern C — Filtered nested set

```python
class BookSerializer(ModelSerializer):
    open_loans = SerializerMethodField()

    def get_open_loans(self, obj):
        return LoanSerializer(obj.loans.filter(status=Loan.Status.BORROWED), many=True).data
```

This is N+1 even with `prefetch_related('loans')` because `.filter(status=...)` re-queries. Fix:

```python
queryset = Book.objects.prefetch_related(
    Prefetch('loans', queryset=Loan.objects.filter(status=Loan.Status.BORROWED), to_attr='open_loans_cache')
)
# Then in serializer use obj.open_loans_cache
```

### Pattern D — Property that crosses relations

```python
class Member(Model):
    @property
    def total_fines(self):
        return Fine.objects.filter(loan__member=self).aggregate(Sum('amount'))['amount__sum']
```

If the serializer exposes `total_fines`, every row triggers a query. Either:
- Annotate at the queryset level: `.annotate(total_fines=Sum('loans__fines__amount'))`
- Or move the calculation to a precomputed/cached field

## The Three Lines That Catch 80% of N+1

```bash
# 1. Find SerializerMethodField bodies that do queries
rg "def get_" <app>/serializers.py | rg '\.(filter|all|first|get)\('

# 2. Find serializer fields with FK source
rg "source='" <app>/serializers.py | rg '\.'

# 3. Find views without select_related/prefetch
rg "queryset = " <app>/views.py -A 3 | rg -v 'select_related|prefetch_related'
```

Prefer `rg` over recursive `grep` — it's faster and respects `.gitignore`.

Run these on the changed app. Anything that pops up needs Step 2 audit.

## Common Mistakes

- **Auditing only the view, not the serializer** — N+1 lives at the intersection. Both files must be open.
- **Assuming `prefetch_related` covers filtered access** — `.filter()` after prefetch re-queries. Use `Prefetch(queryset=...)`.
- **No query-count test** — without an assertion, regressions slip back in next month.
- **Audit passes locally with 1 row** — N+1 is invisible at row count = 1. Test with 10+ rows.

## Quick Reference

| Found this | Do this |
|---|---|
| `obj.fk.field` in serializer | `select_related('fk')` in view |
| `obj.fk.fk2.field` | `select_related('fk__fk2')` |
| `obj.related_set.all()` | `prefetch_related('related_set')` |
| `obj.related_set.filter(...)` | `Prefetch('related_set', queryset=Model.objects.filter(...))` |
| `SerializerMethodField` doing aggregation | `.annotate(...)` on queryset |
| Property crossing relations | Annotate or denormalize |
