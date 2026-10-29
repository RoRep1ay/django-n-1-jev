from decimal import Decimal

from library.models import Fine, Loan


def get_fine_amount(loan: Loan) -> Decimal:
    """Iterates the fines manager, so it reads a prefetched ``fines`` cache when present."""
    return sum((f.amount for f in loan.fines.all() if not f.is_waived), Decimal('0'))


def get_live_fine_amount(loan: Loan) -> Decimal:
    return sum((f.amount for f in Fine.objects.filter(loan=loan, is_waived=False)), Decimal('0'))


def get_display_label(loan: Loan) -> str:
    return f'#{loan.pk} ({loan.loan_days} days)'
