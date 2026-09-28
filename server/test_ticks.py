#!/usr/bin/env python3
"""
test_ticks.py - offline tests for ticks.Scheduler (roadmap 1.9 F8, arch-tick-scheduler)

Deterministic tests drive run_due() with a fake clock; two short tests start the real
thread. Nothing binds a port and windslayer_server's background threads never start.
"""
import logging
import os
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import ticks  # noqa: E402


class FakeClock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


class RunDue(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.s = ticks.Scheduler(clock=self.clock)
        self.calls = []

    def test_one_shots_run_in_deadline_order_once(self):
        self.s.call_later(2.0, self.calls.append, 'b')
        self.s.call_later(1.0, self.calls.append, 'a')
        self.s.call_later(1.0, self.calls.append, 'a2')     # same deadline: FIFO
        self.assertEqual(self.s.run_due(), 0)
        self.clock.t += 1.0
        self.assertEqual(self.s.run_due(), 2)
        self.clock.t += 5.0
        self.assertEqual(self.s.run_due(), 1)
        self.assertEqual(self.s.run_due(), 0)
        self.assertEqual(self.calls, ['a', 'a2', 'b'])
        self.assertEqual(self.s.pending(), 0)

    def test_call_at_and_kwargs(self):
        self.s.call_at(105.0, lambda x, y=0: self.calls.append((x, y)), 1, y=2)
        self.s.run_due(104.9)
        self.s.run_due(105.0)
        self.assertEqual(self.calls, [(1, 2)])

    def test_cancel_before_and_from_inside_callback(self):
        t1 = self.s.call_later(1.0, self.calls.append, 'cancelled')
        t1.cancel()
        box = {}

        def once_then_cancel():
            self.calls.append('periodic')
            box['t'].cancel()
        box['t'] = self.s.call_every(1.0, once_then_cancel)
        self.clock.t += 10
        self.s.run_due()
        self.clock.t += 10
        self.s.run_due()
        self.assertEqual(self.calls, ['periodic'])
        self.assertIsNone(self.s.next_deadline())

    def test_periodic_is_fixed_rate_and_skips_missed_runs(self):
        t = self.s.call_every(1.0, self.calls.append, 'tick')
        self.assertEqual(t.when, 101.0)
        self.clock.t = 101.0
        self.s.run_due()
        self.assertEqual(t.when, 102.0)                     # previous deadline + period
        self.clock.t = 102.4
        self.s.run_due()
        self.assertEqual(t.when, 103.0)                     # not 103.4: fixed rate
        self.clock.t = 110.2                                # stalled for 7 periods
        self.assertEqual(self.s.run_due(), 1)               # one run, no burst
        self.assertAlmostEqual(t.when, 111.2)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(t.runs, 3)

    def test_first_delay(self):
        t = self.s.call_every(15.0, self.calls.append, 'regen', first_delay=0)
        self.assertEqual(t.when, 100.0)
        self.s.run_due()
        self.assertEqual(self.calls, ['regen'])
        self.assertEqual(t.when, 115.0)

    def test_exception_is_logged_and_periodic_keeps_running(self):
        def boom():
            self.calls.append('boom')
            raise RuntimeError('callback bug')
        self.s.call_every(1.0, boom, name='boom')
        self.s.call_later(1.0, self.calls.append, 'after')
        self.clock.t += 1
        with self.assertLogs('WS', logging.ERROR) as cm:
            self.s.run_due()
        self.assertIn('[TICKS] callback boom raised', cm.output[0])
        self.clock.t += 1
        with self.assertLogs('WS', logging.ERROR):
            self.s.run_due()
        self.assertEqual(self.calls, ['boom', 'after', 'boom'])

    def test_callbacks_hold_the_world_lock(self):
        lock = threading.RLock()
        s = ticks.Scheduler(lock=lock, clock=self.clock)
        seen = []
        s.call_later(0, lambda: seen.append(lock._is_owned()))
        s.run_due()
        self.assertEqual(seen, [True])

    def test_scheduling_from_a_callback(self):
        self.s.call_later(1.0, lambda: self.s.call_later(1.0, self.calls.append, 'chained'))
        self.clock.t += 1
        self.s.run_due()
        self.assertEqual(self.calls, [])
        self.clock.t += 1
        self.s.run_due()
        self.assertEqual(self.calls, ['chained'])

    def test_bad_arguments(self):
        with self.assertRaises(ValueError):
            self.s.call_every(0, print)
        with self.assertRaises(TypeError):
            self.s.call_later(1, 'not callable')


class Thread(unittest.TestCase):
    def test_thread_runs_timers_in_order_and_stops(self):
        s = ticks.Scheduler(name='test')
        done = threading.Event()
        order = []
        s.start()
        try:
            self.assertTrue(s.running)
            s.call_later(0.10, order.append, 'late')
            s.call_later(0.02, order.append, 'early')       # earlier deadline wakes the sleeper
            s.call_later(0.15, done.set)
            self.assertTrue(done.wait(2.0))
            self.assertEqual(order, ['early', 'late'])
        finally:
            s.stop()
        self.assertFalse(s.running)

    def test_periodic_on_thread(self):
        s = ticks.Scheduler(name='test2')
        count = []
        t = s.call_every(0.02, count.append, 1)
        s.start()
        try:
            end = time.monotonic() + 2.0
            while len(count) < 3 and time.monotonic() < end:
                time.sleep(0.01)
        finally:
            t.cancel()
            s.stop()
        self.assertGreaterEqual(len(count), 3)


class ServerWiring(unittest.TestCase):
    def test_game_server_owns_an_idle_scheduler_until_start(self):
        import tempfile
        import fakeclient
        server = fakeclient.make_server(tempfile.mkdtemp(prefix='ws_ticks_'))
        self.assertIsInstance(server.ticks, ticks.Scheduler)
        self.assertIs(server.ticks.lock, server.world_lock)
        self.assertFalse(server.ticks.running)              # start() starts it; nothing scheduled yet
        self.assertEqual(server.ticks.pending(), 0)


if __name__ == '__main__':
    unittest.main(verbosity=1)
