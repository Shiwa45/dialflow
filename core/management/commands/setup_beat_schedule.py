# core/management/commands/setup_beat_schedule.py
"""
Setup Celery Beat Schedule
==========================
Seeds the periodic task schedule into django_celery_beat database tables.
Run this once after migrations if using DatabaseScheduler in production.

Usage:
    python manage.py setup_beat_schedule
    python manage.py setup_beat_schedule --reset
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Seed Celery beat schedule into django_celery_beat database'

    def add_arguments(self, parser):
        parser.add_argument('--reset', action='store_true',
                            help='Delete existing schedule before seeding')

    def handle(self, *args, **options):
        from django_celery_beat.models import PeriodicTask, IntervalSchedule, CrontabSchedule
        import json

        self.stdout.write('Setting up Celery beat schedule…')

        if options['reset']:
            PeriodicTask.objects.filter(task__startswith='campaigns.').delete()
            PeriodicTask.objects.filter(task__startswith='agents.').delete()
            PeriodicTask.objects.filter(task__startswith='reports.').delete()
            self.stdout.write('  Deleted existing schedule.')

        # ── Interval helpers ──────────────────────────────────────────────────
        def interval(every, period):
            schedule, _ = IntervalSchedule.objects.get_or_create(
                every=every,
                period=period,
            )
            return schedule

        def crontab(minute='*', hour='*', day_of_week='*',
                    day_of_month='*', month_of_year='*'):
            schedule, _ = CrontabSchedule.objects.get_or_create(
                minute=minute, hour=hour,
                day_of_week=day_of_week,
                day_of_month=day_of_month,
                month_of_year=month_of_year,
            )
            return schedule

        SECOND = IntervalSchedule.SECONDS
        MINUTE = IntervalSchedule.MINUTES

        TASKS = [
            # name, task, schedule, interval/crontab
            {
                'name':     'Predictive dial tick (every 1s)',
                'task':     'campaigns.tasks.predictive_dial_tick',
                'interval': interval(1, SECOND),
                'enabled':  True,
            },
            {
                'name':     'Fill all hoppers (every 30s)',
                'task':     'campaigns.tasks.fill_all_hoppers',
                'interval': interval(30, SECOND),
                'enabled':  True,
            },
            {
                'name':     'Reset stale hopper entries (every 2m)',
                'task':     'campaigns.tasks.reset_stale_hopper_entries',
                'interval': interval(2, MINUTE),
                'enabled':  True,
            },
            {
                'name':     'Update campaign stats (every 30s)',
                'task':     'campaigns.tasks.update_campaign_stats',
                'interval': interval(30, SECOND),
                'enabled':  True,
            },
            {
                'name':     'Recycle failed calls (every 5m)',
                'task':     'campaigns.tasks.recycle_failed_calls',
                'interval': interval(5, MINUTE),
                'enabled':  True,
            },
            {
                'name':     'Check wrapup timeouts (every 5s)',
                'task':     'agents.tasks.check_wrapup_timeouts',
                'interval': interval(5, SECOND),
                'enabled':  True,
            },
            {
                'name':     'Cleanup zombie agents (every 60s)',
                'task':     'agents.tasks.cleanup_zombie_agents',
                'interval': interval(60, SECOND),
                'enabled':  True,
            },
            {
                'name':     'Reset daily agent stats (midnight)',
                'task':     'agents.tasks.reset_daily_stats',
                'crontab':  crontab(minute='0', hour='0'),
                'enabled':  True,
            },
            {
                'name':     'Generate daily snapshot (00:05)',
                'task':     'reports.tasks.generate_daily_snapshot',
                'crontab':  crontab(minute='5', hour='0'),
                'enabled':  True,
            },
        ]

        created = updated = 0
        for task_def in TASKS:
            name     = task_def['name']
            task     = task_def['task']
            enabled  = task_def.get('enabled', True)
            defaults = {
                'task':    task,
                'enabled': enabled,
                'kwargs':  json.dumps({}),
                'args':    json.dumps([]),
            }

            if 'interval' in task_def:
                defaults['interval'] = task_def['interval']
                defaults['crontab']  = None
            else:
                defaults['crontab']  = task_def['crontab']
                defaults['interval'] = None

            obj, was_created = PeriodicTask.objects.update_or_create(
                name=name, defaults=defaults
            )
            if was_created:
                created += 1
            else:
                updated += 1

        self.stdout.write(self.style.SUCCESS(
            f'  ✓ Created {created}, updated {updated} periodic tasks.'
        ))
        self.stdout.write('')
        self.stdout.write('  Start beat with:')
        self.stdout.write('    celery -A dialflow beat -l info '
                          '--scheduler django_celery_beat.schedulers:DatabaseScheduler')
