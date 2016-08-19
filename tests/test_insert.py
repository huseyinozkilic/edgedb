##
# Copyright (c) 2016 MagicStack Inc.
# All rights reserved.
#
# See LICENSE for details.
##


import uuid

from edgedb.lang.common import datetime
from edgedb.client import exceptions as exc
from edgedb.server import _testbase as tb


class TestInsert(tb.QueryTestCase):
    SETUP = """
        CREATE LINK {test::l1} {
            SET mapping := '11';
            SET readonly := False;
        };
        CREATE LINK {test::l2} {
            SET mapping := '11';
            SET readonly := False;
        };
        CREATE LINK {test::l3} {
            SET mapping := '11';
            SET readonly := False;
        };

        CREATE CONCEPT {test::InsertTest} INHERITING {std::Object} {
            CREATE LINK {test::l1} TO {std::int} {
                SET mapping := '11';
                SET readonly := False;
            };
            CREATE REQUIRED LINK {test::l2} TO {std::int} {
                SET mapping := '11';
                SET readonly := False;
            };
            CREATE LINK {test::l3} TO {std::str} {
                SET mapping := '11';
                SET readonly := False;
                SET default := 'test';
            };
        };
    """

    TEARDOWN = """
    """

    async def test_insert_fail_1(self):
        err = 'missing value for required pointer ' + \
              '{test::InsertTest}.{test::l2}'
        with self.assertRaisesRegex(exc.MissingRequiredPointerError, err):
            await self.con.execute('''
                INSERT {test::InsertTest};
            ''')

    async def test_insert_simple01(self, input=r"""
        INSERT {test::InsertTest} {
            l2 := 0,
            l3 := 'test'
        };

        INSERT {test::InsertTest} {
            l3 := "Test\"1\"",
            l2 := 1
        };

        INSERT {test::InsertTest} {
            l3 := 'Test\'2\'',
            l2 := 2
        };

        INSERT {test::InsertTest} {
            l3 := '\"Test\'3\'\"',
            l2 := 3
        };

        SELECT
            {test::InsertTest} {
                l2, l3
            }
        ORDER BY {test::InsertTest}.l2;

        """) -> [
        [],

        [],

        [],

        [],

        [{
            'id': uuid.UUID,
            'l2': 0,
            'l3': 'test',
        }, {
            'id': uuid.UUID,
            'l2': 1,
            'l3': 'Test"1"',
        }, {
            'id': uuid.UUID,
            'l2': 2,
            'l3': "Test'2'",
        }, {
            'id': uuid.UUID,
            'l2': 3,
            'l3': '''"Test'3'"''',
        }]
    ]:
        pass