#!/usr/bin/env python3
"""
ticks.py - the server's single timer thread (roadmap 1.9 F8: arch-tick-scheduler)
=================================================================================
One heap-scheduled thread replaces ad-hoc timers and "run it on the next C2S"
hacks (`_tick_respawns` only ever ran when the client happened to send the misrouted
0x38/0x25, removed in P0 stage 3; roadmap S1-07). Every callback runs under the lock
handed to the scheduler (the world lock), so a timer never races a handler that takes
the same lock.

    sched = Scheduler(lock=world_lock)
    sched.start()
    t = sched.call_later(3.0, despawn, mob_uid)          # one-shot (0x06 ~3 s after death)
    r = sched.call_every(15.0, regen_tick)               # periodic (HP/MP regen)
    t.cancel()                                           # or sched.cancel(t)

Planned users (F8 table): monster despawn/respawn, regen 15 s, buff expiry (50 ms
resolution), DoT 990 ms, party vitals coalesce 250 ms, invite TTLs, period cash
item expiry 60 s, idle reaper 30 s, autosave, maintenance countdown.

Rules
-----
- Callbacks run on the scheduler thread, one at a time, holding `lock`. Keep them
  short: no time.sleep, no blocking socket reads.
- An exception in a callback is logged and swallowed; a periodic timer keeps running.
- Periodic timers are fixed-rate (next = previous deadline + period). If the thread
  fell more than one period behind, the timer skips ahead instead of firing a burst.
- run_due(now) runs every due callback synchronously on the caller's thread with an
  explicit clock value. Offline tests use it with a fake clock and never start the
  thread.
"""
import heapq
import itertools
import logging
import threading
import time

log = logging.getLogger('WS')


class Timer:
    """Handle returned by call_at / call_later / call_every."""
    __slots__ = ('when', 'period', 'fn', 'args', 'kwargs', 'name', 'cancelled', 'runs', '_seq')

    def __init__(self, when, period, fn, args, kwargs, name, seq):
        self.when = when
        self.period = period
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.name = name or getattr(fn, '__name__', 'timer')
        self.cancelled = False
        self.runs = 0
        self._seq = seq

    def cancel(self):
        # Lazy deletion: the heap entry stays until it reaches the top and is dropped.
        self.cancelled = True

    @property
    def active(self):
        return not self.cancelled

    def __repr__(self):
        kind = f'every {self.period}s' if self.period else 'once'
        return f'<Timer {self.name} {kind} at {self.when:.3f}{" cancelled" if self.cancelled else ""}>'


class Scheduler:
    def __init__(self, lock=None, clock=time.monotonic, name='ticks', max_wait=1.0):
        """lock: acquired around every callback (default: a private RLock).
        clock: monotonic seconds; tests may inject a fake one.
        max_wait: upper bound on one idle wait, so a clock that jumps is re-read."""
        self.lock = lock if lock is not None else threading.RLock()
        self.clock = clock
        self.name = name
        self.max_wait = max_wait
        self._heap = []
        self._seq = itertools.count()
        self._cv = threading.Condition(threading.Lock())
        self._thread = None
        self._running = False

    # ------------------------------------------------------------ scheduling ---
    def call_at(self, when, fn, *args, name=None, **kwargs):
        """Run fn(*args, **kwargs) once at clock() >= when."""
        return self._push(float(when), 0.0, fn, args, kwargs, name)

    def call_later(self, delay, fn, *args, name=None, **kwargs):
        """Run fn once, `delay` seconds from now."""
        return self._push(self.clock() + max(0.0, float(delay)), 0.0, fn, args, kwargs, name)

    def call_every(self, period, fn, *args, first_delay=None, name=None, **kwargs):
        """Run fn every `period` seconds (first run after first_delay, default one period)."""
        period = float(period)
        if period <= 0:
            raise ValueError(f'period must be > 0, got {period}')
        delay = period if first_delay is None else max(0.0, float(first_delay))
        return self._push(self.clock() + delay, period, fn, args, kwargs, name)

    def cancel(self, timer):
        if timer is not None:
            timer.cancel()

    def _push(self, when, period, fn, args, kwargs, name):
        if not callable(fn):
            raise TypeError(f'{fn!r} is not callable')
        with self._cv:
            t = Timer(when, period, fn, args, kwargs, name, next(self._seq))
            heapq.heappush(self._heap, (t.when, t._seq, t))
            # Wake the thread: this deadline may be earlier than the one it sleeps on.
            self._cv.notify()
        return t

    # --------------------------------------------------------------- queries ---
    def pending(self):
        """Number of live (not cancelled) timers."""
        with self._cv:
            return sum(1 for _, _, t in self._heap if not t.cancelled)

    def next_deadline(self):
        """Earliest live deadline, or None."""
        with self._cv:
            self._drop_cancelled()
            return self._heap[0][0] if self._heap else None

    @property
    def running(self):
        return self._running and self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------- execution ---
    def _drop_cancelled(self):
        while self._heap and self._heap[0][2].cancelled:
            heapq.heappop(self._heap)

    def _pop_due(self, now):
        """Pop the next due live timer (caller holds _cv), or None."""
        self._drop_cancelled()
        if self._heap and self._heap[0][0] <= now:
            return heapq.heappop(self._heap)[2]
        return None

    def _reschedule(self, t, now):
        if t.cancelled or not t.period:
            return
        nxt = t.when + t.period
        if nxt <= now:
            # Fell more than a period behind (long callback, stalled process): skip the
            # missed runs instead of firing them back to back.
            nxt = now + t.period
        with self._cv:
            if t.cancelled:
                return
            t.when = nxt
            t._seq = next(self._seq)
            heapq.heappush(self._heap, (t.when, t._seq, t))
            self._cv.notify()

    def _run(self, t, now):
        try:
            with self.lock:
                if t.cancelled:
                    return
                t.runs += 1
                t.fn(*t.args, **t.kwargs)
        except Exception:                                   # noqa: BLE001 - a timer must never kill the thread
            log.exception(f'[TICKS] callback {t.name} raised')
        finally:
            self._reschedule(t, now)

    def run_due(self, now=None):
        """Run every timer due at `now` (default clock()) on this thread. Returns the
        number of callbacks run. A periodic timer runs at most once per call."""
        now = self.clock() if now is None else now
        ran = 0
        while True:
            with self._cv:
                t = self._pop_due(now)
            if t is None:
                return ran
            ran += 1
            # _reschedule always moves a periodic timer past `now`, so this loop ends.
            self._run(t, now)

    def _loop(self):
        log.info(f'[TICKS] scheduler thread "{self.name}" started')
        while True:
            with self._cv:
                if not self._running:
                    break
                self._drop_cancelled()
                now = self.clock()
                if not self._heap:
                    self._cv.wait(self.max_wait)
                    continue
                delay = self._heap[0][0] - now
                if delay > 0:
                    self._cv.wait(min(delay, self.max_wait))
                    continue
                t = heapq.heappop(self._heap)[2]
            self._run(t, now)
        log.info(f'[TICKS] scheduler thread "{self.name}" stopped')

    def start(self):
        """Start the timer thread (idempotent)."""
        with self._cv:
            if self._thread is not None and self._thread.is_alive():
                return self
            self._running = True
            self._thread = threading.Thread(target=self._loop, name=f'ticks-{self.name}', daemon=True)
            self._thread.start()
        return self

    def stop(self, timeout=2.0):
        """Stop the thread after the callback in progress; pending timers are kept."""
        with self._cv:
            self._running = False
            self._cv.notify_all()
            th = self._thread
        if th is not None and th is not threading.current_thread():
            th.join(timeout)
        self._thread = None
