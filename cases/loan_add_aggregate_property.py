from django.db.models import Count, Prefetch
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
    total_fines = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)

    class Meta:
        model = Loan
        fields = ['id', 'borrowed_on', 'due_on', 'book_title', 'author_name', 'branch_name', 'member', 'labels', 'total_fines']


class View(generics.ListAPIView):
    serializer_class = LoanSerializer

    def get_queryset(self):
        return (
            Loan.objects
            .select_related('book__author', 'branch', 'member')
            .prefetch_related('labels')
            .order_by('id')
        )
