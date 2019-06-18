from bspump import Processor
import collections
import mongoquery


class LatchProcessor(Processor):
	"""
		Latch accumulates events in the Latch of maximum specified size - `latch_max_size`

		If `latch_max_size` is 0 then latch is not limited

		If accumulated events exceeds `latch_max_size` then first event is dropped.

		The latch can be filled based on the query (empty by default). The query is mongo-like,
		see the rules in `ContentFilter`. If inclusive is True (default), matched with the query event is 
		added to the latch, otherwise skipped.

		The query can be injected with an API call to allow to control events in the latch.

	"""

	ConfigDefaults = {
		'latch_max_size': 50,  # 0 means unlimited size
	}

	def __init__(self, app, pipeline, id=None, config=None):
		super().__init__(app, pipeline, query={}, inclusive=True, id=id, config=config)
		self.Inclusive = inclusive
		max_size = int(self.Config.get('latch_max_size'))
		if max_size == 0:
			self.Latch = collections.deque()
		else:
			self.Latch = collections.deque(maxlen=max_size)

		# Check if the query is correctly implemented		
		try:
			self.Query = mongoquery.Query(query)
			self.Query.match({})
		except mongoquery.QueryError:
			L.warn("Incorrect query")
			raise
		

	def process(self, context, event):
		if self.Query.match(event) == self.Inclusive:
			self.Latch.append(event)
		return event


