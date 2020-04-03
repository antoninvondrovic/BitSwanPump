from ..abc import Expression

from ..builder import ExpressionBuilder


class LOOKUP(Expression):
	"""
	Obtains value from "lookup_id" using "key":

		{
			"function": "LOOKUP",
			"lookup_id": "lookup_id",
			"key": <EXPRESSION>
		}
	"""

	def __init__(self, app, expression_class_registry, expression: dict):
		super().__init__(app, expression_class_registry, expression)
		svc = app.get_service("bspump.PumpService")
		self.Lookup = svc.locate_lookup(expression["lookup_id"])
		self.Key = ExpressionBuilder.build(app, expression_class_registry, expression["key"])

	def __call__(self, context, event, *args, **kwargs):
		return self.Lookup.get(self.Key)
