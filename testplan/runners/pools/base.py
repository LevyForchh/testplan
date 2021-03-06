"""Worker pool executor base classes."""

import abc
import inspect
import logging
import numbers
import os
import psutil

import threading
import time

from schema import Or, And
import six

from testplan.common.config import ConfigOption, validate_func
from testplan.common import entity
from testplan.common.utils.thread import interruptible_join
from testplan.common.utils.exceptions import format_trace
from testplan.common.utils.strings import Color
from testplan.common.utils.timing import wait_until_predicate
from testplan.common.utils import logger

from .communication import Message
from testplan.runners.base import Executor, ExecutorConfig
from .tasks import Task, TaskResult


class Transport(logger.Loggable):
    """
    Transport layer for communication between a pool and a worker.
    Worker send messages, pool receives and send back responses.

    :param recv_sleep: Sleep duration in msg receive loop.
    :type recv_sleep: ``float``
    """

    def __init__(self, recv_sleep=0.05):
        super(Transport, self).__init__()
        self._recv_sleep = recv_sleep
        self.requests = []
        self.responses = []
        self.active = True

    def send(self, message):
        """
        Worker sends a message.

        :param message: Message to be sent.
        :type message: :py:class:`~testplan.runners.pools.communication.Message`
        """
        self.requests.append(message)

    def receive(self):
        """
        Worker receives the response to the message sent.

        :return: Response to the message sent.
        :type: :py:class:`~testplan.runners.pools.communication.Message`
        """
        while self.active:
            try:
                return self.responses.pop()
            except IndexError:
                time.sleep(self._recv_sleep)

    def accept(self):
        """
        Pool receives message sent by worker.

        :return: Message pool received.
        :type: :py:class:`~testplan.runners.pools.communication.Message`
        """
        return self.requests.pop()

    def respond(self, message):
        """
        Used by :py:class:`~testplan.runners.pools.base.Pool` to respond to
        worker request.

        :param message: Respond message.
        :type message: :py:class:`~testplan.runners.pools.communication.Message`
        """
        self.responses.append(message)

    def send_and_receive(self, message, expect=None):
        """
        Send and receive shortcut. optionally assert that the response is
        of the type expected. I.e For a TaskSending message, an Ack is expected.

        :param message: Message sent.
        :type message: :py:class:`~testplan.runners.pools.communication.Message`
        :param expect: Assert message received command is the expected.
        :type expect: ``NoneType`` or
            :py:class:`~testplan.runners.pools.communication.Message`
        :return: Message received.
        :rtype: ``object``
        """
        if not self.active:
            return None
        try:
            self.send(message)
        except Exception as exc:
            self.logger.exception('Hit exception on transport send.')
            raise RuntimeError('On transport send - {}.'.format(exc))

        try:
            received = self.receive()
        except Exception as exc:
            self.logger.exception('Hit exception on transport receive.')
            raise RuntimeError('On transport receive - {}.'.format(exc))

        if self.active and expect is not None:
            if received is None:
                raise RuntimeError('Received None when {} was expected.'.format(
                    expect))
            assert received.cmd == expect
        return received


@six.add_metaclass(abc.ABCMeta)
class ConnectionManager(entity.Resource):
    """
    Abstract base class for classes that manage connections between Pools and
    workers.
    """

    def __init__(self):
        super(ConnectionManager, self).__init__()
        self._workers = []

    @property
    def workers(self):
        """Returns workers this object manages connections for."""
        return self._workers

    def starting(self):
        """
        Perform any connection setup - in the base class no setup is required.
        """
        self.status.change(self.status.STARTED)

    def stopping(self):
        """Unregister workers when stopping."""
        self._unregister_workers()
        self.status.change(self.status.STOPPED)

    def aborting(self):
        """Abort policy - no abort actions are required in the base class."""

    def register(self, worker):
        """
        Register a new worker. Workers should be registered after the
        connection manager is started and will be automatically unregistered
        when it is stopped.
        """
        if self.status.tag != self.status.STARTED:
            raise RuntimeError(
                'Can only register workers when started. Current state is {}'
                .format(self.status.tag))

        if worker in self._workers:
            raise RuntimeError('Worker {} already in ConnectionManager'
                               .format(worker))
        self._workers.append(worker)

    @abc.abstractmethod
    def accept(self):
        """
        Accepts a new message from worker. This method should not block - if
        no message is queued for receiving it should return None.

        :return: Message received from worker transport, or None.
        :rtype: ``NoneType`` or
            :py:class:`~testplan.runners.pools.communication.Message`
        """
        raise NotImplementedError

    def _unregister_workers(self):
        """Remove workers from this connection manager."""
        self._workers = []


class RoundRobinConnManager(ConnectionManager):
    """Manages workers and performs round robin communication with each."""

    def __init__(self):
        super(RoundRobinConnManager, self).__init__()
        self._current = 1

    def accept(self):
        """
        Accepts a new message from the next worker, increments the current
        worker index. Doesn't block if no message is queued for receiving.

        :return: Message received from worker transport, or None.
        :rtype: ``NoneType`` or
            :py:class:`~testplan.runners.pools.communication.Message`
        """
        if not self._workers:
            return None
        msg = None
        try:
            idx = (self._current % len(self._workers)) - 1
            msg = self._workers[idx].transport.accept()
        except IndexError:
            pass
        finally:
            self._current += 1
            return msg


class WorkerConfig(entity.ResourceConfig):
    """
    Configuration object for
    :py:class:`~testplan.runners.pools.base.Worker` resource entity.

    :param index: Worker index id.
    :type index: ``int`` or ``str``
    :param transport: Transport communication class definition.
    :type transport: :py:class:`~testplan.runners.pools.base.Transport`

    Also inherits all :py:class:`~testplan.common.entity.base.ResourceConfig`
    options.
    """

    @classmethod
    def get_options(cls):
        """
        Schema for options validation and assignment of default values.
        """
        return {
            'index': Or(int, str),
            ConfigOption('transport', default=Transport): object,
        }


class Worker(entity.Resource):
    """
    Worker resource that pulls tasks from the transport provided, executes them
    and sends back task results.
    """

    CONFIG = WorkerConfig

    def __init__(self, **options):
        super(Worker, self).__init__(**options)
        self._metadata = None
        self._transport = self.cfg.transport()
        self._loop_handler = None
        self.last_heartbeat = None
        self.assigned = set()
        self.requesting = 0

    @property
    def transport(self):
        """Worker communication transport."""
        return self._transport

    @property
    def metadata(self):
        """Worker metadata information."""
        if not self._metadata:
            self._metadata = {'thread': threading.current_thread(),
                              'index': self.cfg.index}
        return self._metadata

    @property
    def outfile(self):
        """Stdout file."""
        return os.path.join(self.parent.runpath,
                            '{}_startup'.format(self.cfg.index))

    def uid(self):
        """Worker unique index."""
        return self.cfg.index

    def starting(self):
        """Starts the daemonic worker loop."""
        self.make_runpath_dirs()
        self._loop_handler = threading.Thread(
            target=self._loop, args=(self._transport,))
        self._loop_handler.daemon = True
        self._loop_handler.start()

    def stopping(self):
        """Stops the worker."""
        self._transport.active = False
        if self._loop_handler:
            interruptible_join(self._loop_handler)
        self._loop_handler = None

    def aborting(self):
        """Aborting logic, will not wait running tasks."""
        self._transport.active = False

    @property
    def is_alive(self):
        """Poll the loop handler thread to check it is running as expected."""
        return self._loop_handler.is_alive()

    def _loop(self, transport):
        message = Message(**self.metadata)

        while self.active:
            received = transport.send_and_receive(message.make(
                message.TaskPullRequest, data=1))
            if received is None or received.cmd == Message.Stop:
                break
            elif received.cmd == Message.TaskSending:
                results = []
                for item in received.data:
                    results.append(self.execute(item))
                transport.send_and_receive(message.make(
                    message.TaskResults, data=results), expect=message.Ack)
            elif received.cmd == Message.Ack:
                pass
            time.sleep(self.cfg.active_loop_sleep)

    def execute(self, task):
        """
        Executes a task and return the associated task result.

        :param task: Task that worker pulled for execution.
        :type task: :py:class:`~testplan.runners.pools.tasks.base.Task`
        :return: Task result.
        :rtype: :py:class:`~testplan.runners.pools.tasks.base.TaskResult`
        """
        try:
            target = task.materialize()
            if isinstance(target, entity.Runnable):
                if not target.parent:
                  target.parent = self
                if not target.cfg.parent:
                  target.cfg.parent = self.cfg
                result = target.run()
            elif callable(target):
                result = target()
            else:
                result = target.run()
        except BaseException as exc:
            task_result = TaskResult(
                task=task, result=None, status=False,
                reason=format_trace(inspect.trace(), exc))
        else:
            task_result = TaskResult(task=task, result=result, status=True)
        return task_result

    def respond(self, msg):
        """
        Method that the pull uses to respond with a message to the worker.

        :param msg: Response message.
        :type msg: :py:class:`~testplan.runners.pools.communication.Message`
        """
        self._transport.respond(msg)

    def __repr__(self):
        return '{}[{}]'.format(self.__class__.__name__, self.cfg.index)


def default_check_reschedule(pool, task_result):
    """
    Determines if a task should be rescheduled based on the task result info.
    """
    return False


class PoolConfig(ExecutorConfig):
    """
    Configuration object for
    :py:class:`~testplan.runners.pools.base.Pool` executor resource entity.

    :param name: Pool name.
    :type name: ``str``
    :param size: Pool workers size. Default: 4
    :type size: ``int``
    :param worker_type: Type of worker to be initialized.
    :type worker_type: :py:class:`~testplan.runners.pools.base.Worker`
    :param worker_heartbeat: Worker heartbeat period.
    :type worker_heartbeat: ``int`` or ``float`` or ``NoneType``
    :param heartbeat_init_window: Allowed seconds of missing heartbeats from
      workers.
    :type heartbeat_init_window: ``int``
    :param heartbeats_miss_limit: Worker heartbeat period.
    :type heartbeats_miss_limit: ``int``
    :param task_retries_limit: Maximum times a task can be re-assigned to pool.
    :type task_retries_limit: ``int``
    :param max_active_loop_sleep: Maximum value for delay logic in active sleep.
    :type max_active_loop_sleep: ``int`` or ``float``

    Also inherits all :py:class:`~testplan.runners.base.ExecutorConfig`
    options.
    """

    @classmethod
    def get_options(cls):
        """
        Schema for options validation and assignment of default values.
        """
        return {
            'name': str,
            ConfigOption('size', default=4): And(int, lambda x: x > 0),
            ConfigOption('worker_type', default=Worker): object,
            ConfigOption('worker_heartbeat', default=None):
                Or(int, float, None),
            ConfigOption('heartbeat_init_window', default=1800): int,
            ConfigOption('worker_inactivity_threshold', default=300): int,
            ConfigOption('heartbeats_miss_limit', default=3): int,
            ConfigOption('task_retries_limit', default=3): int,
            ConfigOption('max_active_loop_sleep', default=5): numbers.Number}


class Pool(Executor):
    """
    Pool task executor object that initializes workers and dispatches tasks.
    """

    CONFIG = PoolConfig
    CONN_MANAGER = RoundRobinConnManager

    def __init__(self, **options):
        super(Pool, self).__init__(**options)
        self.unassigned = []  # unassigned tasks
        self.task_assign_cnt = {}  # uid: times_assigned
        self.should_reschedule = default_check_reschedule
        self._workers = entity.Environment(parent=self)
        self._workers_last_result = {}
        self._conn = self.CONN_MANAGER()
        self._conn.parent = self
        self._pool_lock = threading.Lock()
        self._metadata = {}
        self.make_runpath_dirs()
        self._metadata['runpath'] = self.runpath
        self._add_workers()

        # Methods for handling different Message types. These are expected to
        # take the worker, request and response objects as the only required
        # positional args.
        self._request_handlers = {
            Message.ConfigRequest: self._handle_cfg_request,
            Message.TaskPullRequest: self._handle_taskpull_request,
            Message.TaskResults: self._handle_taskresults,
            Message.Heartbeat: self._handle_heartbeat,
            Message.SetupFailed: self._handle_setupfailed}

    def uid(self):
        """Pool name."""
        return self.cfg.name

    def add(self, task, uid):
        """
        Add a task for execution.

        :param task: Task to be scheduled to workers.
        :type task: :py:class:`~testplan.runners.pools.tasks.base.Task`
        :param uid: Task uid.
        :type uid: ``str``
        """
        if not isinstance(task, Task):
            raise ValueError('Task was expected, got {} instead.'.format(
                type(task)))
        super(Pool, self).add(task, uid)
        self.unassigned.append(uid)

    def set_reschedule_check(self, check_reschedule):
        """
        Sets callable with custom rules to determine if a task should be
        rescheduled. It must accept the pool object and the task result,
        and based on these it returns if the task should be rescheduled
        (i.e due to a known rare system error).

        :param check_reschedule: Custom callable for task reschedule.
        :type check_reschedule: ``callable`` that takes
          ``pool``, ``task_result`` arguments.
        :return: True if Task should be rescheduled else False.
        :rtype: ``bool``
        """
        validate_func('pool', 'task_result')(check_reschedule)
        self.should_reschedule = check_reschedule

    def _loop(self):
        """
        Main executor work loop - runs in a seperate thread when the Pool is
        started.
        """
        # No heartbeat means no fault tolerance for worker.
        if self.cfg.worker_heartbeat:
            self.logger.debug('Starting worker monitor thread.')
            worker_monitor = threading.Thread(target=self._workers_monitoring)
            worker_monitor.daemon = True
            worker_monitor.start()

        while self.active:
            with self._pool_lock:
                should_continue = self._loop_process_work(self.status.tag)

            if not should_continue:
                break
            else:
                time.sleep(self.cfg.active_loop_sleep)

    def _loop_process_work(self, curr_status):
        """
        Poll for work based on the current pool state and process the next item
        if there is one.

        :return: Whether to continue the main work loop.
        :rtype: ``bool``
        """
        if curr_status == self.status.STARTING:
            self.status.change(self.status.STARTED)
        elif curr_status == self.status.STOPPING:
            self.status.change(self.status.STOPPED)
            return False  # Indicate to break from the main work loop.
        elif curr_status != self.status.STARTED:
            raise RuntimeError('Pool in unexpected state {}'
                               .format(curr_status))
        else:
            msg = self._conn.accept()
            if msg:
                try:
                    self.logger.debug('Received message from worker: %s.',
                                      msg)
                    self.handle_request(msg)
                except Exception as exc:
                    self.logger.error(format_trace(inspect.trace(), exc))

        # The main work loop can continue.
        return True


    def handle_request(self, request):
        """
        Handles a worker request. I.e TaskPull, TaskResults, Heartbeat etc.

        :param request: Worker request.
        :type request: :py:class:`~testplan.runners.pools.communication.Message`
        """
        sender_index = request.sender_metadata['index']
        worker = self._workers[sender_index]
        if not worker.active:
            self.logger.critical(
                'Ignoring message {} - {} from inactive worker {}'.format(
                    request.cmd, request.data, worker))
            # TODO check whether should we respond.
            worker.respond(Message(**self._metadata).make(Message.Ack))
            return
        else:
            worker.last_heartbeat = time.time()

        self.logger.debug('Pool {} request received by {} - {}, {}'.format(
            self.cfg.name, worker, request.cmd, request.data))

        response = Message(**self._metadata)

        if not self.active or self.status.tag == self.STATUS.STOPPING:
            worker.respond(response.make(Message.Stop))
        elif request.cmd in self._request_handlers:
            self._request_handlers[request.cmd](worker, request, response)
        else:
            self.logger.error('Unknown request: {} {} {} {}'.format(
                request, dir(request), request.cmd, request.data))
            worker.respond(response.make(Message.Ack))

    def _handle_cfg_request(self, worker, _, response):
        """Handle a ConfigRequest from a worker."""
        options = []
        cfg = self.cfg

        while cfg:
            try:
                options.append(cfg.denormalize())
            except Exception as exc:
                self.logger.error('Could not denormalize: {} - {}'.format(
                    cfg, exc))
            cfg = cfg.parent

        worker.respond(response.make(Message.ConfigSending,
                                     data=options))

    def _handle_taskpull_request(self, worker, request, response):
        """Handle a TaskPullRequest from a worker."""
        tasks = []

        if self.status.tag == self.status.STARTED:
            for _ in range(request.data):
                try:
                    uid = self.unassigned.pop(0)
                except IndexError:
                    break
                if uid not in self.task_assign_cnt:
                    self.task_assign_cnt[uid] = 0
                if self.task_assign_cnt[uid] >= self.cfg.task_retries_limit:
                    self._discard_task(
                        uid, '{} already reached max retries: {}'.format(
                            self._input[uid], self.cfg.task_retries_limit))
                    continue
                else:
                    self.task_assign_cnt[uid] += 1
                    task = self._input[uid]
                    self.logger.test_info(
                        'Scheduling {} to {}'.format(task, worker))
                    worker.assigned.add(uid)
                    tasks.append(task)
            if tasks:
                worker.respond(response.make(
                    Message.TaskSending, data=tasks))
                worker.requesting = request.data - len(tasks)
                return

        worker.requesting = request.data
        worker.respond(response.make(Message.Ack))

    def _handle_taskresults(self, worker, request, response):
        """Handle a TaskResults message from a worker."""
        for task_result in request.data:
            uid = task_result.task.uid()
            worker.assigned.remove(uid)
            if worker not in self._workers_last_result:
                self._workers_last_result[worker] = time.time()
            self.logger.test_info('De-assign {} from {}'.format(
                task_result.task, worker))

            if self.should_reschedule(self, task_result):
                if self.task_assign_cnt[uid] >= self.cfg.task_retries_limit:
                    self.logger.test_info(
                        'Will not reschedule %(input)s again as it '
                        'reached max retries %(retries)d',
                        {'input': self._input[uid],
                         'retries': self.cfg.task_retries_limit})
                else:
                    self.logger.test_info(
                        'Rescheduling {} due to '
                        'should_reschedule() cfg option of {}'.format(
                            task_result.task, self))
                    self.unassigned.append(uid)
                    continue

            self._print_test_result(task_result)
            self._results[uid] = task_result
            self.ongoing.remove(uid)

        worker.respond(response.make(Message.Ack))

    def _handle_heartbeat(self, worker, request, response):
        """Handle a Heartbeat message received from a worker."""
        worker.last_heartbeat = time.time()
        self.logger.debug(
            'Received heartbeat from {} at {} after {}s.'.format(
                worker, request.data, time.time() - request.data))
        worker.respond(response.make(Message.Ack,
                                     data=worker.last_heartbeat))

    def _handle_setupfailed(self, worker, request, response):
        """Handle a SetupFailed message received from a worker."""
        self.logger.test_info('Worker {} setup failed:{}{}'.format(
            worker, os.linesep, request.data))
        worker.respond(response.make(Message.Ack))
        self._deco_worker(
            worker, 'Aborting {}, setup failed.')

    def _deco_worker(self, worker, message):
        """Decomission a worker."""
        self.logger.critical(message.format(worker))
        if os.path.exists(worker.outfile):
            self.logger.critical('\tlogfile: {}'.format(worker.outfile))
        while worker.assigned:
            uid = worker.assigned.pop()
            self.logger.test_info(
                'Re-assigning {} from {} to {}.'.format(
                    self._input[uid], worker, self))
            self.unassigned.append(uid)
        worker.abort()

    def _workers_handler_monitoring(self, worker, workers_last_killed={}):
        inactivity_threshold = self.cfg.worker_inactivity_threshold

        if worker not in workers_last_killed:
            workers_last_killed[worker] = time.time()

        worker_last_killed = workers_last_killed[worker]
        if not worker.assigned or\
            time.time() - worker_last_killed < inactivity_threshold:
            return

        try:
            proc = psutil.Process(worker.handler.pid)
            children = list(proc.children(recursive=True))
            worker_last_result = self._workers_last_result.get(worker, 0)
            if all(item.status() == 'zombie' for item in children) and\
                    time.time() - worker_last_result > inactivity_threshold:
                workers_last_killed[worker] = time.time()
                try:
                    while worker.assigned:
                        uid = worker.assigned.pop()
                        self.logger.test_info(
                            'Re-assigning {} from {} to {}.'.format(
                                self._input[uid], worker, self))
                        self.unassigned.append(uid)
                    self.logger.test_info(
                        'Restarting worker: {}'.format(worker))
                    worker.stop()
                    worker.start()
                except Exception as exc:
                    self.logger.critical(
                        'Worker {} failed to restart: {}'.format(worker, exc))
                    self._deco_worker(
                        worker, 'Aborting {}, due to defunct child process.')
        except psutil.NoSuchProcess:
            pass

    def _workers_monitoring(self):
        """
        Monitor the health of workers in a loop. Executes in a separate thread.
        """
        if not self.cfg.worker_heartbeat:
            raise RuntimeError(
                'Cannot monitor workers with no heartbeat configured.')

        monitor_started = time.time()
        loop_sleep = self.cfg.worker_heartbeat * self.cfg.heartbeats_miss_limit

        while self._loop_handler.is_alive():
            w_total = set()
            w_uninitialized = set()
            w_active = set()
            w_inactive = set()

            monitor_alive = time.time() - monitor_started
            init_window = monitor_alive <= self.cfg.heartbeat_init_window
            with self._pool_lock:
                for worker in self._workers:
                    if getattr(worker, 'handler', None):
                        self._workers_handler_monitoring(worker)
                    w_total.add(worker)
                    if not worker.active:
                        w_inactive.add(worker)
                    elif worker.last_heartbeat is None:
                        w_uninitialized.add(worker)
                        if not init_window:
                            self._deco_worker(
                                worker, 'Aborting {}, could not initialize.')
                    elif time.time() - worker.last_heartbeat > loop_sleep:
                        w_inactive.add(worker)
                        self._deco_worker(
                            worker, 'Aborting {}, failed to send heartbeats.')
                    else:
                        w_active.add(worker)

                if w_total:
                    if len(w_inactive) == len(w_total):
                        self.logger.critical(
                            'All workers of {} are inactive.'.format(self))
                        self.abort()
                        break

            try:
                # For early finish of worker monitoring thread.
                wait_until_predicate(lambda: not self._loop_handler.is_alive(),
                                     timeout=loop_sleep, interval=0.05)
            except RuntimeError:
                break

    def _discard_task(self, uid, reason):
        self.logger.critical('Discard task {} of {} - {}.'.format(
            self._input[uid], self, reason))
        self._results[uid] = TaskResult(
            task=self._input[uid], status=False,
            reason='Task discarded by {} - {}.'.format(self, reason))
        self.ongoing.remove(uid)

    def _discard_pending_tasks(self):
        self.logger.critical('Discard pending tasks of {}.'.format(self))
        while self.ongoing:
            uid = self.ongoing[0]
            self._results[uid] = TaskResult(
                task=self._input[uid], status=False,
                reason='Task [{}] discarding due to {} abort.'.format(
                    self._input[uid]._target, self))
            self.ongoing.pop(0)

    def _print_test_result(self, task_result):
        if (not isinstance(task_result.result, entity.RunnableResult)) or (
           not hasattr(task_result.result, 'report')):
            return

        # Currently prints report top level result and not details.
        name = task_result.result.report.name
        if task_result.result.report.passed is True:
            self.logger.test_info('{} -> {}'.format(name, Color.green('Pass')))
        else:
            self.logger.test_info('{} -> {}'.format(name, Color.red('Fail')))

    def _add_workers(self):
        """Initialise worker instances."""
        for idx in (str(i) for i in range(self.cfg.size)):
            worker = self.cfg.worker_type(index=idx)
            worker.parent = self
            worker.cfg.parent = self.cfg
            self._workers.add(worker, uid=idx)
            self.logger.debug('Added worker %(index)s (outfile = %(outfile)s)',
                              {'index': idx, 'outfile': worker.outfile})

    def starting(self):
        """Starting the pool and workers."""
        with self._pool_lock:
            self._conn.start()
            for worker in self._workers:
                self._conn.register(worker)
            self._workers.start()

        if self._workers.start_exceptions:
            for msg in self._workers.start_exceptions.values():
                self.logger.error(msg)
            self._workers.stop()
            raise RuntimeError('All workers of {} failed to start.'.format(
            self))

        super(Pool, self).starting()
        self.logger.debug('%s started.', self.__class__.__name__)

    def workers_requests(self):
        """Count how many tasks workers are requesting."""
        return sum(worker.requesting for worker in self._workers)

    def stopping(self):
        """Stop connections and workers."""
        # Stop workers before stopping the connection manager.
        with self._pool_lock:
            self._workers.stop()
            self._conn.stop()
        super(Pool, self).stopping()
        self.logger.debug('Stopped %s', self.__class__.__name__)

    def abort_dependencies(self):
        """Empty generator to override parent implementation."""
        return
        yield

    def aborting(self):
        """Aborting logic."""
        self.logger.debug('Aborting pool {}'.format(self))
        for worker in self._workers:
            worker.abort()
        self._conn.abort()
        self._discard_pending_tasks()
        self.logger.debug('Aborted pool {}'.format(self))

