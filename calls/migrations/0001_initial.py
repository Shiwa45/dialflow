
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('campaigns', '0001_initial'),
        ('leads', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='CallLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('channel_id', models.CharField(blank=True, db_index=True, max_length=100)),
                ('bridge_id', models.CharField(blank=True, max_length=100)),
                ('direction', models.CharField(
                    choices=[('outbound','Outbound'),('inbound','Inbound')],
                    default='outbound', max_length=10,
                )),
                ('phone_number', models.CharField(db_index=True, max_length=30)),
                ('status', models.CharField(default='initiated', max_length=20)),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('answered_at', models.DateTimeField(blank=True, null=True)),
                ('ended_at', models.DateTimeField(blank=True, null=True)),
                ('duration', models.PositiveIntegerField(default=0)),
                ('ring_duration', models.PositiveIntegerField(default=0)),
                ('agent_notes', models.TextField(blank=True)),
                ('recording_path', models.CharField(blank=True, max_length=500)),
                ('amd_result', models.CharField(blank=True, max_length=50)),
                ('agent', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='call_logs', to=settings.AUTH_USER_MODEL,
                )),
                ('campaign', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='call_logs', to='campaigns.campaign',
                )),
                ('disposition', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='call_logs', to='campaigns.disposition',
                )),
                ('lead', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='call_logs', to='leads.lead',
                )),
            ],
            options={'verbose_name': 'Call Log', 'ordering': ['-started_at']},
        ),
        migrations.AddIndex(
            model_name='calllog',
            index=models.Index(fields=['campaign', '-started_at'], name='call_campaign_idx'),
        ),
        migrations.AddIndex(
            model_name='calllog',
            index=models.Index(fields=['agent', '-started_at'], name='call_agent_idx'),
        ),
        migrations.AddIndex(
            model_name='calllog',
            index=models.Index(fields=['channel_id'], name='call_channel_idx'),
        ),
    ]
