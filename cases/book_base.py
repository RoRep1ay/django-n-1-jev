from django.db.models import Count, Prefetch, Q
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

    class Meta:
        model = Book
        fields = ['id', 'title', 'author', 'series_name', 'loan_count']


class View(generics.ListAPIView):
    serializer_class = BookSerializer

    def get_queryset(self):
        return (
            Book.objects
            .select_related('author__publisher', 'series')
            .annotate(loan_count=Count('loans'))
            .order_by('id')
        )
