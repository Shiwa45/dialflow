from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True
    dependencies = [
        ('campaigns', '0001_initial'),
        ('agents', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='DailySnapshot',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('date', models.DateField()),
                ('calls_total', models.PositiveIntegerField(default=0)),
                ('calls_answered', models.PositiveIntegerField(default=0)),
                ('calls_dropped', models.PositiveIntegerField(default=0)),
                ('calls_no_answer', models.PositiveIntegerField(default=0)),
                ('avg_talk_time', models.PositiveIntegerField(default=0)),
                ('total_talk_time', models.PositiveIntegerField(default=0)),
                ('agents_logged_in', models.PositiveIntegerField(default=0)),
                ('abandon_rate', models.DecimalField(decimal_places=2, default=0, max_digits=5)),
                ('campaign', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='snapshots', to='campaigns.campaign')),
            ],
            options={'verbose_name': 'Daily Snapshot', 'ordering': ['-date'], 'unique_together': {('date', 'campaign')}},
        ),
    ]
