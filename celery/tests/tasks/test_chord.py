from __future__ import absolute_import

from mock import patch
from contextlib import contextmanager

from celery import group
from celery import canvas
from celery import result
from celery.exceptions import ChordError
from celery.five import range
from celery.result import AsyncResult, GroupResult, EagerResult
from celery.tests.case import AppCase, Mock

passthru = lambda x: x


class ChordCase(AppCase):

    def setup(self):

        @self.app.task
        def add(x, y):
            return x + y
        self.add = add


class TSR(GroupResult):
    is_ready = True
    value = None

    def ready(self):
        return self.is_ready

    def join(self, propagate=True, **kwargs):
        if propagate:
            for value in self.value:
                if isinstance(value, Exception):
                    raise value
        return self.value
    join_native = join

    def _failed_join_report(self):
        for value in self.value:
            if isinstance(value, Exception):
                yield EagerResult('some_id', value, 'FAILURE')


class TSRNoReport(TSR):

    def _failed_join_report(self):
        return iter([])


@contextmanager
def patch_unlock_retry(app):
    unlock = app.tasks['celery.chord_unlock']
    retry = Mock()
    prev, unlock.retry = unlock.retry, retry
    try:
        yield unlock, retry
    finally:
        unlock.retry = prev


class test_unlock_chord_task(ChordCase):

    @patch('celery.result.GroupResult')
    def test_unlock_ready(self, GroupResult):

        class AlwaysReady(TSR):
            is_ready = True
            value = [2, 4, 8, 6]

        with self._chord_context(AlwaysReady) as (cb, retry, _):
            cb.type.apply_async.assert_called_with(
                ([2, 4, 8, 6], ), {}, task_id=cb.id,
            )
            # did not retry
            self.assertFalse(retry.call_count)

    def test_callback_fails(self):
        class AlwaysReady(TSR):
            is_ready = True
            value = [2, 4, 8, 6]

        def setup(callback):
            callback.apply_async.side_effect = IOError()

        with self._chord_context(AlwaysReady, setup) as (cb, retry, fail):
            self.assertTrue(fail.called)
            self.assertEqual(
                fail.call_args[0][0], cb.id,
            )
            self.assertIsInstance(
                fail.call_args[1]['exc'], ChordError,
            )

    def test_unlock_ready_failed(self):

        class Failed(TSR):
            is_ready = True
            value = [2, KeyError('foo'), 8, 6]

        with self._chord_context(Failed) as (cb, retry, fail_current):
            self.assertFalse(cb.type.apply_async.called)
            # did not retry
            self.assertFalse(retry.call_count)
            self.assertTrue(fail_current.called)
            self.assertEqual(
                fail_current.call_args[0][0], cb.id,
            )
            self.assertIsInstance(
                fail_current.call_args[1]['exc'], ChordError,
            )
            self.assertIn('some_id', str(fail_current.call_args[1]['exc']))

    def test_unlock_ready_failed_no_culprit(self):
        class Failed(TSRNoReport):
            is_ready = True
            value = [2, KeyError('foo'), 8, 6]

        with self._chord_context(Failed) as (cb, retry, fail_current):
            self.assertTrue(fail_current.called)
            self.assertEqual(
                fail_current.call_args[0][0], cb.id,
            )
            self.assertIsInstance(
                fail_current.call_args[1]['exc'], ChordError,
            )

    @contextmanager
    def _chord_context(self, ResultCls, setup=None, **kwargs):
        with patch('celery.result.GroupResult'):

            @self.app.task()
            def callback(*args, **kwargs):
                pass

            pts, result.GroupResult = result.GroupResult, ResultCls
            callback.apply_async = Mock()
            callback_s = callback.s()
            callback_s.id = 'callback_id'
            fail_current = self.app.backend.fail_from_current_stack = Mock()
            try:
                with patch_unlock_retry(self.app) as (unlock, retry):
                    subtask, canvas.maybe_subtask = (
                        canvas.maybe_subtask, passthru,
                    )
                    if setup:
                        setup(callback)
                    try:
                        unlock(
                            'group_id', callback_s,
                            result=[AsyncResult(r) for r in ['1', 2, 3]],
                            GroupResult=ResultCls, **kwargs
                        )
                    finally:
                        canvas.maybe_subtask = subtask
                    yield callback_s, retry, fail_current
            finally:
                result.GroupResult = pts

    @patch('celery.result.GroupResult')
    def test_when_not_ready(self, GroupResult):
        class NeverReady(TSR):
            is_ready = False

        with self._chord_context(NeverReady, interval=10, max_retries=30) \
                as (cb, retry, _):
            self.assertFalse(cb.type.apply_async.called)
            # did retry
            retry.assert_called_with(countdown=10, max_retries=30)

    def test_is_in_registry(self):
        self.assertIn('celery.chord_unlock', self.app.tasks)


class test_chord(ChordCase):

    def test_eager(self):
        from celery import chord

        @self.app.task()
        def addX(x, y):
            return x + y

        @self.app.task()
        def sumX(n):
            return sum(n)

        self.app.conf.CELERY_ALWAYS_EAGER = True
        try:
            x = chord(addX.s(i, i) for i in range(10))
            body = sumX.s()
            result = x(body)
            self.assertEqual(result.get(), sum(i + i for i in range(10)))
        finally:
            self.app.conf.CELERY_ALWAYS_EAGER = False

    def test_apply(self):
        self.app.conf.CELERY_ALWAYS_EAGER = False
        from celery import chord

        m = Mock()
        m.app.conf.CELERY_ALWAYS_EAGER = False
        m.AsyncResult = AsyncResult
        prev, chord._type = chord._type, m
        try:
            x = chord(self.add.s(i, i) for i in range(10))
            body = self.add.s(2)
            result = x(body)
            self.assertTrue(result.id)
            # does not modify original subtask
            with self.assertRaises(KeyError):
                body.options['task_id']
            self.assertTrue(chord._type.called)
        finally:
            chord._type = prev


class test_Chord_task(ChordCase):

    def test_run(self):
        prev, self.app.backend = self.app.backend, Mock()
        self.app.backend.cleanup = Mock()
        self.app.backend.cleanup.__name__ = 'cleanup'
        try:
            Chord = self.app.tasks['celery.chord']

            body = dict()
            Chord(group(self.add.subtask((i, i)) for i in range(5)), body)
            Chord([self.add.subtask((j, j)) for j in range(5)], body)
            self.assertEqual(self.app.backend.on_chord_apply.call_count, 2)
        finally:
            self.app.backend = prev
