# -*- coding: utf-8 -*-
# Unit and doctests for specific database backends.
from __future__ import unicode_literals

import copy
import datetime
from decimal import Decimal
import re
import threading
import unittest
import warnings

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.management.color import no_style
from django.db import (connection, connections, DEFAULT_DB_ALIAS,
    DatabaseError, IntegrityError, reset_queries, transaction)
from django.db.backends import BaseDatabaseWrapper
from django.db.backends.signals import connection_created
from django.db.backends.postgresql_psycopg2 import version as pg_version
from django.db.backends.utils import format_number, CursorWrapper
from django.db.models import Sum, Avg, Variance, StdDev
from django.db.models.fields import (AutoField, DateField, DateTimeField,
    DecimalField, IntegerField, TimeField)
from django.db.models.sql.constants import CURSOR
from django.db.utils import ConnectionHandler
from django.test import (TestCase, TransactionTestCase, override_settings,
    skipUnlessDBFeature, skipIfDBFeature)
from django.test.utils import str_prefix, IgnoreAllDeprecationWarningsMixin
from django.utils import six
from django.utils.six.moves import xrange

from . import models


class DummyBackendTest(TestCase):

    def test_no_databases(self):
        """
        Test that empty DATABASES setting default to the dummy backend.
        """
        DATABASES = {}
        conns = ConnectionHandler(DATABASES)
        self.assertEqual(conns[DEFAULT_DB_ALIAS].settings_dict['ENGINE'],
            'django.db.backends.dummy')


@unittest.skipUnless(connection.vendor == 'oracle', "Test only for Oracle")
class OracleTests(unittest.TestCase):

    def test_quote_name(self):
        # Check that '%' chars are escaped for query execution.
        name = '"SOME%NAME"'
        quoted_name = connection.ops.quote_name(name)
        self.assertEqual(quoted_name % (), name)

    def test_dbms_session(self):
        # If the backend is Oracle, test that we can call a standard
        # stored procedure through our cursor wrapper.
        from django.db.backends.oracle.base import convert_unicode

        with connection.cursor() as cursor:
            cursor.callproc(convert_unicode('DBMS_SESSION.SET_IDENTIFIER'),
                            [convert_unicode('_django_testing!')])

    def test_cursor_var(self):
        # If the backend is Oracle, test that we can pass cursor variables
        # as query parameters.
        from django.db.backends.oracle.base import Database

        with connection.cursor() as cursor:
            var = cursor.var(Database.STRING)
            cursor.execute("BEGIN %s := 'X'; END; ", [var])
            self.assertEqual(var.getvalue(), 'X')

    def test_long_string(self):
        # If the backend is Oracle, test that we can save a text longer
        # than 4000 chars and read it properly
        with connection.cursor() as cursor:
            cursor.execute('CREATE TABLE ltext ("TEXT" NCLOB)')
            long_str = ''.join(six.text_type(x) for x in xrange(4000))
            cursor.execute('INSERT INTO ltext VALUES (%s)', [long_str])
            cursor.execute('SELECT text FROM ltext')
            row = cursor.fetchone()
            self.assertEqual(long_str, row[0].read())
            cursor.execute('DROP TABLE ltext')

    def test_client_encoding(self):
        # If the backend is Oracle, test that the client encoding is set
        # correctly.  This was broken under Cygwin prior to r14781.
        connection.ensure_connection()
        self.assertEqual(connection.connection.encoding, "UTF-8")
        self.assertEqual(connection.connection.nencoding, "UTF-8")

    def test_order_of_nls_parameters(self):
        # an 'almost right' datetime should work with configured
        # NLS parameters as per #18465.
        with connection.cursor() as cursor:
            query = "select 1 from dual where '1936-12-29 00:00' < sysdate"
            # Test that the query succeeds without errors - pre #18465 this
            # wasn't the case.
            cursor.execute(query)
            self.assertEqual(cursor.fetchone()[0], 1)


@unittest.skipUnless(connection.vendor == 'sqlite', "Test only for SQLite")
class SQLiteTests(TestCase):

    longMessage = True

    def test_autoincrement(self):
        """
        Check that auto_increment fields are created with the AUTOINCREMENT
        keyword in order to be monotonically increasing. Refs #10164.
        """
        statements = connection.creation.sql_create_model(models.Square,
            style=no_style())
        match = re.search('"id" ([^,]+),', statements[0][0])
        self.assertIsNotNone(match)
        self.assertEqual('integer NOT NULL PRIMARY KEY AUTOINCREMENT',
            match.group(1), "Wrong SQL used to create an auto-increment "
            "column on SQLite")

    def test_aggregation(self):
        """
        #19360: Raise NotImplementedError when aggregating on date/time fields.
        """
        for aggregate in (Sum, Avg, Variance, StdDev):
            self.assertRaises(NotImplementedError,
                models.Item.objects.all().aggregate, aggregate('time'))
            self.assertRaises(NotImplementedError,
                models.Item.objects.all().aggregate, aggregate('date'))
            self.assertRaises(NotImplementedError,
                models.Item.objects.all().aggregate, aggregate('last_modified'))

    def test_convert_values_to_handle_null_value(self):
        from django.db.backends.sqlite3.base import DatabaseOperations
        convert_values = DatabaseOperations(connection).convert_values
        self.assertIsNone(convert_values(None, AutoField(primary_key=True)))
        self.assertIsNone(convert_values(None, DateField()))
        self.assertIsNone(convert_values(None, DateTimeField()))
        self.assertIsNone(convert_values(None, DecimalField()))
        self.assertIsNone(convert_values(None, IntegerField()))
        self.assertIsNone(convert_values(None, TimeField()))


@unittest.skipUnless(connection.vendor == 'postgresql', "Test only for PostgreSQL")
class PostgreSQLTests(TestCase):

    def assert_parses(self, version_string, version):
        self.assertEqual(pg_version._parse_version(version_string), version)

    def test_parsing(self):
        """Test PostgreSQL version parsing from `SELECT version()` output"""
        self.assert_parses("PostgreSQL 9.3 beta4", 90300)
        self.assert_parses("PostgreSQL 9.3", 90300)
        self.assert_parses("EnterpriseDB 9.3", 90300)
        self.assert_parses("PostgreSQL 9.3.6", 90306)
        self.assert_parses("PostgreSQL 9.4beta1", 90400)
        self.assert_parses("PostgreSQL 9.3.1 on i386-apple-darwin9.2.2, compiled by GCC i686-apple-darwin9-gcc-4.0.1 (GCC) 4.0.1 (Apple Inc. build 5478)", 90301)

    def test_version_detection(self):
        """Test PostgreSQL version detection"""

        # Helper mocks
        class CursorMock(object):
            "Very simple mock of DB-API cursor"
            def execute(self, arg):
                pass

            def fetchone(self):
                return ["PostgreSQL 9.3"]

            def __enter__(self):
                return self

            def __exit__(self, type, value, traceback):
                pass

        class OlderConnectionMock(object):
            "Mock of psycopg2 (< 2.0.12) connection"
            def cursor(self):
                return CursorMock()

        # psycopg2 < 2.0.12 code path
        conn = OlderConnectionMock()
        self.assertEqual(pg_version.get_version(conn), 90300)

    def test_connect_and_rollback(self):
        """
        PostgreSQL shouldn't roll back SET TIME ZONE, even if the first
        transaction is rolled back (#17062).
        """
        databases = copy.deepcopy(settings.DATABASES)
        new_connections = ConnectionHandler(databases)
        new_connection = new_connections[DEFAULT_DB_ALIAS]
        try:
            # Ensure the database default time zone is different than
            # the time zone in new_connection.settings_dict. We can
            # get the default time zone by reset & show.
            cursor = new_connection.cursor()
            cursor.execute("RESET TIMEZONE")
            cursor.execute("SHOW TIMEZONE")
            db_default_tz = cursor.fetchone()[0]
            new_tz = 'Europe/Paris' if db_default_tz == 'UTC' else 'UTC'
            new_connection.close()

            # Fetch a new connection with the new_tz as default
            # time zone, run a query and rollback.
            new_connection.settings_dict['TIME_ZONE'] = new_tz
            new_connection.set_autocommit(False)
            cursor = new_connection.cursor()
            new_connection.rollback()

            # Now let's see if the rollback rolled back the SET TIME ZONE.
            cursor.execute("SHOW TIMEZONE")
            tz = cursor.fetchone()[0]
            self.assertEqual(new_tz, tz)
        finally:
            new_connection.close()

    def test_connect_non_autocommit(self):
        """
        The connection wrapper shouldn't believe that autocommit is enabled
        after setting the time zone when AUTOCOMMIT is False (#21452).
        """
        databases = copy.deepcopy(settings.DATABASES)
        databases[DEFAULT_DB_ALIAS]['AUTOCOMMIT'] = False
        new_connections = ConnectionHandler(databases)
        new_connection = new_connections[DEFAULT_DB_ALIAS]
        try:
            # Open a database connection.
            new_connection.cursor()
            self.assertFalse(new_connection.get_autocommit())
        finally:
            new_connection.close()

    def _select(self, val):
        with connection.cursor() as cursor:
            cursor.execute("SELECT %s", (val,))
            return cursor.fetchone()[0]

    def test_select_ascii_array(self):
        a = ["awef"]
        b = self._select(a)
        self.assertEqual(a[0], b[0])

    def test_select_unicode_array(self):
        a = ["ᄲawef"]
        b = self._select(a)
        self.assertEqual(a[0], b[0])

    def test_lookup_cast(self):
        from django.db.backends.postgresql_psycopg2.operations import DatabaseOperations

        do = DatabaseOperations(connection=None)
        for lookup in ('iexact', 'contains', 'icontains', 'startswith',
                       'istartswith', 'endswith', 'iendswith', 'regex', 'iregex'):
            self.assertIn('::text', do.lookup_cast(lookup))


@unittest.skipUnless(connection.vendor == 'mysql', "Test only for MySQL")
class MySQLTests(TestCase):

    def test_autoincrement(self):
        """
        Check that auto_increment fields are reset correctly by sql_flush().
        Before MySQL version 5.0.13 TRUNCATE did not do auto_increment reset.
        Refs #16961.
        """
        statements = connection.ops.sql_flush(no_style(),
                                              tables=['test'],
                                              sequences=[{
                                                  'table': 'test',
                                                  'col': 'somecol',
                                              }])
        found_reset = False
        for sql in statements:
            found_reset = found_reset or 'ALTER TABLE' in sql
        if connection.mysql_version < (5, 0, 13):
            self.assertTrue(found_reset)
        else:
            self.assertFalse(found_reset)


class DateQuotingTest(TestCase):

    def test_django_date_trunc(self):
        """
        Test the custom ``django_date_trunc method``, in particular against
        fields which clash with strings passed to it (e.g. 'year') - see
        #12818__.

        __: http://code.djangoproject.com/ticket/12818

        """
        updated = datetime.datetime(2010, 2, 20)
        models.SchoolClass.objects.create(year=2009, last_updated=updated)
        years = models.SchoolClass.objects.dates('last_updated', 'year')
        self.assertEqual(list(years), [datetime.date(2010, 1, 1)])

    def test_django_date_extract(self):
        """
        Test the custom ``django_date_extract method``, in particular against fields
        which clash with strings passed to it (e.g. 'day') - see #12818__.

        __: http://code.djangoproject.com/ticket/12818

        """
        updated = datetime.datetime(2010, 2, 20)
        models.SchoolClass.objects.create(year=2009, last_updated=updated)
        classes = models.SchoolClass.objects.filter(last_updated__day=20)
        self.assertEqual(len(classes), 1)


@override_settings(DEBUG=True)
class LastExecutedQueryTest(TestCase):

    def test_last_executed_query(self):
        """
        last_executed_query should not raise an exception even if no previous
        query has been run.
        """
        cursor = connection.cursor()
        try:
            connection.ops.last_executed_query(cursor, '', ())
        except Exception:
            self.fail("'last_executed_query' should not raise an exception.")

    def test_debug_sql(self):
        list(models.Reporter.objects.filter(first_name="test"))
        sql = connection.queries[-1]['sql'].lower()
        self.assertIn("select", sql)
        self.assertIn(models.Reporter._meta.db_table, sql)

    def test_query_encoding(self):
        """
        Test that last_executed_query() returns an Unicode string
        """
        data = models.RawData.objects.filter(raw_data=b'\x00\x46  \xFE').extra(select={'föö': 1})
        sql, params = data.query.sql_with_params()
        cursor = data.query.get_compiler('default').execute_sql(CURSOR)
        last_sql = cursor.db.ops.last_executed_query(cursor, sql, params)
        self.assertIsInstance(last_sql, six.text_type)

    @unittest.skipUnless(connection.vendor == 'sqlite',
                         "This test is specific to SQLite.")
    def test_no_interpolation_on_sqlite(self):
        # Regression for #17158
        # This shouldn't raise an exception
        query = "SELECT strftime('%Y', 'now');"
        connection.cursor().execute(query)
        self.assertEqual(connection.queries[-1]['sql'],
            str_prefix("QUERY = %(_)s\"SELECT strftime('%%Y', 'now');\" - PARAMS = ()"))


class ParameterHandlingTest(TestCase):

    def test_bad_parameter_count(self):
        "An executemany call with too many/not enough parameters will raise an exception (Refs #12612)"
        cursor = connection.cursor()
        query = ('INSERT INTO %s (%s, %s) VALUES (%%s, %%s)' % (
            connection.introspection.table_name_converter('backends_square'),
            connection.ops.quote_name('root'),
            connection.ops.quote_name('square')
        ))
        self.assertRaises(Exception, cursor.executemany, query, [(1, 2, 3)])
        self.assertRaises(Exception, cursor.executemany, query, [(1,)])


# Unfortunately, the following tests would be a good test to run on all
# backends, but it breaks MySQL hard. Until #13711 is fixed, it can't be run
# everywhere (although it would be an effective test of #13711).
class LongNameTest(TestCase):
    """Long primary keys and model names can result in a sequence name
    that exceeds the database limits, which will result in truncation
    on certain databases (e.g., Postgres). The backend needs to use
    the correct sequence name in last_insert_id and other places, so
    check it is. Refs #8901.
    """

    def test_sequence_name_length_limits_create(self):
        """Test creation of model with long name and long pk name doesn't error. Ref #8901"""
        models.VeryLongModelNameZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ.objects.create()

    def test_sequence_name_length_limits_m2m(self):
        """Test an m2m save of a model with a long name and a long m2m field name doesn't error as on Django >=1.2 this now uses object saves. Ref #8901"""
        obj = models.VeryLongModelNameZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ.objects.create()
        rel_obj = models.Person.objects.create(first_name='Django', last_name='Reinhardt')
        obj.m2m_also_quite_long_zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz.add(rel_obj)

    def test_sequence_name_length_limits_flush(self):
        """Test that sequence resetting as part of a flush with model with long name and long pk name doesn't error. Ref #8901"""
        # A full flush is expensive to the full test, so we dig into the
        # internals to generate the likely offending SQL and run it manually

        # Some convenience aliases
        VLM = models.VeryLongModelNameZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ
        VLM_m2m = VLM.m2m_also_quite_long_zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz.through
        tables = [
            VLM._meta.db_table,
            VLM_m2m._meta.db_table,
        ]
        sequences = [
            {
                'column': VLM._meta.pk.column,
                'table': VLM._meta.db_table
            },
        ]
        cursor = connection.cursor()
        for statement in connection.ops.sql_flush(no_style(), tables, sequences):
            cursor.execute(statement)


class SequenceResetTest(TestCase):

    def test_generic_relation(self):
        "Sequence names are correct when resetting generic relations (Ref #13941)"
        # Create an object with a manually specified PK
        models.Post.objects.create(id=10, name='1st post', text='hello world')

        # Reset the sequences for the database
        cursor = connection.cursor()
        commands = connections[DEFAULT_DB_ALIAS].ops.sequence_reset_sql(no_style(), [models.Post])
        for sql in commands:
            cursor.execute(sql)

        # If we create a new object now, it should have a PK greater
        # than the PK we specified manually.
        obj = models.Post.objects.create(name='New post', text='goodbye world')
        self.assertTrue(obj.pk > 10)


# This test needs to run outside of a transaction, otherwise closing the
# connection would implicitly rollback and cause problems during teardown.
class ConnectionCreatedSignalTest(TransactionTestCase):

    available_apps = []

    # Unfortunately with sqlite3 the in-memory test database cannot be closed,
    # and so it cannot be re-opened during testing.
    @skipUnlessDBFeature('test_db_allows_multiple_connections')
    def test_signal(self):
        data = {}

        def receiver(sender, connection, **kwargs):
            data["connection"] = connection

        connection_created.connect(receiver)
        connection.close()
        connection.cursor()
        self.assertTrue(data["connection"].connection is connection.connection)

        connection_created.disconnect(receiver)
        data.clear()
        connection.cursor()
        self.assertTrue(data == {})


class EscapingChecks(TestCase):
    """
    All tests in this test case are also run with settings.DEBUG=True in
    EscapingChecksDebug test case, to also test CursorDebugWrapper.
    """

    bare_select_suffix = connection.features.bare_select_suffix

    def test_paramless_no_escaping(self):
        cursor = connection.cursor()
        cursor.execute("SELECT '%s'" + self.bare_select_suffix)
        self.assertEqual(cursor.fetchall()[0][0], '%s')

    def test_parameter_escaping(self):
        cursor = connection.cursor()
        cursor.execute("SELECT '%%', %s" + self.bare_select_suffix, ('%d',))
        self.assertEqual(cursor.fetchall()[0], ('%', '%d'))

    @unittest.skipUnless(connection.vendor == 'sqlite',
                         "This is an sqlite-specific issue")
    def test_sqlite_parameter_escaping(self):
        #13648: '%s' escaping support for sqlite3
        cursor = connection.cursor()
        cursor.execute("select strftime('%s', date('now'))")
        response = cursor.fetchall()[0][0]
        # response should be an non-zero integer
        self.assertTrue(int(response))


@override_settings(DEBUG=True)
class EscapingChecksDebug(EscapingChecks):
    pass


class BackendTestCase(TestCase):

    def create_squares_with_executemany(self, args):
        self.create_squares(args, 'format', True)

    def create_squares(self, args, paramstyle, multiple):
        cursor = connection.cursor()
        opts = models.Square._meta
        tbl = connection.introspection.table_name_converter(opts.db_table)
        f1 = connection.ops.quote_name(opts.get_field('root').column)
        f2 = connection.ops.quote_name(opts.get_field('square').column)
        if paramstyle == 'format':
            query = 'INSERT INTO %s (%s, %s) VALUES (%%s, %%s)' % (tbl, f1, f2)
        elif paramstyle == 'pyformat':
            query = 'INSERT INTO %s (%s, %s) VALUES (%%(root)s, %%(square)s)' % (tbl, f1, f2)
        else:
            raise ValueError("unsupported paramstyle in test")
        if multiple:
            cursor.executemany(query, args)
        else:
            cursor.execute(query, args)

    def test_cursor_executemany(self):
        #4896: Test cursor.executemany
        args = [(i, i ** 2) for i in range(-5, 6)]
        self.create_squares_with_executemany(args)
        self.assertEqual(models.Square.objects.count(), 11)
        for i in range(-5, 6):
            square = models.Square.objects.get(root=i)
            self.assertEqual(square.square, i ** 2)

    def test_cursor_executemany_with_empty_params_list(self):
        #4765: executemany with params=[] does nothing
        args = []
        self.create_squares_with_executemany(args)
        self.assertEqual(models.Square.objects.count(), 0)

    def test_cursor_executemany_with_iterator(self):
        #10320: executemany accepts iterators
        args = iter((i, i ** 2) for i in range(-3, 2))
        self.create_squares_with_executemany(args)
        self.assertEqual(models.Square.objects.count(), 5)

        args = iter((i, i ** 2) for i in range(3, 7))
        with override_settings(DEBUG=True):
            # same test for DebugCursorWrapper
            self.create_squares_with_executemany(args)
        self.assertEqual(models.Square.objects.count(), 9)

    @skipUnlessDBFeature('supports_paramstyle_pyformat')
    def test_cursor_execute_with_pyformat(self):
        #10070: Support pyformat style passing of parameters
        args = {'root': 3, 'square': 9}
        self.create_squares(args, 'pyformat', multiple=False)
        self.assertEqual(models.Square.objects.count(), 1)

    @skipUnlessDBFeature('supports_paramstyle_pyformat')
    def test_cursor_executemany_with_pyformat(self):
        #10070: Support pyformat style passing of parameters
        args = [{'root': i, 'square': i ** 2} for i in range(-5, 6)]
        self.create_squares(args, 'pyformat', multiple=True)
        self.assertEqual(models.Square.objects.count(), 11)
        for i in range(-5, 6):
            square = models.Square.objects.get(root=i)
            self.assertEqual(square.square, i ** 2)

    @skipUnlessDBFeature('supports_paramstyle_pyformat')
    def test_cursor_executemany_with_pyformat_iterator(self):
        args = iter({'root': i, 'square': i ** 2} for i in range(-3, 2))
        self.create_squares(args, 'pyformat', multiple=True)
        self.assertEqual(models.Square.objects.count(), 5)

        args = iter({'root': i, 'square': i ** 2} for i in range(3, 7))
        with override_settings(DEBUG=True):
            # same test for DebugCursorWrapper
            self.create_squares(args, 'pyformat', multiple=True)
        self.assertEqual(models.Square.objects.count(), 9)

    def test_unicode_fetches(self):
        #6254: fetchone, fetchmany, fetchall return strings as unicode objects
        qn = connection.ops.quote_name
        models.Person(first_name="John", last_name="Doe").save()
        models.Person(first_name="Jane", last_name="Doe").save()
        models.Person(first_name="Mary", last_name="Agnelline").save()
        models.Person(first_name="Peter", last_name="Parker").save()
        models.Person(first_name="Clark", last_name="Kent").save()
        opts2 = models.Person._meta
        f3, f4 = opts2.get_field('first_name'), opts2.get_field('last_name')
        query2 = ('SELECT %s, %s FROM %s ORDER BY %s'
          % (qn(f3.column), qn(f4.column), connection.introspection.table_name_converter(opts2.db_table),
             qn(f3.column)))
        cursor = connection.cursor()
        cursor.execute(query2)
        self.assertEqual(cursor.fetchone(), ('Clark', 'Kent'))
        self.assertEqual(list(cursor.fetchmany(2)), [('Jane', 'Doe'), ('John', 'Doe')])
        self.assertEqual(list(cursor.fetchall()), [('Mary', 'Agnelline'), ('Peter', 'Parker')])

    def test_unicode_password(self):
        old_password = connection.settings_dict['PASSWORD']
        connection.settings_dict['PASSWORD'] = "françois"
        try:
            connection.cursor()
        except DatabaseError:
            # As password is probably wrong, a database exception is expected
            pass
        except Exception as e:
            self.fail("Unexpected error raised with unicode password: %s" % e)
        finally:
            connection.settings_dict['PASSWORD'] = old_password

    def test_database_operations_helper_class(self):
        # Ticket #13630
        self.assertTrue(hasattr(connection, 'ops'))
        self.assertTrue(hasattr(connection.ops, 'connection'))
        self.assertEqual(connection, connection.ops.connection)

    def test_cached_db_features(self):
        self.assertIn(connection.features.supports_transactions, (True, False))
        self.assertIn(connection.features.supports_stddev, (True, False))
        self.assertIn(connection.features.can_introspect_foreign_keys, (True, False))

    def test_duplicate_table_error(self):
        """ Test that creating an existing table returns a DatabaseError """
        cursor = connection.cursor()
        query = 'CREATE TABLE %s (id INTEGER);' % models.Article._meta.db_table
        with self.assertRaises(DatabaseError):
            cursor.execute(query)

    def test_cursor_contextmanager(self):
        """
        Test that cursors can be used as a context manager
        """
        with connection.cursor() as cursor:
            self.assertIsInstance(cursor, CursorWrapper)
        # Both InterfaceError and ProgrammingError seem to be used when
        # accessing closed cursor (psycopg2 has InterfaceError, rest seem
        # to use ProgrammingError).
        with self.assertRaises(connection.features.closed_cursor_error_class):
            # cursor should be closed, so no queries should be possible.
            cursor.execute("SELECT 1" + connection.features.bare_select_suffix)

    @unittest.skipUnless(connection.vendor == 'postgresql',
                         "Psycopg2 specific cursor.closed attribute needed")
    def test_cursor_contextmanager_closing(self):
        # There isn't a generic way to test that cursors are closed, but
        # psycopg2 offers us a way to check that by closed attribute.
        # So, run only on psycopg2 for that reason.
        with connection.cursor() as cursor:
            self.assertIsInstance(cursor, CursorWrapper)
        self.assertTrue(cursor.closed)

    # Unfortunately with sqlite3 the in-memory test database cannot be closed.
    @skipUnlessDBFeature('test_db_allows_multiple_connections')
    def test_is_usable_after_database_disconnects(self):
        """
        Test that is_usable() doesn't crash when the database disconnects.

        Regression for #21553.
        """
        # Open a connection to the database.
        with connection.cursor():
            pass
        # Emulate a connection close by the database.
        connection._close()
        # Even then is_usable() should not raise an exception.
        try:
            self.assertFalse(connection.is_usable())
        finally:
            # Clean up the mess created by connection._close(). Since the
            # connection is already closed, this crashes on some backends.
            try:
                connection.close()
            except Exception:
                pass

    @override_settings(DEBUG=True)
    def test_queries(self):
        """
        Test the documented API of connection.queries.
        """
        reset_queries()

        with connection.cursor() as cursor:
            cursor.execute("SELECT 1" + connection.features.bare_select_suffix)
        self.assertEqual(1, len(connection.queries))

        self.assertIsInstance(connection.queries, list)
        self.assertIsInstance(connection.queries[0], dict)
        six.assertCountEqual(self, connection.queries[0].keys(), ['sql', 'time'])

        reset_queries()
        self.assertEqual(0, len(connection.queries))

    # Unfortunately with sqlite3 the in-memory test database cannot be closed.
    @skipUnlessDBFeature('test_db_allows_multiple_connections')
    @override_settings(DEBUG=True)
    def test_queries_limit(self):
        """
        Test that the backend doesn't store an unlimited number of queries.

        Regression for #12581.
        """
        old_queries_limit = BaseDatabaseWrapper.queries_limit
        BaseDatabaseWrapper.queries_limit = 3
        new_connections = ConnectionHandler(settings.DATABASES)
        new_connection = new_connections[DEFAULT_DB_ALIAS]

        # Initialize the connection and clear initialization statements.
        with new_connection.cursor():
            pass
        new_connection.queries_log.clear()

        try:
            with new_connection.cursor() as cursor:
                cursor.execute("SELECT 1" + new_connection.features.bare_select_suffix)
                cursor.execute("SELECT 2" + new_connection.features.bare_select_suffix)

            with warnings.catch_warnings(record=True) as w:
                self.assertEqual(2, len(new_connection.queries))
                self.assertEqual(0, len(w))

            with new_connection.cursor() as cursor:
                cursor.execute("SELECT 3" + new_connection.features.bare_select_suffix)
                cursor.execute("SELECT 4" + new_connection.features.bare_select_suffix)

            with warnings.catch_warnings(record=True) as w:
                self.assertEqual(3, len(new_connection.queries))
                self.assertEqual(1, len(w))
                self.assertEqual(str(w[0].message), "Limit for query logging "
                    "exceeded, only the last 3 queries will be returned.")

        finally:
            BaseDatabaseWrapper.queries_limit = old_queries_limit
            new_connection.close()


# We don't make these tests conditional because that means we would need to
# check and differentiate between:
# * MySQL+InnoDB, MySQL+MYISAM (something we currently can't do).
# * if sqlite3 (if/once we get #14204 fixed) has referential integrity turned
#   on or not, something that would be controlled by runtime support and user
#   preference.
# verify if its type is django.database.db.IntegrityError.
class FkConstraintsTests(TransactionTestCase):

    available_apps = ['backends']

    def setUp(self):
        # Create a Reporter.
        self.r = models.Reporter.objects.create(first_name='John', last_name='Smith')

    def test_integrity_checks_on_creation(self):
        """
        Try to create a model instance that violates a FK constraint. If it
        fails it should fail with IntegrityError.
        """
        a1 = models.Article(headline="This is a test", pub_date=datetime.datetime(2005, 7, 27), reporter_id=30)
        try:
            a1.save()
        except IntegrityError:
            pass
        else:
            self.skipTest("This backend does not support integrity checks.")
        # Now that we know this backend supports integrity checks we make sure
        # constraints are also enforced for proxy models. Refs #17519
        a2 = models.Article(headline='This is another test', reporter=self.r,
                            pub_date=datetime.datetime(2012, 8, 3),
                            reporter_proxy_id=30)
        self.assertRaises(IntegrityError, a2.save)

    def test_integrity_checks_on_update(self):
        """
        Try to update a model instance introducing a FK constraint violation.
        If it fails it should fail with IntegrityError.
        """
        # Create an Article.
        models.Article.objects.create(headline="Test article", pub_date=datetime.datetime(2010, 9, 4), reporter=self.r)
        # Retrieve it from the DB
        a1 = models.Article.objects.get(headline="Test article")
        a1.reporter_id = 30
        try:
            a1.save()
        except IntegrityError:
            pass
        else:
            self.skipTest("This backend does not support integrity checks.")
        # Now that we know this backend supports integrity checks we make sure
        # constraints are also enforced for proxy models. Refs #17519
        # Create another article
        r_proxy = models.ReporterProxy.objects.get(pk=self.r.pk)
        models.Article.objects.create(headline='Another article',
                                      pub_date=datetime.datetime(1988, 5, 15),
                                      reporter=self.r, reporter_proxy=r_proxy)
        # Retreive the second article from the DB
        a2 = models.Article.objects.get(headline='Another article')
        a2.reporter_proxy_id = 30
        self.assertRaises(IntegrityError, a2.save)

    def test_disable_constraint_checks_manually(self):
        """
        When constraint checks are disabled, should be able to write bad data without IntegrityErrors.
        """
        with transaction.atomic():
            # Create an Article.
            models.Article.objects.create(headline="Test article", pub_date=datetime.datetime(2010, 9, 4), reporter=self.r)
            # Retrieve it from the DB
            a = models.Article.objects.get(headline="Test article")
            a.reporter_id = 30
            try:
                connection.disable_constraint_checking()
                a.save()
                connection.enable_constraint_checking()
            except IntegrityError:
                self.fail("IntegrityError should not have occurred.")
            transaction.set_rollback(True)

    def test_disable_constraint_checks_context_manager(self):
        """
        When constraint checks are disabled (using context manager), should be able to write bad data without IntegrityErrors.
        """
        with transaction.atomic():
            # Create an Article.
            models.Article.objects.create(headline="Test article", pub_date=datetime.datetime(2010, 9, 4), reporter=self.r)
            # Retrieve it from the DB
            a = models.Article.objects.get(headline="Test article")
            a.reporter_id = 30
            try:
                with connection.constraint_checks_disabled():
                    a.save()
            except IntegrityError:
                self.fail("IntegrityError should not have occurred.")
            transaction.set_rollback(True)

    def test_check_constraints(self):
        """
        Constraint checks should raise an IntegrityError when bad data is in the DB.
        """
        with transaction.atomic():
            # Create an Article.
            models.Article.objects.create(headline="Test article", pub_date=datetime.datetime(2010, 9, 4), reporter=self.r)
            # Retrieve it from the DB
            a = models.Article.objects.get(headline="Test article")
            a.reporter_id = 30
            with connection.constraint_checks_disabled():
                a.save()
                with self.assertRaises(IntegrityError):
                    connection.check_constraints()
            transaction.set_rollback(True)


class ThreadTests(TestCase):

    def test_default_connection_thread_local(self):
        """
        Ensure that the default connection (i.e. django.db.connection) is
        different for each thread.
        Refs #17258.
        """
        # Map connections by id because connections with identical aliases
        # have the same hash.
        connections_dict = {}
        connection.cursor()
        connections_dict[id(connection)] = connection

        def runner():
            # Passing django.db.connection between threads doesn't work while
            # connections[DEFAULT_DB_ALIAS] does.
            from django.db import connections
            connection = connections[DEFAULT_DB_ALIAS]
            # Allow thread sharing so the connection can be closed by the
            # main thread.
            connection.allow_thread_sharing = True
            connection.cursor()
            connections_dict[id(connection)] = connection
        for x in range(2):
            t = threading.Thread(target=runner)
            t.start()
            t.join()
        # Check that each created connection got different inner connection.
        self.assertEqual(
            len(set(conn.connection for conn in connections_dict.values())),
            3)
        # Finish by closing the connections opened by the other threads (the
        # connection opened in the main thread will automatically be closed on
        # teardown).
        for conn in connections_dict.values():
            if conn is not connection:
                conn.close()

    def test_connections_thread_local(self):
        """
        Ensure that the connections are different for each thread.
        Refs #17258.
        """
        # Map connections by id because connections with identical aliases
        # have the same hash.
        connections_dict = {}
        for conn in connections.all():
            connections_dict[id(conn)] = conn

        def runner():
            from django.db import connections
            for conn in connections.all():
                # Allow thread sharing so the connection can be closed by the
                # main thread.
                conn.allow_thread_sharing = True
                connections_dict[id(conn)] = conn
        for x in range(2):
            t = threading.Thread(target=runner)
            t.start()
            t.join()
        self.assertEqual(len(connections_dict), 6)
        # Finish by closing the connections opened by the other threads (the
        # connection opened in the main thread will automatically be closed on
        # teardown).
        for conn in connections_dict.values():
            if conn is not connection:
                conn.close()

    def test_pass_connection_between_threads(self):
        """
        Ensure that a connection can be passed from one thread to the other.
        Refs #17258.
        """
        models.Person.objects.create(first_name="John", last_name="Doe")

        def do_thread():
            def runner(main_thread_connection):
                from django.db import connections
                connections['default'] = main_thread_connection
                try:
                    models.Person.objects.get(first_name="John", last_name="Doe")
                except Exception as e:
                    exceptions.append(e)
            t = threading.Thread(target=runner, args=[connections['default']])
            t.start()
            t.join()

        # Without touching allow_thread_sharing, which should be False by default.
        exceptions = []
        do_thread()
        # Forbidden!
        self.assertIsInstance(exceptions[0], DatabaseError)

        # If explicitly setting allow_thread_sharing to False
        connections['default'].allow_thread_sharing = False
        exceptions = []
        do_thread()
        # Forbidden!
        self.assertIsInstance(exceptions[0], DatabaseError)

        # If explicitly setting allow_thread_sharing to True
        connections['default'].allow_thread_sharing = True
        exceptions = []
        do_thread()
        # All good
        self.assertEqual(exceptions, [])

    def test_closing_non_shared_connections(self):
        """
        Ensure that a connection that is not explicitly shareable cannot be
        closed by another thread.
        Refs #17258.
        """
        # First, without explicitly enabling the connection for sharing.
        exceptions = set()

        def runner1():
            def runner2(other_thread_connection):
                try:
                    other_thread_connection.close()
                except DatabaseError as e:
                    exceptions.add(e)
            t2 = threading.Thread(target=runner2, args=[connections['default']])
            t2.start()
            t2.join()
        t1 = threading.Thread(target=runner1)
        t1.start()
        t1.join()
        # The exception was raised
        self.assertEqual(len(exceptions), 1)

        # Then, with explicitly enabling the connection for sharing.
        exceptions = set()

        def runner1():
            def runner2(other_thread_connection):
                try:
                    other_thread_connection.close()
                except DatabaseError as e:
                    exceptions.add(e)
            # Enable thread sharing
            connections['default'].allow_thread_sharing = True
            t2 = threading.Thread(target=runner2, args=[connections['default']])
            t2.start()
            t2.join()
        t1 = threading.Thread(target=runner1)
        t1.start()
        t1.join()
        # No exception was raised
        self.assertEqual(len(exceptions), 0)


class MySQLPKZeroTests(TestCase):
    """
    Zero as id for AutoField should raise exception in MySQL, because MySQL
    does not allow zero for autoincrement primary key.
    """
    @skipIfDBFeature('allows_auto_pk_0')
    def test_zero_as_autoval(self):
        with self.assertRaises(ValueError):
            models.Square.objects.create(id=0, root=0, square=1)


class DBConstraintTestCase(TransactionTestCase):

    available_apps = ['backends']

    def test_can_reference_existant(self):
        obj = models.Object.objects.create()
        ref = models.ObjectReference.objects.create(obj=obj)
        self.assertEqual(ref.obj, obj)

        ref = models.ObjectReference.objects.get(obj=obj)
        self.assertEqual(ref.obj, obj)

    def test_can_reference_non_existant(self):
        self.assertFalse(models.Object.objects.filter(id=12345).exists())
        ref = models.ObjectReference.objects.create(obj_id=12345)
        ref_new = models.ObjectReference.objects.get(obj_id=12345)
        self.assertEqual(ref, ref_new)

        with self.assertRaises(models.Object.DoesNotExist):
            ref.obj

    def test_many_to_many(self):
        obj = models.Object.objects.create()
        obj.related_objects.create()
        self.assertEqual(models.Object.objects.count(), 2)
        self.assertEqual(obj.related_objects.count(), 1)

        intermediary_model = models.Object._meta.get_field_by_name("related_objects")[0].rel.through
        intermediary_model.objects.create(from_object_id=obj.id, to_object_id=12345)
        self.assertEqual(obj.related_objects.count(), 1)
        self.assertEqual(intermediary_model.objects.count(), 2)


class BackendUtilTests(TestCase):

    def test_format_number(self):
        """
        Test the format_number converter utility
        """
        def equal(value, max_d, places, result):
            self.assertEqual(format_number(Decimal(value), max_d, places), result)

        equal('0', 12, 3,
              '0.000')
        equal('0', 12, 8,
              '0.00000000')
        equal('1', 12, 9,
              '1.000000000')
        equal('0.00000000', 12, 8,
              '0.00000000')
        equal('0.000000004', 12, 8,
              '0.00000000')
        equal('0.000000008', 12, 8,
              '0.00000001')
        equal('0.000000000000000000999', 10, 8,
              '0.00000000')
        equal('0.1234567890', 12, 10,
              '0.1234567890')
        equal('0.1234567890', 12, 9,
              '0.123456789')
        equal('0.1234567890', 12, 8,
              '0.12345679')
        equal('0.1234567890', 12, 5,
              '0.12346')
        equal('0.1234567890', 12, 3,
              '0.123')
        equal('0.1234567890', 12, 1,
              '0.1')
        equal('0.1234567890', 12, 0,
              '0')


class DBTestSettingsRenamedTests(IgnoreAllDeprecationWarningsMixin, TestCase):

    mismatch_msg = ("Connection 'test-deprecation' has mismatched TEST "
                    "and TEST_* database settings.")

    @classmethod
    def setUpClass(cls):
        # Silence "UserWarning: Overriding setting DATABASES can lead to
        # unexpected behavior."
        cls.warning_classes.append(UserWarning)

    def setUp(self):
        super(DBTestSettingsRenamedTests, self).setUp()
        self.handler = ConnectionHandler()
        self.db_settings = {'default': {}}

    def test_mismatched_database_test_settings_1(self):
        # if the TEST setting is used, all TEST_* keys must appear in it.
        self.db_settings.update({
            'test-deprecation': {
                'TEST': {},
                'TEST_NAME': 'foo',
            }
        })
        with override_settings(DATABASES=self.db_settings):
            with self.assertRaisesMessage(ImproperlyConfigured, self.mismatch_msg):
                self.handler.prepare_test_settings('test-deprecation')

    def test_mismatched_database_test_settings_2(self):
        # if the TEST setting is used, all TEST_* keys must match.
        self.db_settings.update({
            'test-deprecation': {
                'TEST': {'NAME': 'foo'},
                'TEST_NAME': 'bar',
            },
        })
        with override_settings(DATABASES=self.db_settings):
            with self.assertRaisesMessage(ImproperlyConfigured, self.mismatch_msg):
                self.handler.prepare_test_settings('test-deprecation')

    def test_mismatched_database_test_settings_3(self):
        # Verifies the mapping of an aliased key.
        self.db_settings.update({
            'test-deprecation': {
                'TEST': {'CREATE_DB': 'foo'},
                'TEST_CREATE': 'bar',
            },
        })
        with override_settings(DATABASES=self.db_settings):
            with self.assertRaisesMessage(ImproperlyConfigured, self.mismatch_msg):
                self.handler.prepare_test_settings('test-deprecation')

    def test_mismatched_database_test_settings_4(self):
        # Verifies the mapping of an aliased key when the aliased key is missing.
        self.db_settings.update({
            'test-deprecation': {
                'TEST': {},
                'TEST_CREATE': 'bar',
            },
        })
        with override_settings(DATABASES=self.db_settings):
            with self.assertRaisesMessage(ImproperlyConfigured, self.mismatch_msg):
                self.handler.prepare_test_settings('test-deprecation')

    def test_mismatched_settings_old_none(self):
        self.db_settings.update({
            'test-deprecation': {
                'TEST': {'CREATE_DB': None},
                'TEST_CREATE': '',
            },
        })
        with override_settings(DATABASES=self.db_settings):
            with self.assertRaisesMessage(ImproperlyConfigured, self.mismatch_msg):
                self.handler.prepare_test_settings('test-deprecation')

    def test_mismatched_settings_new_none(self):
        self.db_settings.update({
            'test-deprecation': {
                'TEST': {},
                'TEST_CREATE': None,
            },
        })
        with override_settings(DATABASES=self.db_settings):
            with self.assertRaisesMessage(ImproperlyConfigured, self.mismatch_msg):
                self.handler.prepare_test_settings('test-deprecation')

    def test_matched_test_settings(self):
        # should be able to define new settings and the old, if they match
        self.db_settings.update({
            'test-deprecation': {
                'TEST': {'NAME': 'foo'},
                'TEST_NAME': 'foo',
            },
        })
        with override_settings(DATABASES=self.db_settings):
            self.handler.prepare_test_settings('test-deprecation')

    def test_new_settings_only(self):
        # should be able to define new settings without the old
        self.db_settings.update({
            'test-deprecation': {
                'TEST': {'NAME': 'foo'},
            },
        })
        with override_settings(DATABASES=self.db_settings):
            self.handler.prepare_test_settings('test-deprecation')

    def test_old_settings_only(self):
        # should be able to define old settings without the new
        self.db_settings.update({
            'test-deprecation': {
                'TEST_NAME': 'foo',
            },
        })
        with override_settings(DATABASES=self.db_settings):
            self.handler.prepare_test_settings('test-deprecation')

    def test_empty_settings(self):
        with override_settings(DATABASES=self.db_settings):
            self.handler.prepare_test_settings('default')
