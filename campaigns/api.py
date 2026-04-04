# campaigns/api.py
from rest_framework import serializers, viewsets
from rest_framework.routers import DefaultRouter
from .models import Campaign, Disposition


class DispositionSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Disposition
        fields = ('id', 'name', 'category', 'outcome', 'color', 'hotkey', 'sort_order')


class CampaignSerializer(serializers.ModelSerializer):
    dispositions = DispositionSerializer(many=True, read_only=True)

    class Meta:
        model  = Campaign
        fields = ('id', 'name', 'status', 'dial_mode', 'dispositions',
                  'stat_calls_today', 'stat_answered_today',
                  'stat_abandon_rate', 'stat_agents_active')


class CampaignViewSet(viewsets.ReadOnlyModelViewSet):
    queryset         = Campaign.objects.all()
    serializer_class = CampaignSerializer


router = DefaultRouter()
router.register(r'campaigns', CampaignViewSet)
