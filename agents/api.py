# agents/api.py
from rest_framework import serializers, viewsets
from rest_framework.routers import DefaultRouter


class AgentStatusSerializer(serializers.Serializer):
    user_id      = serializers.IntegerField()
    username     = serializers.CharField(source='user.username')
    full_name    = serializers.SerializerMethodField()
    status       = serializers.CharField()
    status_since = serializers.DateTimeField(source='status_changed_at')

    def get_full_name(self, obj):
        return obj.user.get_full_name() or obj.user.username


class AgentStatusViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = AgentStatusSerializer

    def get_queryset(self):
        from agents.models import AgentStatus
        return AgentStatus.objects.select_related('user').filter(
            status__in=['ready', 'on_call', 'wrapup', 'break']
        )


router = DefaultRouter()
router.register(r'agent-status', AgentStatusViewSet, basename='agent-status')
