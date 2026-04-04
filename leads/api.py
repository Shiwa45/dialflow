# leads/api.py
from rest_framework import serializers, viewsets, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from .models import Lead
from django.db.models import Q


class LeadSerializer(serializers.ModelSerializer):
    full_name = serializers.SerializerMethodField()

    class Meta:
        model  = Lead
        fields = ('id', 'first_name', 'last_name', 'full_name',
                  'primary_phone', 'email', 'company', 'city', 'state',
                  'priority', 'is_active', 'do_not_call', 'created_at')

    def get_full_name(self, obj):
        return obj.full_name


class LeadViewSet(viewsets.ModelViewSet):
    queryset         = Lead.objects.filter(is_active=True)
    serializer_class = LeadSerializer
    filter_backends  = [filters.SearchFilter, filters.OrderingFilter]
    search_fields    = ['first_name', 'last_name', 'primary_phone', 'email']
    ordering_fields  = ['created_at', 'priority', 'last_name']

    @action(detail=True, methods=['post'], url_path='mark-dnc')
    def mark_dnc(self, request, pk=None):
        lead = self.get_object()
        lead.mark_dnc(added_by=request.user)
        return Response({'success': True})


from rest_framework.routers import DefaultRouter
router = DefaultRouter()
router.register(r'leads', LeadViewSet)
