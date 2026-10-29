from decimal import Decimal

from django.db import models
from django.db.models import Sum


class Publisher(models.Model):
    name = models.CharField(max_length=100)


class Author(models.Model):
    publisher = models.ForeignKey(Publisher, on_delete=models.PROTECT, related_name='authors')
    name = models.CharField(max_length=100)
    country = models.CharField(max_length=2, default='JP')


class Series(models.Model):
    author = models.ForeignKey(Author, on_delete=models.PROTECT, related_name='series')
    name = models.CharField(max_length=100)


class Book(models.Model):
    author = models.ForeignKey(Author, on_delete=models.PROTECT, related_name='books')
    series = models.ForeignKey(Series, on_delete=models.PROTECT, related_name='books')
    title = models.CharField(max_length=100)

    @property
    def open_loan_count(self) -> int:
        return self.loans.exclude(status=Loan.Status.RETURNED).count()


class Branch(models.Model):
    name = models.CharField(max_length=50)


class Member(models.Model):
    name = models.CharField(max_length=100)
    email = models.EmailField()
    country = models.CharField(max_length=2)


class Label(models.Model):
    name = models.CharField(max_length=50)


class Loan(models.Model):
    class Status(models.IntegerChoices):
        BORROWED = 1
        RETURNED = 2
        LOST = 3

    book = models.ForeignKey(Book, on_delete=models.PROTECT, related_name='loans')
    member = models.ForeignKey(Member, on_delete=models.PROTECT, related_name='loans')
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='loans')
    labels = models.ManyToManyField(Label, related_name='loans', blank=True)
    borrowed_on = models.DateField()
    due_on = models.DateField()
    status = models.PositiveSmallIntegerField(choices=Status.choices, default=Status.BORROWED)

    @property
    def loan_days(self) -> int:
        return (self.due_on - self.borrowed_on).days

    @property
    def total_fines(self) -> Decimal:
        return self.fines.filter(is_waived=False).aggregate(total=Sum('amount'))['total'] or Decimal('0')


class Fine(models.Model):
    loan = models.ForeignKey(Loan, on_delete=models.PROTECT, related_name='fines')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    is_waived = models.BooleanField(default=False)
