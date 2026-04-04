# conftest.py
import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'dialflow.settings.test')


def pytest_configure(config):
    os.environ['DJANGO_SETTINGS_MODULE'] = 'dialflow.settings.test'
    django.setup()
