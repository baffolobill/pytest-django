"""A py.test plugin which helps testing Django applications

This plugin handles creating and destroying the test environment and
test database and provides some useful text fixtues.
"""

import os
import sys

import pytest

from .django_compat import is_django_unittest
from .fixtures import (django_db_setup, django_db, django_db_transactional,
                       django_test_db_config, django_db_reuse, client,
                       admin_client, rf, settings, live_server,
                       _live_server_helper)

from .lazy_django import skip_if_no_django, django_settings_is_configured


(django_db_setup, django_db, django_db_transactional, django_test_db_config, django_db_reuse,
 client, admin_client, rf, settings, live_server, _live_server_helper)


SETTINGS_MODULE_ENV = 'DJANGO_SETTINGS_MODULE'
CONFIGURATION_ENV = 'DJANGO_CONFIGURATION'


################ pytest hooks ################


def pytest_addoption(parser):
    group = parser.getgroup('django')
    group._addoption('--reuse-db',
                     action='store_true', dest='reuse_db', default=False,
                     help='Re-use the testing database if it already exists, '
                          'and do not remove it when the test finishes. This '
                          'option will be ignored when --no-db is given.')
    group._addoption('--create-db',
                     action='store_true', dest='create_db', default=False,
                     help='Re-create the database, even if it exists. This '
                          'option will be ignored if not --reuse-db is given.')
    group._addoption('--ds',
                     action='store', type='string', dest='ds', default=None,
                     help='Set DJANGO_SETTINGS_MODULE.')
    group._addoption('--dc',
                     action='store', type='string', dest='dc', default=None,
                     help='Set DJANGO_CONFIGURATION.')
    parser.addini(CONFIGURATION_ENV,
                  'django-configurations class to use by pytest-django.')
    group._addoption('--liveserver', default=None,
                     help='Address and port for the live_server fixture.')
    parser.addini(SETTINGS_MODULE_ENV,
                  'Django settings module to use by pytest-django.')

def _load_settings(config, options):
    # Configure DJANGO_SETTINGS_MODULE
    ds = (options.ds or
          config.getini(SETTINGS_MODULE_ENV) or
          os.environ.get(SETTINGS_MODULE_ENV))

    # Configure DJANGO_CONFIGURATION
    dc = (options.dc or
          config.getini(CONFIGURATION_ENV) or
          os.environ.get(CONFIGURATION_ENV))

    if ds:
        os.environ[SETTINGS_MODULE_ENV] = ds

        if dc:
            os.environ[CONFIGURATION_ENV] = dc

            # Install the django-configurations importer
            import configurations.importer
            configurations.importer.install()

        from django.conf import settings
        try:
            settings.DATABASES
        except ImportError:
            e = sys.exc_info()[1]
            raise pytest.UsageError(*e.args)


if pytest.__version__[:3] >= "2.4":
    def pytest_load_initial_conftests(early_config, parser, args):
        _load_settings(early_config, parser.parse_known_args(args))


def pytest_configure(config):
    # Register the marks
    config.addinivalue_line(
        'markers',
        'django_db(transaction=False): Mark the test as using '
        'the django test database.  The *transaction* argument marks will '
        "allow you to use real transactions in the test like Django's "
        'TransactionTestCase.')
    config.addinivalue_line(
        'markers',
        'urls(modstr): Use a different URLconf for this test, similar to '
        'the `urls` attribute of Django `TestCase` objects.  *modstr* is '
        'a string specifying the module of a URL config, e.g. '
        '"my_app.test_urls".')

    if pytest.__version__[:3] < "2.4":
        _load_settings(config, config.option)


################ Autouse fixtures ################


@pytest.fixture(autouse=True, scope='session')
def django_test_environment(request):
    """
    Sets up the Django test environment, by calling Django's setup_test_environment
    """
    if django_settings_is_configured():
        from django.test.utils import setup_test_environment, teardown_test_environment
        from django.conf import settings

        setup_test_environment()
        settings.DEBUG = False

        request.addfinalizer(teardown_test_environment)


@pytest.fixture(autouse=True, scope='session')
def django_cursor_wrapper(request):
    """The django cursor wrapper, internal to pytest-django

    This will globally disable all database access. The object
    returned has a .enable() and a .disable() method which can be used
    to temporarily enable database access.
    """
    if django_settings_is_configured():

        import django.db.backends.util

        manager = CursorManager(django.db.backends.util)
        manager.disable()
        request.addfinalizer(manager.restore)
    else:
        manager = CursorManager()
    return manager


@pytest.fixture(autouse=True)
def _django_db_marker(request):
    """Implement the django_db marker, internal to pytest-django

    This will dynamically request the ``db`` or ``django_db_transactional``
    fixtures as required by the django_db marker.
    """
    marker = request.node.get_marker('django_db')

    if marker:
        validate_django_db(marker)
        if marker.transaction:
            request.getfuncargvalue('django_db_transactional')
        else:
            request.getfuncargvalue('django_db')


@pytest.fixture(autouse=True)
def _django_setup_unittest(request, django_cursor_wrapper):
    """Setup a django unittest, internal to pytest-django"""
    if django_settings_is_configured() and is_django_unittest(request.node):
        request.getfuncargvalue('django_test_environment')
        request.getfuncargvalue('django_db_setup')
        django_cursor_wrapper.enable()
        request.addfinalizer(django_cursor_wrapper.restore)


@pytest.fixture(autouse=True, scope='function')
def _django_clear_outbox(request):
    """Clear the django outbox, internal to pytest-django"""
    if django_settings_is_configured():
        from django.core import mail
        mail.outbox = []


@pytest.fixture(autouse=True, scope='function')
def _django_set_urlconf(request):
    """Apply the @pytest.mark.urls marker, internal to pytest-django"""
    marker = request.node.get_marker('urls')
    if marker:
        skip_if_no_django()
        import django.conf
        from django.core.urlresolvers import clear_url_caches

        validate_urls(marker)
        original_urlconf = django.conf.settings.ROOT_URLCONF
        django.conf.settings.ROOT_URLCONF = marker.urls
        clear_url_caches()

        def restore():
            django.conf.settings.ROOT_URLCONF = original_urlconf

        request.addfinalizer(restore)


################ Helper Functions ################


class CursorManager(object):
    """Manager for django.db.backends.util.CursorWrapper

    This is the object returned by django_cursor_wrapper.

    If created with None as django.db.backends.util the object is a
    no-op.
    """

    def __init__(self, dbutil=None):
        self.stack = []

        self._dbutil = dbutil
        if dbutil:
            self._orig_wrapper = dbutil.CursorWrapper

    def _blocking_wrapper(*args, **kwargs):
        __tracebackhide__ = True
        __tracebackhide__  # Silence pyflakes
        pytest.fail('Database access not allowed, '
                    'use the "django_db" mark to enable')


    def enable(self):
        """Enable access to the django database"""
        if self._dbutil:
            self.stack.append(self._dbutil.CursorWrapper)
            self._dbutil.CursorWrapper = self._orig_wrapper

    def disable(self):
        if self._dbutil:
            self.stack.append(self._dbutil.CursorWrapper)
            self._dbutil.CursorWrapper = self._blocking_wrapper

    def restore(self):
        assert self.stack, 'no state to pop!'
        self._dbutil.CursorWrapper = self.stack.pop()

    def __enter__(self):
        self.enable()

    def __exit__(self, exc_type, exc_value, traceback):
        self.restore()


def validate_django_db(marker):
    """This function validates the django_db marker

    It checks the signature and creates the `transaction` attribute on
    the marker which will have the correct value.
    """
    def apifun(transaction=False):
        marker.transaction = transaction
    apifun(*marker.args, **marker.kwargs)


def validate_urls(marker):
    """This function validates the urls marker

    It checks the signature and creates the `urls` attribute on the
    marker which will have the correct value.
    """
    def apifun(urls):
        marker.urls = urls
    apifun(*marker.args, **marker.kwargs)
