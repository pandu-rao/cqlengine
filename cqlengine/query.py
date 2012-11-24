from collections import namedtuple
import copy
from hashlib import md5
from time import time

from cqlengine.connection import get_connection
from cqlengine.exceptions import CQLEngineException

#CQL 3 reference:
#http://www.datastax.com/docs/1.1/references/cql/index

class QueryException(CQLEngineException): pass
class QueryOperatorException(QueryException): pass

class QueryOperator(object):
    # The symbol that identifies this operator in filter kwargs
    # ie: colname__<symbol>
    symbol = None

    # The comparator symbol this operator
    # uses in cql
    cql_symbol = None

    def __init__(self, column, value):
        self.column = column
        self.value = value

        #the identifier is a unique key that will be used in string
        #replacement on query strings, it's created from a hash
        #of this object's id and the time
        self.identifier = md5(str(id(self)) + str(time())).hexdigest()

        #perform validation on this operator
        self.validate_operator()
        self.validate_value()

    @property
    def cql(self):
        """
        Returns this operator's portion of the WHERE clause
        :param valname: the dict key that this operator's compare value will be found in
        """
        return '{} {} :{}'.format(self.column.db_field_name, self.cql_symbol, self.identifier)

    def validate_operator(self):
        """
        Checks that this operator can be used on the column provided
        """
        if self.symbol is None:
            raise QueryOperatorException(
                    "{} is not a valid operator, use one with 'symbol' defined".format(
                        self.__class__.__name__
                    )
                )
        if self.cql_symbol is None:
            raise QueryOperatorException(
                    "{} is not a valid operator, use one with 'cql_symbol' defined".format(
                        self.__class__.__name__
                    )
                )

    def validate_value(self):
        """
        Checks that the compare value works with this operator

        Doesn't do anything by default
        """
        pass

    def get_dict(self):
        """
        Returns this operators contribution to the cql.query arg dictionanry

        ie: if this column's name is colname, and the identifier is colval,
        this should return the dict: {'colval':<self.value>}
        SELECT * FROM column_family WHERE colname=:colval
        """
        return {self.identifier: self.value}

    @classmethod
    def get_operator(cls, symbol):
        if not hasattr(cls, 'opmap'):
            QueryOperator.opmap = {}
            def _recurse(klass):
                if klass.symbol:
                    QueryOperator.opmap[klass.symbol.upper()] = klass
                for subklass in klass.__subclasses__():
                    _recurse(subklass)
                pass
            _recurse(QueryOperator)
        try:
            return QueryOperator.opmap[symbol.upper()]
        except KeyError:
            raise QueryOperatorException("{} doesn't map to a QueryOperator".format(symbol))

class EqualsOperator(QueryOperator):
    symbol = 'EQ'
    cql_symbol = '='

class InOperator(EqualsOperator):
    symbol = 'IN'
    cql_symbol = 'IN'

class GreaterThanOperator(QueryOperator):
    symbol = "GT"
    cql_symbol = '>'

class GreaterThanOrEqualOperator(QueryOperator):
    symbol = "GTE"
    cql_symbol = '>='

class LessThanOperator(QueryOperator):
    symbol = "LT"
    cql_symbol = '<'

class LessThanOrEqualOperator(QueryOperator):
    symbol = "LTE"
    cql_symbol = '<='

class QuerySet(object):
    #TODO: support specifying offset and limit (use slice) (maybe return a mutated queryset)
    #TODO: support specifying columns to exclude or select only
    #TODO: support ORDER BY
    #TODO: cache results in this instance, but don't copy them on deepcopy

    def __init__(self, model):
        super(QuerySet, self).__init__()
        self.model = model
        self.column_family_name = self.model.column_family_name()

        #Where clause filters
        self._where = []

        #ordering arguments
        self._order = []

        #subset selection
        self._limit = None
        self._start = None

        #see the defer and only methods
        self._defer_fields = []
        self._only_fields = []

        self._cursor = None

    def __unicode__(self):
        return self._select_query()

    def __str__(self):
        return str(self.__unicode__())

    #----query generation / execution----

    def _validate_where_syntax(self):
        """ Checks that a filterset will not create invalid cql """

        #check that there's either a = or IN relationship with a primary key or indexed field
        equal_ops = [w for w in self._where if isinstance(w, EqualsOperator)]
        if not any([w.column.primary_key or w.column.index for w in equal_ops]):
            raise QueryException('Where clauses require either a "=" or "IN" comparison with either a primary key or indexed field')
        #TODO: abuse this to see if we can get cql to raise an exception

    def _where_clause(self):
        """ Returns a where clause based on the given filter args """
        self._validate_where_syntax()
        return ' AND '.join([f.cql for f in self._where])

    def _where_values(self):
        """ Returns the value dict to be passed to the cql query """
        values = {}
        for where in self._where:
            values.update(where.get_dict())
        return values

    def _select_query(self, count=False):
        """
        Returns a select clause based on the given filter args

        :param count: indicates this should return a count query only
        """
        qs = []
        if count:
            qs += ['SELECT COUNT(*)']
        else:
            fields = self.model._columns.keys()
            if self._defer_fields:
                fields = [f for f in fields if f not in self._defer_fields]
            elif self._only_fields:
                fields = [f for f in fields if f in self._only_fields]
            db_fields = [self.model._columns[f].db_field_name for f in fields]
            qs += ['SELECT {}'.format(', '.join(db_fields))]

        qs += ['FROM {}'.format(self.column_family_name)]

        if self._where:
            qs += ['WHERE {}'.format(self._where_clause())]

        if not count:
            #TODO: add support for limit, start, order by, and reverse
            pass

        return ' '.join(qs)

    #----Reads------

    def __iter__(self):
        if self._cursor is None:
            conn = get_connection(self.model.keyspace)
            self._cursor = conn.cursor()
            self._cursor.execute(self._select_query(), self._where_values())
            self._rowcount = self._cursor.rowcount
        return self

    def _construct_instance(self, values):
        #translate column names to model names
        field_dict = {}
        db_map = self.model._db_map
        for key, val in values.items():
            if key in db_map:
                field_dict[db_map[key]] = val
            else:
                field_dict[key] = val
        return self.model(**field_dict)

    def _get_next(self):
        """ Gets the next cursor result """
        cur = self._cursor
        values = cur.fetchone()
        if values is None: return
        names = [i[0] for i in cur.description]
        value_dict = dict(zip(names, values))
        return self._construct_instance(value_dict)

    def next(self):
        instance = self._get_next() 
        if instance is None: raise StopIteration
        return instance

    def first(self):
        pass

    def all(self):
        clone = copy.deepcopy(self)
        clone._where = []
        return clone

    def _parse_filter_arg(self, arg):
        """
        Parses a filter arg in the format:
        <colname>__<op>
        :returns: colname, op tuple
        """
        statement = arg.split('__')
        if len(statement) == 1:
            return arg, None
        elif len(statement) == 2:
            return statement[0], statement[1]
        else:
            raise QueryException("Can't parse '{}'".format(arg))

    def filter(self, **kwargs):
        #add arguments to the where clause filters
        clone = copy.deepcopy(self)
        for arg, val in kwargs.items():
            col_name, col_op = self._parse_filter_arg(arg)
            #resolve column and operator
            try:
                column = self.model._columns[col_name]
            except KeyError:
                raise QueryException("Can't resolve column name: '{}'".format(col_name))

            #get query operator, or use equals if not supplied
            operator_class = QueryOperator.get_operator(col_op or 'EQ')
            operator = operator_class(column, val)

            clone._where.append(operator)

        return clone

    def __call__(self, **kwargs):
        return self.filter(**kwargs)

    def count(self):
        """ Returns the number of rows matched by this query """
        con = get_connection(self.model.keyspace)
        cur = con.cursor()
        cur.execute(self._select_query(count=True), self._where_values())
        return cur.fetchone()[0]

    def __len__(self):
        return self.count()

    def find(self, pk):
        """
        loads one document identified by it's primary key
        """
        #TODO: make this a convenience wrapper of the filter method
        #TODO: rework this to work with multiple primary keys
        qs = 'SELECT * FROM {column_family} WHERE {pk_name}=:{pk_name}'
        qs = qs.format(column_family=self.column_family_name,
                       pk_name=self.model._pk_name)
        conn = get_connection(self.model.keyspace)
        self._cursor = conn.cursor()
        self._cursor.execute(qs, {self.model._pk_name:pk})
        return self._get_next()

    def _only_or_defer(self, action, fields):
        clone = copy.deepcopy(self)
        if clone._defer_fields or clone._only_fields:
            raise QueryException("QuerySet alread has only or defer fields defined")

        #check for strange fields
        missing_fields = [f for f in fields if f not in self.model._columns.keys()]
        if missing_fields:
            raise QueryException("Can't resolve fields {} in {}".format(', '.join(missing_fields), self.model.__name__))

        if action == 'defer':
            clone._defer_fields = fields
        elif action == 'only':
            clone._only_fields = fields
        else:
            raise ValueError

        return clone

    def only(self, fields):
        """ Load only these fields for the returned query """
        return self._only_or_defer('only', fields)

    def defer(self, fields):
        """ Don't load these fields for the returned query """
        return self._only_or_defer('defer', fields)

    #----writes----
    def save(self, instance):
        """
        Creates / updates a row.
        This is a blind insert call.
        All validation and cleaning needs to happen 
        prior to calling this.
        """
        assert type(instance) == self.model

        #organize data
        value_pairs = []
        values = instance.as_dict()

        #get defined fields and their column names
        for name, col in self.model._columns.items():
            value_pairs += [(col.db_field_name, values.get(name))]

        #construct query string
        field_names = zip(*value_pairs)[0]
        field_values = dict(value_pairs)
        qs = ["INSERT INTO {}".format(self.column_family_name)]
        qs += ["({})".format(', '.join(field_names))]
        qs += ['VALUES']
        qs += ["({})".format(', '.join([':'+f for f in field_names]))]
        qs = ' '.join(qs)

        conn = get_connection(self.model.keyspace)
        cur = conn.cursor()
        cur.execute(qs, field_values)

    def create(self, **kwargs):
        return self.model(**kwargs).save()

    #----delete---
    def delete(self, columns=[]):
        """
        Deletes the contents of a query

        :returns: number of rows deleted
        """
        qs = ['DELETE FROM {}'.format(self.column_family_name)]
        if self._where:
            qs += ['WHERE {}'.format(self._where_clause())]
        qs = ' '.join(qs)

        #TODO: Return number of rows deleted
        con = get_connection(self.model.keyspace)
        cur = con.cursor()
        cur.execute(qs, self._where_values())
        return cur.fetchone()



    def delete_instance(self, instance):
        """ Deletes one instance """
        pk_name = self.model._pk_name
        qs = ['DELETE FROM {}'.format(self.column_family_name)]
        qs += ['WHERE {0}=:{0}'.format(pk_name)]
        qs = ' '.join(qs)

        conn = get_connection(self.model.keyspace)
        cur = conn.cursor()
        cur.execute(qs, {pk_name:instance.pk})

