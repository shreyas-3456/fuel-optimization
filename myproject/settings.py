"""
Django 6.0.6 settings for myproject.
"""

from pathlib import Path
import os
import environ
env = environ.Env()
environ.Env.read_env()
BASE_DIR = Path(__file__).resolve().parent.parent



def load_dotenv(path):
    if not path.exists():
        return
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv(BASE_DIR / '.env')

SECRET_KEY = 'django-insecure-change-me-in-production'

DEBUG = True

ALLOWED_HOSTS = ['*']

INSTALLED_APPS = [
    'core',
    'django.contrib.gis',
    'django.contrib.postgres'
]

MIDDLEWARE = [
    'core.profiling.ProfilingMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

DATABASES = {
    'default': {
        'ENGINE': 'django.contrib.gis.db.backends.postgis',
        'NAME': env('DB_NAME'),
        'USER': env('DB_USER'),
        'PASSWORD': env('DB_PASSWORD'),
        'HOST': env('DB_HOST', default='localhost'),
        'PORT': env('DB_PORT', default='5432'),
    }
}

ROOT_URLCONF = 'myproject.urls'

WSGI_APPLICATION = 'myproject.wsgi.application'

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

GRAPHHOPPER_API_KEY = os.environ.get('GRAPHHOPPER_API_KEY', '')
NOMINATIM_USER_AGENT = os.environ.get(
    'NOMINATIM_USER_AGENT',
    'spotter-ai-fuel-route-api/1.0'
)
FUEL_PRICE_CSV = BASE_DIR / 'fuel-prices-for-be-assessment (1).csv'

# Logging
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'plain': {
            'format': '%(asctime)s %(levelname)s %(name)s %(message)s',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'plain',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'WARNING',
    },
    'loggers': {
        'core.profiler': {
            'handlers': ['console'],
            'level': os.environ.get('PROFILER_LOG_LEVEL', 'INFO'),
            'propagate': False,
        },
        'core.routing': {
            'handlers': ['console'],
            'level': os.environ.get('ROUTING_LOG_LEVEL', 'INFO'),
            'propagate': False,
        },
    },
}
