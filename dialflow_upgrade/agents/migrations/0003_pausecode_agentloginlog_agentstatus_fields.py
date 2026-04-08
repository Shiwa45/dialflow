# agents/migrations/0003_pausecode_agentloginlog_agentstatus_fields.py
# Generated manually for DialFlow Pro upgrade

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('agents', '0002_alter_agentstatus_id_alter_calldisposition_id'),
    ]

    operations = [
        migrations.CreateModel(
            name='PauseCode',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('name', models.CharField(max_length=100, unique=True)),
                ('code', models.CharField(max_length=20, unique=True)),
                ('is_active', models.BooleanField(default=True)),
                ('sort_order', models.PositiveSmallIntegerField(default=0)),
            ],
            options={
                'verbose_name': 'Pause Code',
                'ordering': ['sort_order', 'name'],
            },
        ),
        migrations.CreateModel(
            name='AgentLoginLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('login_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('logout_at', models.DateTimeField(blank=True, null=True)),
                ('ip_address', models.GenericIPAddressField(blank=True, null=True)),
                ('user_agent', models.TextField(blank=True)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='login_logs', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'Agent Login Log',
                'ordering': ['-login_at'],
                'indexes': [
                    models.Index(fields=['user', '-login_at']),
                    models.Index(fields=['user', 'login_at']),
                ],
            },
        ),
        migrations.AddField(
            model_name='agentstatus',
            name='pause_code',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='agents.pausecode'),
        ),
        migrations.AddField(
            model_name='agentstatus',
            name='login_log',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='agent_statuses', to='agents.agentloginlog'),
        ),
    ]
