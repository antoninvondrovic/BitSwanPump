import time
import numpy as np

import logging

from .timewindowanalyzer import TimeWindowAnalyzer
from .timewindowmatrix import TimeWindowMatrix

###

L = logging.getLogger(__name__)

###

ConfigDefaults = {
	#TODO 'resolution': 60,  # Resolution (aka column width) in seconds
	'event_name': '', # User defined value, e.g. server name
	'load': '', # User defined load to matrix, if not specified (left empty), load will be the count of occurences (histogram)
	'lower_bound': 0, # if lower bound > upper bound: alarm is set when value is below lower bound, if lower bound != 0 and upper bound > lower_bound: alarm is set when value is out of bounds
	'upper_bound': 1000,
}

class ThresholdAnalyzer(TimeWindowAnalyzer):
	def __init__(self, app, pipeline, id=None, config=None):
		super().__init__(app, pipeline, matrix_id=None, dtype='float_', columns=15, analyze_on_clock=False,
						 resolution=60, start_time=None, clock_driven=False, id=id, config=config)

		# self._resolution = int(self.Config['resolution'])
		self.Event_name = self.Config['event_name']
		self.Load = self.Config['load']
		self.Lower = self.Config['lower_bound']
		self.Upper = self.Config['upper_bound']

		self.TimeWindow.zeros() #initializing timewindow with zeros


	# check if event contains related fields
	def predicate(self, context, event):
		if self.Event_name not in event:
			return False

		if "@timestamp" not in event:
			return False

		return True


	def evaluate(self, context, event):
		value = str(event[self.Event_name])  # server name e.g.
		time_stamp = event["@timestamp"] # time stamp of the event

		row = self.TimeWindow.get_row_index(value)
		if row is None:
			row = self.TimeWindow.add_row(value)

		# find the column in timewindow matrix to fit in
		column = self.TimeWindow.get_column(time_stamp)
		if column is None:
			return

		# load
		if self.Load == '':
			self.TimeWindow.Array[row, column] += 1
		else:
			self.TimeWindow.Array[row, column] = event[self.Load]


	def analyze(self): #TODO set analyzing method
		print(self.TimeWindow.Array.shape[0])
		if self.TimeWindow.Array.shape[0] == 0: # checking an empty array
			return

		#TODO Check if this below is not a better solution
		# use np function to analyze threshold
		# 	...

		# if not self.Lower <= 0 and self.Lower < self.Upper: # range
		# 	if self.TimeWindow.Array.shape[0] < self.Lower or self.TimeWindow.Array.shape[0] > self.Upper:
		# 		self.alarm()  # call alarm method
		# elif self.Lower > self.Upper: # below the limit
		# 	if self.TimeWindow.Array.shape[0] < self.Lower:
		# 		self.alarm()  # call alarm method
		# elif self.Lower == 0 and self.Upper > self.Lower: # above the limit
		# 	if self.TimeWindow.Array.shape[0] > self.Upper:
		# 		self.alarm() # call alarm method
		# else:
		# 	raise ValueError

		#TODO
		# if value is in AlarmDict - hold/dont start the alarm, else: start the alarm
		# threshold_limiter = len(line v matrixu (row))f
		# as a len_threshold analyzer use np. function?

	def alarm(self):
		self.alarm_val = str('Threshold has been subceeded / exceeded at {} %'.format(str(datetime.datetime.now()
																						  )[:-7].replace(" ", "T")))
		return self.alarm_val

	# def alarm(self, len_array, lower_bound, upper_bound): #TODO set alarm
	# 	lower_bound = lower_bound
	# 	upper_bound = upper_bound
	#
	# 	if not int(len_array):
	# 		raise ValueError
	# 	#
	# 	# if not int(len_threshold):
	# 	# 	raise ValueError
	#
	# 	if level == 'above':
	# 		self.alarm_val = str('Threshold has been exceeded by {} %'.format(
	# 			(abs(len_array-len_threshold) / len_threshold) * 100))
	# 	elif level == 'below':
	# 		self.alarm_val = str('Threshold has been subceeded by {} %'.format(
	# 			(abs(len_threshold - len_array) / len_threshold_limiter) * 100))
	# 	elif level == 'range':
	# 		if len_array > len_threshold[1]:
	# 			self.alarm_val = str('Range has been exceeded by {} %'.format(
	# 				(abs(len_array - len_threshold[1]) / len_threshold[1]) * 100))
	# 		elif len_threshold_limiter < len_threshold[0]:
	# 			self.alarm_val = str('Threshold has been subceeded by {} %'.format(
	# 				(abs(len_threshold[0] - len_threshold_limiter) / len_array) * 100))
	# 	else:
	# 		raise TypeError
	#
	# 	return self.alarm_val

