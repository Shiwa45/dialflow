from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    initial = True
    dependencies = [
        ('campaigns', '0001_initial'),
        ('calls', '0001_initial'),
        ('leads', '0002_attempt_note'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='AgentStatus',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('status', models.CharField(
                    choices=[('offline','Offline'),('ready','Ready'),('on_call','On Call'),
                             ('wrapup','Wrap-up'),('break','Break'),('training','Training')],
                    default='offline', max_length=20,
                )),
                ('status_changed_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('active_channel_id', models.CharField(blank=True, max_length=100)),
                ('active_lead_id', models.IntegerField(blank=True, null=True)),
                ('active_call_log_id', models.IntegerField(blank=True, null=True)),
                ('call_started_at', models.DateTimeField(blank=True, null=True)),
                ('wrapup_started_at', models.DateTimeField(blank=True, null=True)),
                ('wrapup_call_log_id', models.IntegerField(blank=True, null=True)),
                ('last_heartbeat', models.DateTimeField(blank=True, null=True)),
                ('calls_today', models.PositiveIntegerField(default=0)),
                ('talk_time_today', models.PositiveIntegerField(default=0)),
                ('break_time_today', models.PositiveIntegerField(default=0)),
                ('active_campaign', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='active_agents', to='campaigns.campaign')),
                ('user', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='agent_status', to=settings.AUTH_USER_MODEL)),
            ],
            options={'verbose_name': 'Agent Status', 'verbose_name_plural': 'Agent Statuses'},
        ),
        migrations.CreateModel(
            name='CallDisposition',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('notes', models.TextField(blank=True)),
                ('callback_at', models.DateTimeField(blank=True, null=True)),
                ('auto_applied', models.BooleanField(default=False)),
                ('agent', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='dispositions_made', to=settings.AUTH_USER_MODEL)),
                ('call_log', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='disposition_record', to='calls.calllog')),
                ('campaign', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to='campaigns.campaign')),
                ('disposition', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='campaigns.disposition')),
                ('lead', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='dispositions', to='leads.lead')),
            ],
            options={'verbose_name': 'Call Disposition', 'ordering': ['-created_at']},
        ),
        migrations.AddIndex(model_name='agentstatus', index=models.Index(fields=['status'], name='agent_status_idx')),
        migrations.AddIndex(model_name='agentstatus', index=models.Index(fields=['active_campaign', 'status'], name='agent_campaign_status_idx')),
    ]
