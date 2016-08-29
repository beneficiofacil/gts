""" Handlers for implementing v1beta2 of the taskqueue REST API. """
import json
import re
import sys
import tornado.escape

from queue import InvalidLeaseRequest
from queue import PullQueue
from queue import QUEUE_FIELDS
from task import InvalidTaskInfo
from task import Task
from task import TASK_FIELDS
from tornado.web import MissingArgumentError
from tornado.web import RequestHandler
from unpackaged import APPSCALE_LIB_DIR

sys.path.append(APPSCALE_LIB_DIR)
from constants import HTTPCodes

# The prefix for all of the handlers of the pull queue REST API.
REST_PREFIX = '/taskqueue/v1beta2/projects/(?:.~)?([a-z0-9-]+)/taskqueues'

# Matches commas that are outside of parentheses.
FIELD_DELIMITERS_RE = re.compile(r',(?=[^)]*(?:\(|$))')


def parse_fields(fields_string):
  """ Converts a fields string to a tuple.

  Args:
    fields_string: A string extracted from a URL parameter.
  Returns:
    A tuple containing the fields to use. If a field contains sub-fields, it is
    represented as a dictionary.
  """
  fields = []
  for main_field in FIELD_DELIMITERS_RE.split(fields_string):
    if '(' not in main_field:
      fields.append(main_field)
      continue

    section, sub_fields = main_field.split('(')
    fields.append({section: tuple(sub_fields[:-1].split(','))})

  return tuple(fields)


def write_error(request, code, message):
  """ Sets the response headers and body for error messages.

  Args:
    request: A tornado request object.
    code: The HTTP response code.
    message: The error message to use in the body of the request.
  """
  error = {'error': {'code': code, 'message': message}}
  request.set_status(code)
  request.write(json.dumps(error))


class RESTQueue(RequestHandler):
  PATH = '{}/([a-zA-Z0-9-]+)'.format(REST_PREFIX)

  def initialize(self, queue_handler):
    """ Provide access to the queue handler. """
    self.queue_handler = queue_handler

  def get(self, project, queue):
    """ Return info about an existing queue.

    Args:
      project: A string containing an application ID.
      queue: A string containing a queue name.
    """
    queue = self.queue_handler.get_queue(project, queue)
    if queue is None:
      write_error(self, HTTPCodes.NOT_FOUND, 'Queue not found.')
      return

    if not isinstance(queue, PullQueue):
      write_error(self, HTTPCodes.BAD_REQUEST,
                  'The REST API is only applicable to pull queues.')
      return

    get_stats = bool(self.get_argument('getStats', False))

    requested_fields = self.get_argument('fields', None)
    if requested_fields is None:
      fields = QUEUE_FIELDS
    else:
      fields = parse_fields(requested_fields)

    self.write(queue.to_json(include_stats=get_stats, fields=fields))


class RESTTasks(RequestHandler):
  PATH = '{}/([a-zA-Z0-9-]+)/tasks'.format(REST_PREFIX)

  def initialize(self, queue_handler):
    """ Provide access to the queue handler. """
    self.queue_handler = queue_handler

  def get(self, project, queue):
    """ List all non-deleted tasks in a queue, whether or not they are
    currently leased, up to a maximum of 100.

    Args:
      project: A string containing an application ID.
      queue: A string containing a queue name.
    """
    requested_fields = self.get_argument('fields', None)
    if requested_fields is None:
      fields = ('kind', {'items': TASK_FIELDS})
    else:
      fields = parse_fields(requested_fields)

    queue = self.queue_handler.get_queue(project, queue)
    if queue is None:
      write_error(self, HTTPCodes.NOT_FOUND, 'Queue not found.')
      return

    tasks = queue.list_tasks()
    task_list = {}
    if 'kind' in fields:
      task_list['kind'] = 'taskqueues#tasks'

    for field in fields:
      if isinstance(field, dict) and 'items' in field:
        task_list['items'] = [task.json_safe_dict(fields=field['items'])
                              for task in tasks]

    self.write(json.dumps(task_list))

  def post(self, project, queue):
    """ Insert a task into an existing queue.

    Args:
      project: A string containing an application ID.
      queue: A string containing a queue name.
    """
    try:
      task_info = tornado.escape.json_decode(self.request.body)
    except ValueError:
      write_error(self, HTTPCodes.BAD_REQUEST,
                  'The request body must contain a task.')
      return

    task = Task(task_info)

    requested_fields = self.get_argument('fields', None)
    if requested_fields is None:
      fields = TASK_FIELDS
    else:
      fields = parse_fields(requested_fields)

    queue = self.queue_handler.get_queue(project, queue)
    if queue is None:
      write_error(self, HTTPCodes.NOT_FOUND, 'Queue not found.')
      return

    try:
      queue.add_task(task)
    except InvalidTaskInfo as insert_error:
      write_error(self, HTTPCodes.BAD_REQUEST, insert_error.message)
      return

    self.write(json.dumps(task.json_safe_dict(fields=fields)))


class RESTLease(RequestHandler):
  PATH = '{}/([a-zA-Z0-9-]+)/tasks/lease'.format(REST_PREFIX)

  def initialize(self, queue_handler):
    """ Provide access to the queue handler. """
    self.queue_handler = queue_handler

  def post(self, project, queue):
    """ Acquire a lease on the topmost N unowned tasks in a queue.

    Args:
      project: A string containing an application ID.
      queue: A string containing a queue name.
    """
    try:
      lease_seconds = int(self.get_argument('leaseSecs'))
    except MissingArgumentError:
      write_error(self, HTTPCodes.BAD_REQUEST,
                  'Required parameter leaseSecs not specified.')
      return
    except ValueError:
      write_error(self, HTTPCodes.BAD_REQUEST, 'leaseSecs must be an integer.')
      return

    try:
      num_tasks = int(self.get_argument('numTasks'))
    except MissingArgumentError:
      write_error(self, HTTPCodes.BAD_REQUEST,
                  'Required parameter numTasks not specified.')
      return
    except ValueError:
      write_error(self, HTTPCodes.BAD_REQUEST, 'numTasks must be an integer.')
      return

    try:
      group_by_tag = bool(self.get_argument('groupByTag', False))
    except ValueError:
      write_error(self, HTTPCodes.BAD_REQUEST, 'groupByTag must be a boolean.')
      return

    tag = self.get_argument('tag', None)

    requested_fields = self.get_argument('fields', None)
    if requested_fields is None:
      fields = ('kind', {'items': TASK_FIELDS})
    else:
      fields = parse_fields(requested_fields)

    queue = self.queue_handler.get_queue(project, queue)
    if queue is None:
      write_error(self, HTTPCodes.NOT_FOUND, 'Queue not found.')
      return

    try:
      tasks = queue.lease_tasks(num_tasks, lease_seconds, group_by_tag, tag)
    except InvalidLeaseRequest as lease_error:
      write_error(self, HTTPCodes.BAD_REQUEST, lease_error.message)
      return

    task_list = {}
    if 'kind' in fields:
      task_list['kind'] = 'taskqueues#tasks'

    for field in fields:
      if isinstance(field, dict) and 'items' in field:
        task_list['items'] = [task.json_safe_dict(fields=field['items'])
                              for task in tasks]

    self.write(json.dumps(task_list))


class RESTTask(RequestHandler):
  PATH = '{}/([a-zA-Z0-9-]+)/tasks/([a-zA-Z0-9_-]+)'.format(REST_PREFIX)

  def initialize(self, queue_handler):
    """ Provide access to the queue handler. """
    self.queue_handler = queue_handler

  def get(self, project, queue, task):
    """ Get the named task in a queue.

    Args:
      project: A string containing an application ID.
      queue: A string containing a queue name.
      task: A string containing a task ID.
    """
    task = Task({'id': task, 'queueName': queue})

    requested_fields = self.get_argument('fields', None)
    if requested_fields is None:
      fields = TASK_FIELDS
    else:
      fields = parse_fields(requested_fields)

    omit_payload = False
    if 'payloadBase64' not in fields:
      omit_payload = True

    queue = self.queue_handler.get_queue(project, queue)
    if queue is None:
      write_error(self, HTTPCodes.NOT_FOUND, 'Queue not found.')
      return

    task = queue.get_task(task, omit_payload=omit_payload)
    self.write(json.dumps(task.json_safe_dict(fields=fields)))

  def post(self, project, queue, task):
    """ Update the duration of a task lease.

    Args:
      project: A string containing an application ID.
      queue: A string containing a queue name.
      task: A string containing a task ID.
    """
    try:
      task_info = tornado.escape.json_decode(self.request.body)
    except ValueError:
      write_error(self, HTTPCodes.BAD_REQUEST,
                  'The request body must contain a task.')
      return

    if 'leaseTimestamp' not in task_info:
      write_error(self, HTTPCodes.BAD_REQUEST, 'leaseTimestamp is required.')
      return

    # GAE uses the ID from the post body and ignores the value in the URL.
    if 'id' not in task_info:
      write_error(self, HTTPCodes.BAD_REQUEST, 'id is required.')
      return
    provided_task = Task(task_info)

    try:
      new_lease_seconds = int(self.get_argument('newLeaseSeconds'))
    except MissingArgumentError:
      write_error(self, HTTPCodes.BAD_REQUEST,
                  'Required parameter newLeaseSeconds not specified.')
      return
    except ValueError:
      write_error(self, HTTPCodes.BAD_REQUEST,
                  'newLeaseSeconds must be an integer.')
      return

    requested_fields = self.get_argument('fields', None)
    if requested_fields is None:
      fields = TASK_FIELDS
    else:
      fields = parse_fields(requested_fields)

    queue = self.queue_handler.get_queue(project, queue)
    if queue is None:
      write_error(self, HTTPCodes.NOT_FOUND, 'Queue not found.')
      return

    try:
      task = queue.update_lease(provided_task, new_lease_seconds)
    except InvalidLeaseRequest as lease_error:
      write_error(self, HTTPCodes.BAD_REQUEST, lease_error.message)
      return

    self.write(json.dumps(task.json_safe_dict(fields=fields)))

  def delete(self, project, queue, task):
    """ Delete a task from a queue.

    Args:
      project: A string containing an application ID.
      queue: A string containing a queue name.
      task: A string containing a task ID.
    """
    task = Task({'id': task})

    queue = self.queue_handler.get_queue(project, queue)
    if queue is None:
      write_error(self, HTTPCodes.NOT_FOUND, 'Queue not found.')
      return

    queue.delete_task(task)

  def patch(self, project, queue, task):
    """ Update tasks that are leased out of a queue.

    Args:
      project: A string containing an application ID.
      queue: A string containing a queue name.
      task: A string containing a task ID.
    """
    try:
      task_info = tornado.escape.json_decode(self.request.body)
    except ValueError:
      write_error(self, HTTPCodes.BAD_REQUEST,
                  'The request body must contain a task.')
      return

    # GAE requires the queueName to be part of the PATCH.
    if 'queueName' not in task_info:
      write_error(self, HTTPCodes.BAD_REQUEST, 'queueName is invalid.')
      return
    if task_info['queueName'] != queue:
      write_error(self, HTTPCodes.BAD_REQUEST, 'queueName cannot be updated.')
      return

    if 'id' in task_info and task_info['id'] != task:
      write_error(self, HTTPCodes.BAD_REQUEST, 'Task ID cannot be updated.')
      return
    task_info['id'] = task

    try:
      new_task = Task(task_info)
    except InvalidTaskInfo as task_error:
      write_error(self, HTTPCodes.BAD_REQUEST, task_error.message)
      return

    try:
      new_lease_seconds = int(self.get_argument('newLeaseSeconds'))
    except MissingArgumentError:
      write_error(self, HTTPCodes.BAD_REQUEST,
                  'Required parameter newLeaseSeconds not specified.')
      return
    except ValueError:
      write_error(self, HTTPCodes.BAD_REQUEST,
                  'newLeaseSeconds must be an integer.')
      return

    requested_fields = self.get_argument('fields', None)
    if requested_fields is None:
      fields = TASK_FIELDS
    else:
      fields = parse_fields(requested_fields)

    queue = self.queue_handler.get_queue(project, queue)
    if queue is None:
      write_error(self, HTTPCodes.NOT_FOUND, 'Queue not found.')
      return

    try:
      task = queue.update_task(new_task, new_lease_seconds)
    except InvalidLeaseRequest as lease_error:
      write_error(self, HTTPCodes.BAD_REQUEST, lease_error.message)
      return

    self.write(json.dumps(task.json_safe_dict(fields=fields)))