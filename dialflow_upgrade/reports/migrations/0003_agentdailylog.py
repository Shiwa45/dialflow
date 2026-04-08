# reports/migrations/0003_agentdailylog.py
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('campaigns', '0001_initial'),
        ('reports', '0002_alter_dailysnapshot_avg_talk_time_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='AgentDailyLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('date', models.DateField()),
                ('login_time', models.PositiveIntegerField(default=0, help_text='Total login seconds')),
                ('talk_time', models.PositiveIntegerField(default=0, help_text='Total talk seconds')),
                ('break_time', models.PositiveIntegerField(default=0, help_text='Total break seconds')),
                ('wrapup_time', models.PositiveIntegerField(default=0, help_text='Total wrapup seconds')),
                ('calls_dialed', models.PositiveIntegerField(default=0)),
                ('calls_answered', models.PositiveIntegerField(default=0)),
                ('calls_transferred', models.PositiveIntegerField(default=0)),
                ('dispositions_sale', models.PositiveIntegerField(default=0)),
                ('dispositions_dnc', models.PositiveIntegerField(default=0)),
                ('dispositions_callback', models.PositiveIntegerField(default=0)),
                ('dispositions_other', models.PositiveIntegerField(default=0)),
                ('agent', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='daily_logs', to=settings.AUTH_USER_MODEL)),
                ('campaign', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='campaigns.campaign')),
            ],
            options={
                'unique_together': {('date', 'agent', 'campaign')},
                'ordering': ['-date'],
                'verbose_name': 'Agent Daily Log',
            },
        ),
    ]
