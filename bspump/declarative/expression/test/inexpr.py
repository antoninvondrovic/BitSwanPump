from ...abc import Expression

from ..value.valueexpr import VALUE
from ..value.eventexpr import EVENT


class IN(Expression):
	"""
	Checks if expression is of given list.
	"""

	Attributes = {
		"What": ["*"],  # TODO: 'What' is different from list, set and dict
		"Where": ["list", "set", "dict"],
	}

	Category = "Compare"


	def __init__(self, app, *, arg_what, arg_where):
		super().__init__(app)

		if isinstance(arg_what, Expression):
			self.What = arg_what
		else:
			self.What = VALUE(app, value=arg_what)

		if isinstance(arg_where, Expression):
			self.Where = arg_where
		else:
			self.Where = VALUE(app, value=arg_where)

		assert(self.Where.get_outlet_type() in ('list', 'set', 'dict'))


	def optimize(self):
		if isinstance(self.Where, VALUE):

			if self.Where.get_outlet_type() == 'list':
				return IN_optimized_list_where(self)

			if self.Where.get_outlet_type() == 'set':
				return IN_optimized_set_where(self)


		if isinstance(self.What, VALUE) and isinstance(self.Where, EVENT):
			return IN_optimized_EVENT_VALUE(self)

		return None


	def __call__(self, context, event, *args, **kwargs):
		return self.What(context, event, *args, **kwargs) in self.Where(context, event, *args, **kwargs)



class IN_optimized_list_where(IN):

	def __init__(self, orig):
		super().__init__(
			orig.App,
			arg_what=orig.What,
			arg_where=orig.Where
		)

		self._where_value = frozenset(self.Where({}, {}))

	def optimize(self):
		# This is to prevent re-optimising the class
		return None


	def __call__(self, context, event, *args, **kwargs):
		return self.What(context, event, *args, **kwargs) in self._where_value


class IN_optimized_set_where(IN):

	def __init__(self, orig):
		super().__init__(
			orig.App,
			arg_what=orig.What,
			arg_where=orig.Where
		)

		self._where_value = self.Where({}, {})


	def optimize(self):
		# This is to prevent re-optimising the class
		return None


	def __call__(self, context, event, *args, **kwargs):
		return self.What(context, event, *args, **kwargs) in self._where_value


class IN_optimized_EVENT_VALUE(IN):

	def __init__(self, orig):
		super().__init__(
			orig.App,
			arg_what=orig.What,
			arg_where=orig.Where
		)

		self._what_value = self.What({}, {})

	def optimize(self):
		# This is to prevent re-optimising the class
		return None

	def __call__(self, context, event, *args, **kwargs):
		return self._what_value in event
