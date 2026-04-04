from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('leads', '0001_initial'),
        ('campaigns', '0001_initial'),
        ('calls', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='lead',
            name='campaigns',
            field=models.ManyToManyField(blank=True, related_name='leads', to='campaigns.campaign'),
        ),
        migrations.CreateModel(
            name='LeadAttempt',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('attempt_number', models.PositiveSmallIntegerField(default=1)),
                ('phone_number', models.CharField(max_length=30)),
                ('result', models.CharField(blank=True, max_length=50)),
                ('attempted_at', models.DateTimeField(auto_now_add=True)),
                ('campaign', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='lead_attempts', to='campaigns.campaign')),
                ('lead', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='attempts', to='leads.lead')),
                ('call_log', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='calls.calllog')),
            ],
            options={'verbose_name': 'Lead Attempt', 'ordering': ['-attempted_at']},
        ),
        migrations.CreateModel(
            name='LeadNote',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('note', models.TextField()),
                ('agent', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
                ('campaign', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='campaigns.campaign')),
                ('lead', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='notes', to='leads.lead')),
            ],
            options={'verbose_name': 'Lead Note', 'ordering': ['-created_at']},
        ),
        migrations.AddIndex(model_name='leadattempt', index=models.Index(fields=['campaign', 'lead', '-attempt_number'], name='attempt_campaign_idx')),
    ]
