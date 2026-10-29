from rest_framework import generics, serializers

from library.models import Author, Book, Series


class SeriesSerializer(serializers.ModelSerializer):
    class Meta:
        model = Series
        fields = ['id', 'name']


class AuthorSerializer(serializers.ModelSerializer):
    series = SeriesSerializer(many=True)
    book_count = serializers.SerializerMethodField()
    loan_total = serializers.SerializerMethodField()

    class Meta:
        model = Author
        fields = ['id', 'name', 'series', 'book_count', 'loan_total']

    def get_book_count(self, obj):
        return len(obj.books.all())

    def get_loan_total(self, obj):
        return sum(len(b.loans.all()) for b in obj.books.all())


class View(generics.ListAPIView):
    serializer_class = AuthorSerializer

    def get_queryset(self):
        return Author.objects.prefetch_related('series', 'books__loans').order_by('id')
