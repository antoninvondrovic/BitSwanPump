import abc
import asyncio
import collections
import concurrent
import datetime
import logging

import time

import asab
from .abc.connection import Connection
from .abc.generator import Generator
from .abc.sink import Sink
from .abc.source import Source
from .analyzer import Analyzer
from .exception import ProcessingError

#

L = logging.getLogger(__name__)

#


class Pipeline(abc.ABC, asab.ConfigObject):
	"""
	Description: Pipeline is ...


	Parameters: ---


	:return: xxxx
	"""



	ConfigDefaults = {
		"async_concurency_limit": 1000,  # TODO concurrency
		"reset_profiler": True,
	}

	def __init__(self, app, id=None, config=None):
		_id = id if id is not None else self.__class__.__name__
		super().__init__("pipeline:{}".format(_id), config=config)

		self.Id = _id
		self.App = app
		self.Loop = app.Loop

		self.AsyncFutures = []
		self.AsyncConcurencyLimit = int(self.Config["async_concurency_limit"])
		self.ResetProfiler = self.Config.getboolean("reset_profiler")
		assert (self.AsyncConcurencyLimit > 1)

		# This object serves to identify the throttler, because list cannot be used as a throttler
		self.AsyncFuturesThrottler = object()

		self.Sources = []
		self.Processors = [[]]  # List of lists of processors, the depth is increased by a Generator object

		# Publish-Subscribe for this pipeline
		self.PubSub = asab.PubSub(app)
		self.MetricsService = app.get_service('asab.MetricsService')
		self.MetricsCounter = self.MetricsService.create_counter(
			"bspump.pipeline",
			tags={'pipeline': self.Id},
			init_values={
				'event.in': 0,
				'event.out': 0,
				'event.drop': 0,
				'warning': 0,
				'error': 0,
			}
		)
		self.MetricsEPSCounter = self.create_eps_counter()

		self.MetricsGauge = self.MetricsService.create_gauge(
			"bspump.pipeline.gauge",
			tags={'pipeline': self.Id},
			init_values={
				'warning.ratio': 0.0,
				'error.ratio': 0.0,
			}
		)
		self.MetricsDutyCycle = self.MetricsService.create_duty_cycle(
			self.Loop,
			"bspump.pipeline.dutycycle",
			tags={'pipeline': self.Id},
			init_values={
				'ready': False,
			}
		)
		self.ProfilerCounter = {}

		app.PubSub.subscribe(
			"Metrics.flush!",
			self._on_metrics_flush
		)

		# Pipeline logger
		self.L = PipelineLogger(
			"bspump.pipeline.{}".format(self.Id),
			self.MetricsCounter
		)

		self.LastReadyStateSwitch = self.Loop.time()

		self._error = None  # None if not in error state otherwise there is a tuple (context, event, exc, timestamp)

		self._throttles = set()
		self._ancestral_pipelines = set()

		self._ready = asyncio.Event(loop=app.Loop)
		self._ready.clear()

		# Chillout is used to break a pipeline processing to smaller tasks that allows other event in event loop to be processed
		self._chillout_trigger = 10000
		self._chillout_counter = 0

		self._context = {}

	def time(self):
		"""
		Description: Pipeline is ...


		Parameters: ---


		:return: xxxx
		"""
		return self.App.time()

	def get_throttles(self):
		"""
		Description: Pipeline is ...


		Parameters: ---


		:return: xxxx
		"""
		return self._throttles


	def _on_metrics_flush(self, event_type):
		"""
		Description: Pipeline is ...


		Parameters: event_type


		:return: xxxx
		"""
		for field in self.MetricsCounter.Storage["fieldset"]:
			values = field["values"]
			if values["event.in"] == 0:
				self.MetricsGauge.set("warning.ratio", 0.0)
				self.MetricsGauge.set("error.ratio", 0.0)
				continue
			self.MetricsGauge.set("warning.ratio", values["warning"] / values["event.in"])
			self.MetricsGauge.set("error.ratio", values["error"] / values["event.in"])

	def is_error(self):
		"""
		Description: Pipeline is ...


		Parameters: ---


		:return: xxxx
		"""
		return self._error is not None

	def set_error(self, context, event, exc):
		"""
		Description: If called with `exc is None`, then reset error (aka recovery)


		Parameters: context event exc


		:return: xxxx
		"""
		if exc is None:
			# Reset branch
			if self._error is not None:
				self._error = None
				L.info("Error cleared at a pipeline '{}'".format(self.Id))

				for source in self.Sources:
					source.restart(self.Loop)

				self.PubSub.publish("bspump.pipeline.clear_error!", pipeline=self)
				self._evaluate_ready()

		else:
			if self.handle_error(exc, context, event):

				self.MetricsEPSCounter.add('warning', 1)
				self.MetricsCounter.add('warning', 1)
				self.PubSub.publish("bspump.pipeline.warning!", pipeline=self)
				return
			else:
				self.MetricsEPSCounter.add('error', 1)
				self.MetricsCounter.add('error', 1)

			if (self._error is not None):
				L.warning("Error on a pipeline is already set!")

			self._error = (context, event, exc, self.App.time())
			L.exception("Pipeline '{}' stopped due to a processing error: {} ({})".format(self.Id, exc, type(exc)))

			self.PubSub.publish("bspump.pipeline.error!", pipeline=self)
			self._evaluate_ready()

	def handle_error(self, exception, context, event):
		"""
		Description: Override to evaluate on the :meth:`Pipeline <bspump.Pipeline()>` processing error.
		Return False for hard errors (stop the :meth:`Pipeline <bspump.Pipeline()>` processing) or True for soft errors that will be ignored


		Parameters: exception, context, event


		:return:


		Example:

		.. code:: python

			class SampleInternalPipeline(bspump.Pipeline):

				def __init__(self, app, pipeline_id):
					super().__init__(app, pipeline_id)

					self.build(
						bspump.common.InternalSource(app, self),
						bspump.common.JSONParserProcessor(app, self),
						bspump.common.PPrintSink(app, self)
					)

				def handle_error(self, exception, context, event):
					if isinstance(exception, json.decoder.JSONDecodeError):
						return True
					return False
		"""

		return False

	def link(self, ancestral_pipeline):
		"""
		Description: Link this :meth:`Pipeline <bspump.Pipeline()>` with an ancestral :meth:`Pipeline <bspump.Pipeline()>`.
		This is needed e. g. for a propagation of the throttling from child :meth:`Pipelines <bspump.Pipeline()>` back to their ancestors.
		If the child :meth:`Pipeline <bspump.Pipeline()>` uses InternalSource, which may become throttled because the internal queue is full,
		the throttling is propagated to the ancestral :meth:`Pipeline <bspump.Pipeline()>`, so that its source may block incoming events until the
		internal queue is empty again.


		Parameters: :param ancestral_pipeline: pipeline


		:return: xxxx
		"""

		self._ancestral_pipelines.add(ancestral_pipeline)

	def unlink(self, ancestral_pipeline):
		"""
		Description: Unlink an ancestral pipeline from this :meth:`Pipeline <bspump.Pipeline()>`.


		Parameters: ancestral_pipeline: pipeline


		:return: xxxx
		"""

		self._ancestral_pipelines.remove(ancestral_pipeline)

	def throttle(self, who, enable=True):
		"""
		Description:

		:return:
		"""
		# L.debug("Pipeline '{}' throttle {} by {}".format(self.Id, "enabled" if enable else "disabled", who))
		if enable:
			self._throttles.add(who)
		else:
			if who in self._throttles:
				try:
					self._throttles.remove(who)
				except KeyError:
					raise KeyError("'{}' not present among throttles".format(who))

		# Throttle primary pipelines, if there are any
		for ancestral_pipeline in self._ancestral_pipelines:
			ancestral_pipeline.throttle(who=who, enable=enable)

		self._evaluate_ready()

	def _evaluate_ready(self):
		"""
		Description:

		:return:
		"""
		orig_ready = self.is_ready()

		# Do we observed an error?
		new_ready = self._error is None

		# Are we throttled?
		if new_ready:
			new_ready = len(self._throttles) == 0

		if orig_ready != new_ready:
			if new_ready:
				self._ready.set()
				self.PubSub.publish("bspump.pipeline.ready!", pipeline=self)
				self.MetricsDutyCycle.set('ready', True)
			else:
				self._ready.clear()
				self.PubSub.publish("bspump.pipeline.not_ready!", pipeline=self)
				self.MetricsDutyCycle.set('ready', False)

	async def ready(self):
		"""
		Description: Can be used in source: `await self.Pipeline.ready()`

		Parameters:

		:return:
		"""

		self._chillout_counter += 1
		if self._chillout_counter >= self._chillout_trigger:
			self._chillout_counter = 0
			await asyncio.sleep(0, loop=self.Loop)

		await self._ready.wait()
		return True

	def is_ready(self):
		"""
		Description:

		:return:
		"""
		return self._ready.is_set()

	def _do_process(self, event, depth, context):
		"""
		Description:

		:return:
		"""
		for processor in self.Processors[depth]:

			t0 = time.perf_counter()
			try:
				event = processor.process(context, event)

			except BaseException as e:
				if depth > 0:
					raise  # Handle error on the top depth
				self.set_error(context, event, e)
				event = None  # Event is discarted

			finally:
				self.ProfilerCounter[processor.Id].add('duration', time.perf_counter() - t0)
				self.ProfilerCounter[processor.Id].add('run', 1)

			if event is None:  # Event has been consumed on the way
				if len(self.Processors) == (depth + 1):
					if isinstance(processor, Sink):

						self.MetricsEPSCounter.add('eps.out', 1)
						self.MetricsCounter.add('event.out', 1)
					else:
						self.MetricsEPSCounter.add('eps.drop', 1)
						self.MetricsCounter.add('event.drop', 1)
				return

		assert (event is not None)

		self.set_error(
			context,
			event,
			ProcessingError("Incomplete pipeline, event '{}' is not consumed by a Sink".format(event))
		)

	def inject(self, context, event, depth):
		"""
		Description: Inject method serves to inject events into the :meth:`Pipeline <bspump.Pipeline()>`'s depth defined by the depth attribute.
		Every depth is interconnected with a generator object.

		#For normal operations, it is highly recommended to use process method instead (see below).

		:return:
		"""

		if context is None:
			context = self._context.copy()
		else:
			context = context.copy()
			context.update(self._context)

		self._do_process(event, depth, context)

	async def process(self, event, context=None):
		"""
		Description: Process method serves to inject events into the :meth:`Pipeline <bspump.Pipeline()>`'s depth 0,
		while incrementing the event.in metric.

		:return:

		:hint: This is recommended way of inserting events into a :meth:`Pipeline <bspump.Pipeline()>`.


		"""

		while not self.is_ready():
			await self.ready()

		self.MetricsEPSCounter.add('eps.in', 1)
		self.MetricsCounter.add('event.in', 1)

		self.inject(context, event, depth=0)



	def create_eps_counter(self):
		"""
		Description:

		:return:
		"""
		return self.MetricsService.create_eps_counter(
			"bspump.pipeline.eps",
			tags={'pipeline': self.Id},
			init_values={
				'eps.in': 0,
				'eps.out': 0,
				'eps.drop': 0,
				'warning': 0,
				'error': 0,
			}
		)

	# Future methods

	def ensure_future(self, coro):
		"""
		Description: You can use this method to schedule a future task that will be executed in a context of the :meth:`Pipeline <bspump.Pipeline()>`.
		The :meth:`Pipeline <bspump.Pipeline()>` also manages a whole lifecycle of the future/task, which means,
		it will collect the future result, trash it, and mainly it will capture any possible exception,
		which will then block the :meth:`Pipeline <bspump.Pipeline()>` via set_error().

		:return:

		:hint: If the number of futures exceeds the configured limit, the :meth:`Pipeline <bspump.Pipeline()>` is throttled.
		"""

		future = asyncio.ensure_future(coro, loop=self.Loop)
		future.add_done_callback(self._future_done)
		self.AsyncFutures.append(future)

		# Throttle when the number of futures exceeds the max count
		if len(self.AsyncFutures) == self.AsyncConcurencyLimit:
			self.throttle(self.AsyncFuturesThrottler, True)


	def _future_done(self, future):
		"""
		Description: Removes future from the future list and disables throttling, if the number of
		futures does not exceed the configured limit.

		:return:

		:hint: If there is an error while processing the future, it it set to the :meth:`Pipeline <bspump.Pipeline()>`.
		"""

		# Remove the throttle
		if len(self.AsyncFutures) == self.AsyncConcurencyLimit:
			self.throttle(self.AsyncFuturesThrottler, False)

		self.AsyncFutures.remove(future)

		exception = future.exception()
		if exception is not None:
			try:
				self.set_error(None, None, exception)
			except Exception:
				# The exception is handled by set_error
				pass

	# Construction

	def set_source(self, source):
		"""
		Description: set_source is a method that sets a selected source that will pump data into the BSPump

		:return:

		"""
		if isinstance(source, Source):
			self.Sources.append(source)
		else:
			self.Sources.extend(source)

	def append_processor(self, processor):
		"""
		Description: adds a :meth:`Processors <bspump.Processor()>` to the :meth:`Pipeline <bspump.Pipeline()>`

		:return:

		"""
		# TODO: Check if possible: self.Processors[*][-1] is Sink, no processors after Sink, ...
		# TODO: Check if fitting
		self.Processors[-1].append(processor)

		if isinstance(processor, Generator):
			processor.set_depth(len(self.Processors) - 1)
			self.Processors.append([])

		self._post_add_processor(processor)

	def remove_processor(self, processor_id):
		"""
		Description: removes the processor from the :meth:`Pipeline <bspump.Pipeline()>`

		:return:
		"""
		for depth in self.Processors:
			for idx, processor in enumerate(depth):
				if processor.Id != processor_id:
					continue
				del depth[idx]
				del self.ProfilerCounter[processor.Id]
				if isinstance(processor, Analyzer):
					del self.ProfilerCounter['analyzer_' + processor.Id]
				return
		raise KeyError("Cannot find processor '{}'".format(processor_id))

	def insert_before(self, id, processor):
		"""
		Description: Insert the :meth:`Processors <bspump.Processor()>` into a :meth:`Pipeline <bspump.Pipeline()>` before another processor specified by id

		return: True on success. False otherwise (id not found)
		"""
		for processors in self.Processors:
			for idx, _processor in enumerate(processors):
				if _processor.Id == id:
					processors.insert(idx, processor)
					self._post_add_processor(processor)
					return True
		return False

	def insert_after(self, id, processor):
		"""
		Description: Insert the :meth:`Processors <bspump.Processor()>` into a :meth:`Pipeline <bspump.Pipeline()>` after another :meth:`Processors <bspump.Processor()>` specified by id

		:return: True on success. False otherwise (id not found)
		"""
		for processors in self.Processors:
			for idx, _processor in enumerate(processors):
				if _processor.Id == id:
					processors.insert(idx + 1, processor)
					self._post_add_processor(processor)
					return True
		return False

	def _post_add_processor(self, processor):
		"""
		Description:

		:return:
		"""
		self.ProfilerCounter[processor.Id] = self.MetricsService.create_counter(
			'bspump.pipeline.profiler',
			tags={
				'processor': processor.Id,
				'pipeline': self.Id,
			},
			init_values={'duration': 0.0, 'run': 0},
			reset=self.ResetProfiler,
		)
		if isinstance(processor, Analyzer):
			self.ProfilerCounter['analyzer_' + processor.Id] = self.MetricsService.create_counter(
				'bspump.pipeline.profiler',
				tags={
					'analyzer': processor.Id,
					'pipeline': self.Id,
				},
				init_values={'duration': 0.0, 'run': 0},
				reset=self.ResetProfiler,
			)

	def build(self, source, *processors):
		"""
		Description: This method enables to add sources, :meth:`Processors <bspump.Processor()>`, and sink to create the structure of the :meth:`Pipeline <bspump.Pipeline()>`.

		:return:
		"""
		self.set_source(source)
		for processor in processors:
			self.append_processor(processor)

	def iter_processors(self):
		"""
		Description: Iterate thru all processors.

		:return:
		"""
		for processors in self.Processors:
			for processor in processors:
				yield processor

	# Locate  ...

	def locate_source(self, address):
		"""
		Description: Find a source by id.

		:return:
		"""
		for source in self.Sources:
			if source.Id == address:
				return source
		return None

	def locate_connection(self, app, connection_id):
		"""
		Description: Find a connection by id.

		:return:
		"""
		if isinstance(connection_id, Connection):
			return connection_id
		svc = app.get_service("bspump.PumpService")
		connection = svc.locate_connection(connection_id)
		if connection is None:
			raise RuntimeError("Cannot locate connection '{}'".format(connection_id))
		return connection

	def locate_processor(self, processor_id):
		"""
		Description: Find by a processor by id.

		:return:
		"""
		for processor in self.iter_processors():
			if processor.Id == processor_id:
				return processor

	# Lifecycle ...

	def start(self):
		"""
		Description: starts the lifecycle of the :meth:`Pipeline <bspump.Pipeline()>`

		:return:
		"""
		self.PubSub.publish("bspump.pipeline.start!", pipeline=self)

		# Start all non-started sources
		for source in self.Sources:
			source.start(self.Loop)

		self._evaluate_ready()

	async def stop(self):
		"""
		Desription: stops the lifecycle of the :meth:`Pipeline <bspump.Pipeline()>`

		:return:
		"""
		self.PubSub.publish("bspump.pipeline.stop!", pipeline=self)

		# Stop all futures
		while len(self.AsyncFutures) > 0:
			# The futures are removed in _future_done
			await asyncio.wait(
				self.AsyncFutures,
				return_when=concurrent.futures.ALL_COMPLETED
			)

		# Stop all started sources
		for source in self.Sources:
			await source.stop()

	# Rest API

	def rest_get(self):
		"""
		Description:

		:return:
		"""
		rest = {
			'Id': self.Id,
			'Ready': self.is_ready(),
			'Throttles': list(self._throttles),
			'Sources': self.Sources,
			'Processors': [],
			'Metrics': self.MetricsService.Storage.Metrics,
			'Log': [record.__dict__ for record in self.L.Deque]
		}

		for processors in self.Processors:
			rest['Processors'].append(processors)

		if self._error:
			error_text = str(self._error[2])  # (context, event, exc, timestamp)[2]
			error_time = self._error[3]
			if len(error_text) == 0:
				error_text = str(type(self._error[2]))
			rest['Error'] = error_text
			rest['ErrorTimestamp'] = error_time

		return rest


###


class PipelineLogger(logging.Logger):
	"""
	Description: PipelineLogger is a feature of BSPump which enables direct monitoring of a specific :meth:`Pipeline <bspump.Pipeline()>`.
	It offers an overview of errors, error handling, data in a given time with its timestamp

	:return:
	"""

	def __init__(self, name, metrics_counter, level=logging.NOTSET):
		super().__init__(name, level=level)
		self.Deque = collections.deque([], 50)
		self._metrics_counter = metrics_counter

	# TODO: configurable maxlen that is now 50 ^^
	# TODO: configurable log level (per pipeline, from its config)

	def handle(self, record):
		"""
		Description: Counts and adds errors to the error counter

		:return:
		"""
		# Count errors and warnings
		if record.levelno == logging.WARNING:
			self._metrics_counter.add("warning", 1)
		elif record.levelno >= logging.ERROR:
			self._metrics_counter.add("error", 1)

		# Add formatted timestamp
		record.timestamp = self._format_time(record)

		# Add record
		self.Deque.append(record)

	def _format_time(self, record):
		"""
		Description:

		:return:
		"""
		try:
			ct = datetime.datetime.fromtimestamp(record.created)
			return ct.isoformat()
		except BaseException as e:
			L.error("ERROR when logging: {}".format(e))
			return str(record.created)
