# This file may be a good candidate to be the first one to be refactored into a separate process,
# for the sake of performance and scalability.

import asyncio
import atexit
import contextvars
import inspect
import logging
from collections import deque
from collections.abc import Coroutine, Iterator
from typing import Final

from typing_extensions import TypedDict

from litellm._logging import verbose_logger
from litellm.constants import (
    LOGGING_WORKER_CONCURRENCY,
    LOGGING_WORKER_MAX_QUEUE_SIZE,
    LOGGING_WORKER_MAX_TIME_PER_COROUTINE,
    LOGGING_WORKER_TIMEOUT_SUMMARY_WINDOW_SECONDS,
    MAX_ITERATIONS_TO_CLEAR_QUEUE,
    MAX_TIME_TO_CLEAR_QUEUE,
)

_CALLBACK_DEADLINE: Final[contextvars.ContextVar[float | None]] = contextvars.ContextVar(
    "logging_callback_deadline", default=None
)


def optional_callback_budget(maximum: float, *, fraction: float = 0.25) -> float:
    deadline: Final = _CALLBACK_DEADLINE.get()
    return (
        maximum
        if deadline is None
        else max(0.0, min(maximum, (deadline - asyncio.get_running_loop().time()) * fraction))
    )


def _coroutine_name(coroutine: Coroutine) -> str:
    return getattr(coroutine, "__qualname__", None) or getattr(coroutine, "__name__", None) or type(coroutine).__name__


class LoggingTask(TypedDict):
    """
    A logging task with its associated context to ensure logging is executed in
    the original task's context.
    """

    coroutine: Coroutine
    context: contextvars.Context


class LoggingWorker:
    """
    A simple, async logging worker that processes log coroutines in the background.
    Designed to be best-effort with bounded queues to prevent backpressure.

    Execution concurrency and overflow-pump tasks are bounded. Deferred coroutine
    retention is not hard-bounded: callers requiring a memory ceiling must apply
    admission control or persist serializable events before enqueueing callbacks.

    This leads to a +200 RPS performance improvement when using LiteLLM Python SDK or Proxy Server.
    - Use this to queue coroutine tasks that are not critical to the main flow of the application. e.g Success/Error callbacks, logging, etc.
    """

    def __init__(
        self,
        timeout: float = LOGGING_WORKER_MAX_TIME_PER_COROUTINE,
        max_queue_size: int = LOGGING_WORKER_MAX_QUEUE_SIZE,
        concurrency: int = LOGGING_WORKER_CONCURRENCY,
        timeout_summary_window: float = LOGGING_WORKER_TIMEOUT_SUMMARY_WINDOW_SECONDS,
    ):
        self.timeout = timeout
        self.max_queue_size = max_queue_size
        self.concurrency = concurrency
        self.timeout_summary_window = timeout_summary_window
        self._queue: asyncio.Queue[LoggingTask] | None = None
        self._worker_task: asyncio.Task | None = None
        self._running_tasks: set[asyncio.Task] = set()
        self._dequeued_tasks: dict[int, LoggingTask] = {}  # mutable-ok: refs so flush can rescue never-started tasks
        self._scheduled_tasks = dict[int, asyncio.Task[None]]()
        self._sem: asyncio.Semaphore | None = None
        self._bound_loop: asyncio.AbstractEventLoop | None = None
        self._overflow = deque[LoggingTask]()
        self._overflow_pump: asyncio.Task[None] | None = None
        self._overflow_reported = False
        self._timeout_total: int = 0
        self._timeout_burst_count: int = 0
        self._timeout_last_callback: str | None = None
        self._timeout_summary_task: asyncio.Task | None = None

        # Register cleanup handler to flush remaining events on exit
        atexit.register(self._flush_on_exit)

    @property
    def pending_count(self) -> int:
        return (self._queue.qsize() if self._queue is not None else 0) + len(self._overflow) + len(self._dequeued_tasks)

    @property
    def is_overloaded(self) -> bool:
        return bool(self._overflow) or (self._queue is not None and self._queue.full())

    def _track_dequeued(self, task: LoggingTask) -> None:
        self._dequeued_tasks[id(task)] = task

    def _untrack_dequeued(self, task: LoggingTask) -> None:
        self._dequeued_tasks.pop(id(task), None)
        self._scheduled_tasks.pop(id(task), None)

    def _unstarted_dequeued_tasks(self) -> tuple[LoggingTask, ...]:
        return tuple(
            task
            for task in self._dequeued_tasks.values()
            if inspect.getcoroutinestate(task["coroutine"]) == inspect.CORO_CREATED
        )

    def _run_coroutine_silently(self, loop: asyncio.AbstractEventLoop, task: LoggingTask) -> bool:
        context: Final = task["context"].copy()
        context.run(_CALLBACK_DEADLINE.set, loop.time() + self.timeout)
        try:
            context.run(loop.run_until_complete, asyncio.wait_for(task["coroutine"], timeout=self.timeout))
        except (Exception, asyncio.CancelledError):  # noqa: BLE001  # atexit flush must never break the user's program
            return False
        return True

    def _create_callback_task(self, task: LoggingTask) -> asyncio.Task[object]:
        context: Final = task["context"].copy()
        context.run(_CALLBACK_DEADLINE.set, asyncio.get_running_loop().time() + self.timeout)
        return context.run(asyncio.create_task, task["coroutine"])

    @staticmethod
    def _drain_pending(queue: "asyncio.Queue[LoggingTask]") -> tuple[LoggingTask, ...]:
        """Pop every task still queued, without awaiting them, so they can be moved to another queue."""

        def _pop_until_empty() -> Iterator[LoggingTask]:
            while True:
                try:
                    yield queue.get_nowait()
                except asyncio.QueueEmpty:
                    return

        return tuple(_pop_until_empty())

    def _ensure_queue(self) -> None:
        """Initialize the queue if it doesn't exist or if event loop has changed."""
        try:
            current_loop: Final = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop, can't initialize
            return

        # The queue, semaphore and worker task are all bound to the loop that created them. On a
        # loop change we hand the still-pending tasks to a fresh queue instead of dropping them,
        # so queued spend-logging coroutines are not silently discarded (and never left un-awaited).
        if self._queue is not None and self._bound_loop is not current_loop:
            if self._overflow_pump is not None and not self._overflow_pump.done():
                self._cancel_overflow_pump()
            carried_over: Final = self._drain_pending(self._queue)
            revived: Final = self._unstarted_dequeued_tasks()
            deferred: Final = tuple(self._overflow)
            self._dequeued_tasks.clear()
            self._scheduled_tasks.clear()
            self._overflow = deque((*revived, *deferred))
            self._overflow_pump = None
            new_queue: Final[asyncio.Queue[LoggingTask]] = asyncio.Queue(maxsize=self.max_queue_size)
            for carried_task in carried_over:
                new_queue.put_nowait(carried_task)
            if carried_over or revived or deferred:
                verbose_logger.warning(
                    "LoggingWorker: event loop changed; carried %d queued, %d dequeued and %d deferred logging task(s)",
                    len(carried_over),
                    len(revived),
                    len(deferred),
                )
            else:
                verbose_logger.debug("LoggingWorker: Event loop changed, reinitializing queue and worker")
            self._sem = None
            self._worker_task = None
            self._running_tasks = set()
            self._timeout_summary_task = None
            self._timeout_burst_count = 0
            self._queue = new_queue
            self._bound_loop = current_loop
            return

        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=self.max_queue_size)
            self._bound_loop = current_loop

    def start(self) -> None:
        """Start the logging worker. Idempotent - safe to call multiple times."""
        self._ensure_queue()
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.concurrency)
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker_loop())
        self._start_overflow_pump()

    async def _process_log_task(
        self, task: LoggingTask, sem: asyncio.Semaphore, queue: "asyncio.Queue[LoggingTask]"
    ) -> None:
        """Runs the logging task and handles cleanup. Releases semaphore when done."""
        try:
            if self._queue is not None:
                # Run the coroutine in its original context
                callback_task: Final = self._create_callback_task(task)
                try:
                    await asyncio.wait_for(callback_task, timeout=self.timeout)
                except asyncio.TimeoutError as e:
                    if callback_task.cancelled():
                        self._record_callback_timeout(task["coroutine"])
                    else:
                        verbose_logger.exception("LoggingWorker error: %s", e)
                except Exception as e:
                    verbose_logger.exception("LoggingWorker error: %s", e)
                finally:
                    self._untrack_dequeued(task)
                    queue.task_done()
                    if self._queue is queue and queue.empty() and not self._overflow:
                        self._overflow_reported = False
        finally:
            # Always release semaphore, even if queue is None
            sem.release()

    def _record_callback_timeout(self, coroutine: Coroutine) -> None:
        """Count a callback timeout and arm a debounced summary, so a burst of timeouts
        (e.g. a slow Redis timing out many callbacks at once) logs one bounded line rather
        than a full ERROR stacktrace per callback."""
        self._timeout_total += 1
        self._timeout_burst_count += 1
        self._timeout_last_callback = _coroutine_name(coroutine)
        if self._timeout_summary_task is None or self._timeout_summary_task.done():
            self._timeout_summary_task = asyncio.create_task(self._flush_timeout_summary())

    async def _flush_timeout_summary(self) -> None:
        """After the burst settles, log one bounded summary covering every timeout in it."""
        await asyncio.sleep(self.timeout_summary_window)
        self._emit_timeout_summary()

    def _emit_timeout_summary(self) -> None:
        """Log one bounded summary for the current burst and reset the burst counter."""
        burst_count: Final = self._timeout_burst_count
        self._timeout_burst_count = 0
        if burst_count <= 0:
            return
        verbose_logger.warning(
            "LoggingWorker: %d callback(s) timed out after %ss (callback: %s); %d timed out since start",
            burst_count,
            self.timeout,
            self._timeout_last_callback,
            self._timeout_total,
        )

    async def _worker_loop(self) -> None:
        """Main worker loop that gets tasks and schedules them to run concurrently."""
        queue: Final = self._queue
        sem: Final = self._sem
        running: Final = self._running_tasks
        if queue is None or sem is None:
            return
        try:
            while True:
                # Acquire semaphore before removing task from queue to prevent
                # unbounded growth of waiting tasks
                await sem.acquire()
                try:
                    task = await queue.get()
                    self._track_dequeued(task)
                    # Track each spawned coroutine so we can cancel on shutdown.
                    processing_task = asyncio.create_task(self._process_log_task(task, sem, queue))
                    self._scheduled_tasks[id(task)] = processing_task
                    running.add(processing_task)
                    processing_task.add_done_callback(running.discard)
                except (Exception, asyncio.CancelledError):
                    # If task creation fails, release semaphore to prevent deadlock
                    sem.release()
                    raise

        except asyncio.CancelledError:
            verbose_logger.debug("LoggingWorker cancelled during shutdown")
            processors: Final = tuple(running)
            await asyncio.gather(*processors, return_exceptions=True)
            for pending in reversed(self._unstarted_dequeued_tasks()):
                scheduled: Final = self._scheduled_tasks.get(id(pending))
                if scheduled is not None and scheduled.done():
                    self._untrack_dequeued(pending)
                    queue.task_done()
                    sem.release()
                    self._overflow.appendleft(pending)
            if self._queue is queue:
                await self.clear_queue()

    def enqueue(self, coroutine: Coroutine) -> None:
        """Queue a callback without blocking the request that produced it."""
        if self._queue is None:
            return
        task: Final = LoggingTask(coroutine=coroutine, context=contextvars.copy_context())
        if self._overflow:
            self._defer_task(task)
            return
        try:
            self._queue.put_nowait(task)
        except asyncio.QueueFull:
            self._defer_task(task)

    def _defer_task(self, task: LoggingTask) -> None:
        if not self._overflow_reported:
            verbose_logger.warning("LoggingWorker queue is full; deferring callbacks to its bounded worker")
            self._overflow_reported = True
        self._overflow.append(task)
        self._start_overflow_pump()

    def _start_overflow_pump(self) -> None:
        if not self._overflow or self._queue is None:
            return
        if self._overflow_pump is not None and not self._overflow_pump.done():
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._overflow_pump = asyncio.create_task(self._pump_overflow(self._queue))

    def _cancel_overflow_pump(self) -> None:
        if self._overflow_pump is not None:
            try:
                self._overflow_pump.cancel()
            except RuntimeError:
                pass

    async def _pump_overflow(self, queue: "asyncio.Queue[LoggingTask]") -> None:
        pending: Final = self._overflow
        loop: Final = asyncio.get_running_loop()
        try:
            while pending:
                await queue.put(pending[0])
                pending.popleft()
        finally:
            if self._overflow_pump is asyncio.current_task(loop=loop):
                self._overflow_pump = None

    async def _process_single_task(self, task: LoggingTask) -> None:
        queue: Final = self._queue
        if queue is None:
            return
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.concurrency)
        sem: Final = self._sem
        self._track_dequeued(task)
        try:
            await sem.acquire()
        except asyncio.CancelledError:
            self._untrack_dequeued(task)
            queue.task_done()
            self._overflow.appendleft(task)
            self._start_overflow_pump()
            raise
        try:
            await asyncio.wait_for(self._create_callback_task(task), timeout=self.timeout)
        except Exception:
            pass
        finally:
            self._untrack_dequeued(task)
            queue.task_done()
            sem.release()

    def ensure_initialized_and_enqueue(self, async_coroutine: Coroutine):
        """
        Ensure the logging worker is initialized and enqueue the coroutine.
        """
        self.start()
        self.enqueue(async_coroutine)

    async def stop(self) -> None:
        """Stop the logging worker and clean up resources."""
        if self._timeout_summary_task is not None:
            self._timeout_summary_task.cancel()
            self._timeout_summary_task = None
        self._emit_timeout_summary()

        if self._worker_task is None and not self._running_tasks and self._overflow_pump is None:
            # No worker launched and no in-flight tasks to drain.
            return

        tasks_to_cancel: Final[list[asyncio.Task]] = list(self._running_tasks)
        if self._worker_task:
            # Include the main worker loop so it stops fetching work.
            tasks_to_cancel.append(self._worker_task)
        if self._overflow_pump is not None:
            tasks_to_cancel.append(self._overflow_pump)

        for task in tasks_to_cancel:
            # Propagate cancellation to every pending task.
            task.cancel()

        # Wait for cancellation to settle; ignore errors raised during shutdown.
        await asyncio.gather(*tasks_to_cancel, return_exceptions=True)

        self._worker_task = None
        # Drop references to completed tasks so we can restart cleanly.
        self._running_tasks.clear()
        self._overflow_pump = None

    async def flush(self) -> None:
        """Flush the logging queue.

        Waits until every enqueued task has completed. ``queue.join()`` blocks
        on the queue's unfinished-task counter (decremented by ``task_done()``),
        so it correctly handles items that have been dequeued but whose
        callback hasn't finished yet — ``queue.empty()`` would return True in
        that window and cause us to skip the wait.

        ``start()`` runs first so a queue left behind by a previous event loop
        is carried onto this one and drained here instead of joined forever.
        """
        if self._queue is None:
            return
        self.start()
        queue: Final = self._queue
        while True:
            self._start_overflow_pump()
            pump: Final = self._overflow_pump
            if pump is not None:
                await asyncio.shield(pump)
            await queue.join()
            if not self._overflow:
                return

    async def clear_queue(self):
        """
        Clear the queue with a maximum time limit.
        """
        queue: Final = self._queue
        if queue is None:
            return

        start_time: Final = asyncio.get_event_loop().time()

        for _ in range(MAX_ITERATIONS_TO_CLEAR_QUEUE):
            # Check if we've exceeded the maximum time
            if asyncio.get_event_loop().time() - start_time >= MAX_TIME_TO_CLEAR_QUEUE:
                verbose_logger.warning("clear_queue exceeded max_time of %ss, stopping early", MAX_TIME_TO_CLEAR_QUEUE)
                break

            try:
                task = queue.get_nowait()
                # Await the coroutine to properly execute and avoid "never awaited" warnings
                try:
                    await self._process_single_task(task)
                except Exception:
                    # Suppress errors during cleanup
                    pass
                finally:
                    # Clear reference to prevent memory leaks
                    task = None
            except asyncio.QueueEmpty:
                break

    def _safe_log(self, level: str, message: str) -> None:
        """
        Safely log a message during shutdown, suppressing errors if logging is closed.
        """
        # Check if logger has valid handlers before attempting to log
        # During shutdown, handlers may be closed, causing ValueError when writing
        if not hasattr(verbose_logger, "handlers") or not verbose_logger.handlers:
            return

        # Check if any handler has a valid stream
        has_valid_handler = False
        for handler in verbose_logger.handlers:
            try:
                if hasattr(handler, "stream") and handler.stream and not handler.stream.closed:
                    has_valid_handler = True
                    break
                elif not hasattr(handler, "stream"):
                    # Non-stream handlers (like NullHandler) are always valid
                    has_valid_handler = True
                    break
            except (AttributeError, ValueError):
                continue

        if not has_valid_handler:
            return

        try:
            if level == "debug":
                verbose_logger.debug(message)
            elif level == "info":
                verbose_logger.info(message)
            elif level == "warning":
                verbose_logger.warning(message)
            elif level == "error":
                verbose_logger.error(message)
        except (ValueError, OSError, AttributeError):
            # Logging handlers may be closed during shutdown
            # Silently ignore logging errors to prevent breaking shutdown
            pass

    def _flush_on_exit(self):
        """
        Flush remaining events synchronously before process exit.
        Called automatically via atexit handler.

        This ensures callbacks queued by async completions are processed
        even when the script exits before the worker loop can handle them.

        Note: All logging in this method is wrapped to handle cases where
        logging handlers are closed during shutdown.
        """
        if self._queue is None:
            self._safe_log("debug", "[LoggingWorker] atexit: No queue initialized")
            return

        self._cancel_overflow_pump()
        self._overflow_pump = None

        unstarted_dequeued: Final = self._unstarted_dequeued_tasks()
        if self._queue.empty() and not unstarted_dequeued and not self._overflow:
            self._safe_log("debug", "[LoggingWorker] atexit: Queue is empty")
            return

        queue_size: Final = self._queue.qsize() + len(unstarted_dequeued) + len(self._overflow)
        self._safe_log("info", f"[LoggingWorker] atexit: Flushing {queue_size} remaining events...")

        # Create a new event loop since the original is closed
        loop: Final = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            # Process remaining queue items with time limit
            processed = 0
            start_time: Final = loop.time()

            # logging.raiseExceptions is a process-wide global; scope the
            # suppression to just the drain loop, where shutdown callbacks may
            # log to already-closed handler streams, so other threads keep their
            # logging error reporting for as little of the window as possible.
            previous_raise_exceptions: Final = logging.raiseExceptions
            logging.raiseExceptions = False
            try:
                for pending in unstarted_dequeued:
                    if (
                        processed >= MAX_ITERATIONS_TO_CLEAR_QUEUE
                        or loop.time() - start_time >= MAX_TIME_TO_CLEAR_QUEUE
                    ):
                        break
                    if self._run_coroutine_silently(loop, pending):
                        processed += 1
                    self._untrack_dequeued(pending)

                while (not self._queue.empty() or self._overflow) and processed < MAX_ITERATIONS_TO_CLEAR_QUEUE:
                    if loop.time() - start_time >= MAX_TIME_TO_CLEAR_QUEUE:
                        self._safe_log(
                            "warning",
                            f"[LoggingWorker] atexit: Reached time limit ({MAX_TIME_TO_CLEAR_QUEUE}s), stopping flush",
                        )
                        break

                    try:
                        task = self._queue.get_nowait() if not self._queue.empty() else self._overflow.popleft()
                    except asyncio.QueueEmpty:
                        break

                    # Run the coroutine synchronously in new loop
                    # Note: We run the coroutine directly, not via create_task,
                    # since we're in a new event loop context
                    try:
                        if self._run_coroutine_silently(loop, task):
                            processed += 1
                    finally:
                        # Clear reference to prevent memory leaks
                        task = None
            finally:
                logging.raiseExceptions = previous_raise_exceptions

            self._safe_log(
                "info",
                f"[LoggingWorker] atexit: Successfully flushed {processed} events!",
            )

        finally:
            loop.close()


# Global instance for backward compatibility
GLOBAL_LOGGING_WORKER: Final = LoggingWorker()
