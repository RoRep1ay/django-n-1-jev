from django.db.models import Count, Prefetch, Sum
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
    fined = serializers.SerializerMethodField()

    class Meta:
        model = Loan
        fields = ['id', 'borrowed_on', 'due_on', 'book_title', 'author_name', 'branch_name', 'member', 'labels', 'fined']

    def get_fined(self, obj):
        return self.context['fines_by_loan'].get(obj.pk)


class View(generics.ListAPIView):
    serializer_class = LoanSerializer

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['fines_by_loan'] = dict(
            Fine.objects.filter(is_waived=False).values_list('loan_id').annotate(total=Sum('amount'))
        )
        return context

    def get_queryset(self):
        return (
            Loan.objects
            .select_related('book__author', 'branch', 'member')
            .prefetch_related('labels')
            .order_by('id')
        )
