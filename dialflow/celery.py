# dialflow/celery.py
import os
from celery import Celery
from celery.schedules import crontab

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'dialflow.settings')

app = Celery('dialflow')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()

app.conf.beat_schedule = {

    # ── Predictive dialer (every second — critical path) ─────────────────────
    'predictive-dial': {
        'task': 'campaigns.tasks.predictive_dial_tick',
        'schedule': 1.0,
    },

    # ── Hopper fill (keep leads in queue) ────────────────────────────────────
    'hopper-fill': {
        'task': 'campaigns.tasks.fill_all_hoppers',
        'schedule': 30.0,
    },

    # ── Auto-wrapup enforcement (server-side) ─────────────────────────────────
    'check-wrapup-timeouts': {
        'task': 'agents.tasks.check_wrapup_timeouts',
        'schedule': 5.0,
    },

    # ── Agent zombie cleanup (mark inactive agents offline) ───────────────────
    'zombie-cleanup': {
        'task': 'agents.tasks.cleanup_zombie_agents',
        'schedule': 60.0,
    },

    # ── Campaign stats refresh ────────────────────────────────────────────────
    'campaign-stats-update': {
        'task': 'campaigns.tasks.update_campaign_stats',
        'schedule': 30.0,
    },

    # ── Stale hopper entry reset ──────────────────────────────────────────────
    'reset-stale-hopper': {
        'task': 'campaigns.tasks.reset_stale_hopper_entries',
        'schedule': 120.0,
    },

    # ── Daily: recycle failed calls ───────────────────────────────────────────
    'recycle-failed-calls': {
        'task': 'campaigns.tasks.recycle_failed_calls',
        'schedule': crontab(minute='*/5'),  # every 5 min during the day
    },

    # ── Daily: reset agent stats at midnight ─────────────────────────────────
    'reset-daily-agent-stats': {
        'task': 'agents.tasks.reset_daily_stats',
        'schedule': crontab(hour=0, minute=0),
    },

    # ── Daily report snapshot at midnight ────────────────────────────────────
    'daily-report-snapshot': {
        'task': 'reports.tasks.generate_daily_snapshot',
        'schedule': crontab(hour=0, minute=5),
    },
}

app.conf.timezone = os.environ.get('TIME_ZONE', 'Asia/Kolkata')
