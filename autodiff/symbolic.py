import copy
import numpy as np
import theano
import theano.tensor as tt
from inspect import getargspec

from autodiff.context import Context
from autodiff.compat import OrderedDict
from autodiff.utils import orderedcallargs


class Symbolic(object):
    def __init__(self, pyfn):
        # make deepcopy of pyfn because we might change its defaults
        self._pyfn = copy.deepcopy(pyfn)

        self.s_vars = OrderedDict()
        self.s_args = OrderedDict()
        self.s_results = OrderedDict()
        self._cache = dict()

        # replace integer defaults in pyfn to avoid problems
        if self._pyfn.func_defaults:
            new_defaults = tuple([np.int_(d)
                                  if type(d) is int and -5 <= d <= 256 else d
                                  for d in self._pyfn.func_defaults])
            self._pyfn.func_defaults = new_defaults

    @property
    def pyfn(self):
        return self._pyfn

    @property
    def cache(self):
        return self._cache

    def trace(self, *args, **kwargs):
        """
        Given args and kwargs, call the Python function and get its
        symbolic representation.

        Three ordered dictionaries are maintained:
            self.s_vars   : {id(obj) : sym_var}
                            Contains all symbolic variables traced during
                            function execution, indexed by the id of the
                            corresponding Python object.

            self.s_args   : {arg name : sym_var}
                            Contains all symbolic inputs to the function,
                            indexed by the argument name.

            self.s_results : {id(obj) : sym_var}
                            Contains the symbolic function results, indexed by
                            the id of the corresponding Python object.

            The dictionaries are cleared every time this method is run.

        """

        # check for small ints and collections
        def check(name, i):
            # Check argument i (with name 'name') for small ints or
            # collections.  If it is a small int, replace it with a numpy int.
            # If a collection, raise a helpful error.
            #
            # This is required because:
            #     1. PyAutoDiff can not shadow CPython ints because they are
            #     cached objects that reuse ids.
            #
            #     2. Theano functions can not accept arguments that are
            #     collections.
            if type(i) is int and -5 <= i <= 256:
                return np.int_(i)
            elif isinstance(i, (list, tuple, dict)):
                raise TypeError('Function arguments can not be '
                                'containers (received {0} for '
                                'argument \'{1}\').'.format(i, name))
            else:
                return i

        argspec = getargspec(self.pyfn)
        tmp_args = tuple(check(n, a) for n, a in zip(argspec.args, args))
        args = tmp_args + tuple(check(argspec.varargs, a)
                                for a in args[len(argspec.args):])
        kwargs = OrderedDict((k, check(k, v)) for k, v in kwargs.iteritems())

        # clear symbolic dictionaries
        self.s_vars.clear()
        self.s_args.clear()
        self.s_results.clear()

        # trace the function
        c = Context()
        results = c.call(self.pyfn, args, kwargs)

        # collect symbolic variables in s_vars
        self.s_vars.update(c.svars)

        # collect symbolic arguments in s_args
        callargs = orderedcallargs(self.pyfn, *args, **kwargs)

        for name, arg in callargs.iteritems():

            # collect variable args
            if name == argspec.varargs:
                self.s_args[name] = ()
                for i, a in enumerate(arg):
                    try:
                        self.s_args[name] += (self.s_vars[id(a)],)
                        self.s_args[name][-1].name = '{0}_{1}'.format(name, i)
                    except KeyError:
                        raise KeyError('Unable to trace item {0} of variable '
                                       'argument \'{1}\'.'.format(i, name))
                    except:
                        raise

            # collect variable kwargs
            elif name == argspec.keywords:
                for n, a in arg.iteritems():
                    try:
                        self.s_args[n] = self.s_vars[id(a)]
                        self.s_args[n].name = n
                    except KeyError:
                        raise KeyError('Unable to trace argument '
                                       '\'{0}\'.'.format(n))
                    except:
                        raise

            # collect positional args
            else:
                try:
                    self.s_args[name] = self.s_vars[id(arg)]
                    self.s_args[name].name = name
                except KeyError:
                    raise KeyError('Unable to trace argument '
                                   '\'{0}\'.'.format(name))
                except:
                    raise

        # collect symbolic results in s_results
        if not isinstance(results, tuple):
            results = [results]
        for i, r in enumerate(results):
            try:
                self.s_results[id(r)] = self.s_vars[id(r)]
            except KeyError:
                raise KeyError('Unable to trace result #{0} '
                               '(indexed from 1).'.format(i + 1))
            except:
                raise


class Function(Symbolic):

    def _compile_function(self, args, kwargs):
        argspec = getargspec(self.pyfn)
        callargs = orderedcallargs(self.pyfn, *args, **kwargs)

        # trace the function
        self.trace(*args, **kwargs)

        # collect givens
        givens = OrderedDict()
        for name, arg in self.s_args.iteritems():
            # check for the varargs tuple
            if name != argspec.varargs:
                givens[arg] = arg.type(name='{0}'.format(arg.name))
            else:
                givens.update(
                    (v, v.type(name='{0}_{1}'.format(argspec.varargs, i)))
                    for i, v in enumerate(arg))

        # collect inputs
        defaults = dict()
        if argspec.defaults:
            default_slice = slice(-len(argspec.defaults),
                                  -1 if argspec.varargs else None)
            defaults.update(zip(argspec.args[default_slice],
                                argspec.defaults))
        inputs = [theano.Param(
            givens[arg], default=defaults.get(name), name=name)
            for name, arg in self.s_args.iteritems()
            if name is not argspec.varargs]

        inputs.extend(givens[a] for a in self.s_args.get(argspec.varargs, ()))

        # collect outputs
        outputs = self.s_results.values()
        if len(outputs) == 1:
            outputs = outputs[0]

        # compile function
        fn = theano.function(inputs=inputs,
                             outputs=outputs,
                             givens=givens,
                             on_unused_input='ignore')

        # store in cache corresponding to the number of positional inputs
        self.cache[len(callargs.get(argspec.varargs, ()))] = fn

        return fn

    def __call__(self, *args, **kwargs):
        return self.call(*args, **kwargs)

    def call(self, *args, **kwargs):
        argspec = getargspec(self.pyfn)
        callargs = orderedcallargs(self.pyfn, *args, **kwargs)

        # try to retrieve function from cache; otherwise compile
        fn = self.cache.get(len(callargs.get(argspec.varargs, ())),
                            self._compile_function(args, kwargs))

        pos_args = [callargs[arg] for arg in argspec.args]
        pos_args.extend(callargs.get(argspec.varargs, ()))
        kw_args = callargs.get(argspec.keywords, {})

        return fn(*pos_args, **kw_args)


class Gradient(object):
    def __init__(self, fn):
        self._fn = fn
        self._grad_fn = None

    @property
    def fn(self):
        return self._fn

    @property
    def grad_fn(self):
        return self._grad_fn

    def __call__(self, *args, **kwargs):
        # TODO: convert small ints to arrays to allow tracking, but
        # watch out for functions that require int arguments
        if self.grad_fn is None:
            ctxt = Context()
            result = ctxt.call(self.fn, args, kwargs)

            try:
                s_args = [ctxt.svars[id(a)] for a in args]
                s_kwargs = [ctxt.svars[id(v)] for v in kwargs.values()]
                s_result = ctxt.svars[id(result)]
            except KeyError:
                print 'ERROR: PyAD was unable to trace the requested variable.'
                raise
            except:
                raise
            grad = tt.grad(s_result, s_args + s_kwargs)
            if len(grad) == 1:
                grad = grad[0]

            self._grad_fn = theano.function([s_args + s_kwargs], grad)
            self._sym_grad = grad

        all_args = args + tuple(kwargs.values())
        try:
            grad_result = self.grad_fn(*all_args)
        except:
            self._grad_fn = None
            raise
        return grad_result
