"""pytest-asyncio implementation."""
import asyncio
import contextlib
import functools
import inspect
import socket

import pytest
try:
    from _pytest.python import transfer_markers
except ImportError:  # Pytest 4.1.0 removes the transfer_marker api (#104)
    def transfer_markers(*args, **kwargs):  # noqa
        """Noop when over pytest 4.1.0"""
        pass

try:
    from async_generator import isasyncgenfunction
except ImportError:
    from inspect import isasyncgenfunction


def _is_coroutine(obj):
    """Check to see if an object is really an asyncio coroutine."""
    return asyncio.iscoroutinefunction(obj) or inspect.isgeneratorfunction(obj)


def pytest_configure(config):
    """Inject documentation."""
    config.addinivalue_line("markers",
                            "asyncio: "
                            "mark the test as a coroutine, it will be "
                            "run using an asyncio event loop")


@pytest.mark.tryfirst
def pytest_pycollect_makeitem(collector, name, obj):
    """A pytest hook to collect asyncio coroutines."""
    if collector.funcnamefilter(name) and _is_coroutine(obj):
        item = pytest.Function.from_parent(collector, name=name)

        # Due to how pytest test collection works, module-level pytestmarks
        # are applied after the collection step. Since this is the collection
        # step, we look ourselves.
        transfer_markers(obj, item.cls, item.module)
        item = pytest.Function.from_parent(collector, name=name)  # To reload keywords.

        if 'asyncio' in item.keywords:
            return list(collector._genfunctions(name, obj))


@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(fixturedef, request):
    """Adjust the event loop policy when an event loop is produced."""
    if fixturedef.argname == "event_loop" and 'asyncio' in request.keywords:
        outcome = yield
        loop = outcome.get_result()
        policy = asyncio.get_event_loop_policy()
        try:
            old_loop = policy.get_event_loop()
        except RuntimeError as exc:
            if 'no current event loop' not in str(exc):
                raise
            old_loop = None
        policy.set_event_loop(loop)
        fixturedef.addfinalizer(lambda: policy.set_event_loop(old_loop))
        return

    if isasyncgenfunction(fixturedef.func):
        # This is an async generator function. Wrap it accordingly.
        generator = fixturedef.func

        strip_request = False
        if 'request' not in fixturedef.argnames:
            fixturedef.argnames += ('request', )
            strip_request = True

        def wrapper(*args, **kwargs):
            request = kwargs['request']
            if strip_request:
                del kwargs['request']

            gen_obj = generator(*args, **kwargs)

            async def setup():
                res = await gen_obj.__anext__()
                return res

            def finalizer():
                """Yield again, to finalize."""
                async def async_finalizer():
                    try:
                        await gen_obj.__anext__()
                    except StopAsyncIteration:
                        pass
                    else:
                        msg = "Async generator fixture didn't stop."
                        msg += "Yield only once."
                        raise ValueError(msg)
                asyncio.get_event_loop().run_until_complete(async_finalizer())

            request.addfinalizer(finalizer)
            return asyncio.get_event_loop().run_until_complete(setup())

        fixturedef.func = wrapper
    elif inspect.iscoroutinefunction(fixturedef.func):
        coro = fixturedef.func

        def wrapper(*args, **kwargs):
            async def setup():
                res = await coro(*args, **kwargs)
                return res

            return asyncio.get_event_loop().run_until_complete(setup())

        fixturedef.func = wrapper
    yield


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_pyfunc_call(pyfuncitem):
    """
    Run asyncio marked test functions in an event loop instead of a normal
    function call.
    """
    if 'asyncio' in pyfuncitem.keywords:
        if getattr(pyfuncitem.obj, 'is_hypothesis_test', False):
            pyfuncitem.obj.hypothesis.inner_test = wrap_in_sync(
                pyfuncitem.obj.hypothesis.inner_test
            )
        else:
            pyfuncitem.obj = wrap_in_sync(pyfuncitem.obj)
    yield


def wrap_in_sync(func):
    """Return a sync wrapper around an async function executing it in the
    current event loop."""

    @functools.wraps(func)
    def inner(**kwargs):
        coro = func(**kwargs)
        if coro is not None:
            task = asyncio.ensure_future(coro)
            try:
                asyncio.get_event_loop().run_until_complete(task)
            except BaseException:
                # run_until_complete doesn't get the result from exceptions
                # that are not subclasses of `Exception`. Consume all
                # exceptions to prevent asyncio's warning from logging.
                if task.done() and not task.cancelled():
                    task.exception()
                raise
    return inner


def pytest_runtest_setup(item):
    if 'asyncio' in item.keywords and 'event_loop' not in item.fixturenames:
        # inject an event loop fixture for all async tests
        item.fixturenames.append('event_loop')
    if item.get_closest_marker("asyncio") is not None \
        and not getattr(item.obj, 'hypothesis', False) \
        and getattr(item.obj, 'is_hypothesis_test', False):
            pytest.fail(
                'test function `%r` is using Hypothesis, but pytest-asyncio '
                'only works with Hypothesis 3.64.0 or later.' % item
            )


@pytest.fixture
def event_loop(request):
    """Create an instance of the default event loop for each test case."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


def _unused_tcp_port():
    """Find an unused localhost TCP port from 1024-65535 and return it."""
    with contextlib.closing(socket.socket()) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


@pytest.fixture
def unused_tcp_port():
    return _unused_tcp_port()


@pytest.fixture
def unused_tcp_port_factory():
    """A factory function, producing different unused TCP ports."""
    produced = set()

    def factory():
        """Return an unused port."""
        port = _unused_tcp_port()

        while port in produced:
            port = _unused_tcp_port()

        produced.add(port)

        return port
    return factory
