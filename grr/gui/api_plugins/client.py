#!/usr/bin/env python
"""API handlers for accessing and searching clients and managing labels."""

import shlex
import sys

from grr.gui import api_call_handler_base
from grr.gui import api_call_handler_utils

from grr.gui.api_plugins import stats as api_stats

from grr.lib import aff4
from grr.lib import client_index
from grr.lib import data_store
from grr.lib import events
from grr.lib import flow
from grr.lib import ip_resolver
from grr.lib import queue_manager
from grr.lib import rdfvalue
from grr.lib import timeseries
from grr.lib import utils

from grr.lib.aff4_objects import aff4_grr
from grr.lib.aff4_objects import collects
from grr.lib.aff4_objects import standard
from grr.lib.aff4_objects import stats as aff4_stats

from grr.lib.flows.general import audit
from grr.lib.flows.general import discovery

from grr.lib.rdfvalues import aff4_rdfvalues
from grr.lib.rdfvalues import client as rdf_client
from grr.lib.rdfvalues import flows as rdf_flows
from grr.lib.rdfvalues import structs as rdf_structs

from grr.proto import api_pb2


class InterrogateOperationNotFoundError(
    api_call_handler_base.ResourceNotFoundError):
  """Raised when an interrogate operation could not be found."""


class ApiClient(rdf_structs.RDFProtoStruct):
  protobuf = api_pb2.ApiClient

  def InitFromAff4Object(self, client_obj):
    self.urn = client_obj.urn

    self.agent_info = client_obj.Get(client_obj.Schema.CLIENT_INFO)
    self.hardware_info = client_obj.Get(client_obj.Schema.HARDWARE_INFO)
    self.os_info = rdf_client.Uname(
        system=client_obj.Get(client_obj.Schema.SYSTEM),
        node=client_obj.Get(client_obj.Schema.HOSTNAME),
        release=client_obj.Get(client_obj.Schema.OS_RELEASE),
        # TODO(user): Check if ProtoString.Validate should be fixed
        # to do an isinstance() check on a value. Is simple type
        # equality check used there for performance reasons?
        version=utils.SmartStr(
            client_obj.Get(client_obj.Schema.OS_VERSION, "")),
        kernel=client_obj.Get(client_obj.Schema.KERNEL),
        machine=client_obj.Get(client_obj.Schema.ARCH),
        fqdn=client_obj.Get(client_obj.Schema.FQDN),
        install_date=client_obj.Get(client_obj.Schema.INSTALL_DATE))
    self.knowledge_base = client_obj.Get(client_obj.Schema.KNOWLEDGE_BASE)
    self.memory_size = client_obj.Get(client_obj.Schema.MEMORY_SIZE)

    self.first_seen_at = client_obj.Get(client_obj.Schema.FIRST_SEEN)
    self.last_seen_at = client_obj.Get(client_obj.Schema.PING)
    self.last_booted_at = client_obj.Get(client_obj.Schema.LAST_BOOT_TIME)
    self.last_clock = client_obj.Get(client_obj.Schema.CLOCK)
    last_crash = client_obj.Get(client_obj.Schema.LAST_CRASH)
    if last_crash is not None:
      self.last_crash_at = last_crash.timestamp

    self.labels = client_obj.GetLabels()
    self.interfaces = client_obj.Get(client_obj.Schema.INTERFACES)
    kb = client_obj.Get(client_obj.Schema.KNOWLEDGE_BASE)
    self.users = kb and kb.users or []
    self.volumes = client_obj.Get(client_obj.Schema.VOLUMES)

    type_obj = client_obj.Get(client_obj.Schema.TYPE)
    if type_obj:
      self.age = type_obj.age

    return self


class ApiClientActionRequest(rdf_structs.RDFProtoStruct):
  protobuf = api_pb2.ApiClientActionRequest


class ApiSearchClientsArgs(rdf_structs.RDFProtoStruct):
  protobuf = api_pb2.ApiSearchClientsArgs


class ApiSearchClientsResult(rdf_structs.RDFProtoStruct):
  protobuf = api_pb2.ApiSearchClientsResult


class ApiSearchClientsHandler(api_call_handler_base.ApiCallHandler):
  """Renders results of a client search."""

  args_type = ApiSearchClientsArgs
  result_type = ApiSearchClientsResult

  def Handle(self, args, token=None):
    end = args.count or sys.maxint

    keywords = shlex.split(args.query)

    index = aff4.FACTORY.Create(
        client_index.MAIN_INDEX,
        aff4_type=client_index.ClientIndex,
        mode="rw",
        token=token)
    result_urns = sorted(
        index.LookupClients(keywords), key=str)[args.offset:args.offset + end]
    result_set = aff4.FACTORY.MultiOpen(result_urns, token=token)

    api_clients = []
    for child in result_set:
      api_clients.append(ApiClient().InitFromAff4Object(child))

    return ApiSearchClientsResult(items=api_clients)


class ApiLabelsRestrictedSearchClientsHandler(
    api_call_handler_base.ApiCallHandler):
  """Renders results of a client search."""

  args_type = ApiSearchClientsArgs
  result_type = ApiSearchClientsResult

  def __init__(self, labels_whitelist=None, labels_owners_whitelist=None):
    super(ApiLabelsRestrictedSearchClientsHandler, self).__init__()

    self.labels_whitelist = set(labels_whitelist or [])
    self.labels_owners_whitelist = set(labels_owners_whitelist or [])

  def _CheckClientLabels(self, client_obj, token=None):
    for label in client_obj.GetLabels():
      if (label.name in self.labels_whitelist and
          label.owner in self.labels_owners_whitelist):
        return True

    return False

  def Handle(self, args, token=None):
    if args.count:
      end = args.offset + args.count
    else:
      end = sys.maxint

    keywords = shlex.split(args.query)

    index = aff4.FACTORY.Create(
        client_index.MAIN_INDEX,
        aff4_type=client_index.ClientIndex,
        mode="rw",
        token=token)
    all_urns = set()
    for label in self.labels_whitelist:
      label_filter = ["label:" + label] + keywords
      all_urns.update(index.LookupClients(label_filter))

    all_objs = aff4.FACTORY.MultiOpen(
        sorted(
            all_urns, key=str), aff4_type=aff4_grr.VFSGRRClient, token=token)

    api_clients = []
    index = 0
    for client_obj in all_objs:
      if self._CheckClientLabels(client_obj):
        if index >= args.offset and index < end:
          api_clients.append(ApiClient().InitFromAff4Object(client_obj))

        index += 1
        if index >= end:
          break

    return ApiSearchClientsResult(items=api_clients)


class ApiGetClientArgs(rdf_structs.RDFProtoStruct):
  protobuf = api_pb2.ApiGetClientArgs


class ApiGetClientHandler(api_call_handler_base.ApiCallHandler):
  """Renders summary of a given client."""

  args_type = ApiGetClientArgs
  result_type = ApiClient

  def Handle(self, args, token=None):
    if not args.timestamp:
      age = rdfvalue.RDFDatetime.Now()
    else:
      age = rdfvalue.RDFDatetime(args.timestamp)

    client = aff4.FACTORY.Open(
        args.client_id, aff4_type=aff4_grr.VFSGRRClient, age=age, token=token)

    return ApiClient().InitFromAff4Object(client)


class ApiGetClientVersionTimesArgs(rdf_structs.RDFProtoStruct):
  protobuf = api_pb2.ApiGetClientVersionTimesArgs


class ApiGetClientVersionTimesResult(rdf_structs.RDFProtoStruct):
  protobuf = api_pb2.ApiGetClientVersionTimesResult


class ApiGetClientVersionTimesHandler(api_call_handler_base.ApiCallHandler):
  """Retrieves a list of versions for the given client."""

  args_type = ApiGetClientVersionTimesArgs
  result_type = ApiGetClientVersionTimesResult

  def Handle(self, args, token=None):
    fd = aff4.FACTORY.Open(
        args.client_id, mode="r", age=aff4.ALL_TIMES, token=token)

    type_values = list(fd.GetValuesForAttribute(fd.Schema.TYPE))
    times = sorted([t.age for t in type_values], reverse=True)

    return ApiGetClientVersionTimesResult(times=times)


class ApiInterrogateClientArgs(rdf_structs.RDFProtoStruct):
  protobuf = api_pb2.ApiInterrogateClientArgs


class ApiInterrogateClientResult(rdf_structs.RDFProtoStruct):
  protobuf = api_pb2.ApiInterrogateClientResult


class ApiInterrogateClientHandler(api_call_handler_base.ApiCallHandler):
  """Interrogates the given client."""

  args_type = ApiInterrogateClientArgs
  result_type = ApiInterrogateClientResult

  def Handle(self, args, token=None):
    flow_urn = flow.GRRFlow.StartFlow(
        client_id=args.client_id, flow_name="Interrogate", token=token)

    return ApiInterrogateClientResult(operation_id=str(flow_urn))


class ApiGetInterrogateOperationStateArgs(rdf_structs.RDFProtoStruct):
  protobuf = api_pb2.ApiGetInterrogateOperationStateArgs


class ApiGetInterrogateOperationStateResult(rdf_structs.RDFProtoStruct):
  protobuf = api_pb2.ApiGetInterrogateOperationStateResult


class ApiGetInterrogateOperationStateHandler(
    api_call_handler_base.ApiCallHandler):
  """Retrieves the state of the interrogate operation."""

  args_type = ApiGetInterrogateOperationStateArgs
  result_type = ApiGetInterrogateOperationStateResult

  def Handle(self, args, token=None):
    try:
      flow_obj = aff4.FACTORY.Open(
          args.operation_id, aff4_type=discovery.Interrogate, token=token)

      complete = not flow_obj.GetRunner().IsRunning()
    except aff4.InstantiationError:
      raise InterrogateOperationNotFoundError("Operation with id %s not found" %
                                              args.operation_id)

    result = ApiGetInterrogateOperationStateResult()
    if complete:
      result.state = ApiGetInterrogateOperationStateResult.State.FINISHED
    else:
      result.state = ApiGetInterrogateOperationStateResult.State.RUNNING

    return result


class ApiGetLastClientIPAddressArgs(rdf_structs.RDFProtoStruct):
  protobuf = api_pb2.ApiGetLastClientIPAddressArgs


class ApiGetLastClientIPAddressResult(rdf_structs.RDFProtoStruct):
  protobuf = api_pb2.ApiGetLastClientIPAddressResult


class ApiGetLastClientIPAddressHandler(api_call_handler_base.ApiCallHandler):
  """Retrieves the last ip a client used for communication with the server."""

  args_type = ApiGetLastClientIPAddressArgs
  result_type = ApiGetLastClientIPAddressResult

  def Handle(self, args, token=None):
    client = aff4.FACTORY.Open(
        args.client_id, aff4_type=aff4_grr.VFSGRRClient, token=token)

    ip = client.Get(client.Schema.CLIENT_IP)
    status, info = ip_resolver.IP_RESOLVER.RetrieveIPInfo(ip)

    return ApiGetLastClientIPAddressResult(ip=ip, info=info, status=status)


class ApiListClientCrashesArgs(rdf_structs.RDFProtoStruct):
  protobuf = api_pb2.ApiListClientCrashesArgs


class ApiListClientCrashesResult(rdf_structs.RDFProtoStruct):
  protobuf = api_pb2.ApiListClientCrashesResult


class ApiListClientCrashesHandler(api_call_handler_base.ApiCallHandler):
  """Returns a list of crashes for the given client."""

  args_type = ApiListClientCrashesArgs
  result_type = ApiListClientCrashesResult

  def Handle(self, args, token=None):
    try:
      aff4_crashes = aff4.FACTORY.Open(
          args.client_id.Add("crashes"),
          mode="r",
          aff4_type=collects.PackedVersionedCollection,
          token=token)

      total_count = len(aff4_crashes)
      result = api_call_handler_utils.FilterCollection(aff4_crashes,
                                                       args.offset, args.count,
                                                       args.filter)
    except aff4.InstantiationError:
      total_count = 0
      result = []

    return ApiListClientCrashesResult(items=result, total_count=total_count)


class ApiAddClientsLabelsArgs(rdf_structs.RDFProtoStruct):
  protobuf = api_pb2.ApiAddClientsLabelsArgs


class ApiAddClientsLabelsHandler(api_call_handler_base.ApiCallHandler):
  """Adds labels to a given client."""

  args_type = ApiAddClientsLabelsArgs

  def Handle(self, args, token=None):
    audit_description = ",".join([
        token.username + u"." + utils.SmartUnicode(name) for name in args.labels
    ])
    audit_events = []

    try:
      index = aff4.FACTORY.Create(
          client_index.MAIN_INDEX,
          aff4_type=client_index.ClientIndex,
          mode="rw",
          token=token)
      client_objs = aff4.FACTORY.MultiOpen(
          args.client_ids,
          aff4_type=aff4_grr.VFSGRRClient,
          mode="rw",
          token=token)
      for client_obj in client_objs:
        client_obj.AddLabels(*args.labels)
        index.AddClient(client_obj)
        client_obj.Close()

        audit_events.append(
            events.AuditEvent(
                user=token.username,
                action="CLIENT_ADD_LABEL",
                flow_name="handler.ApiAddClientsLabelsHandler",
                client=client_obj.urn,
                description=audit_description))
    finally:
      events.Events.PublishMultipleEvents(
          {
              audit.AUDIT_EVENT: audit_events
          }, token=token)


class ApiRemoveClientsLabelsArgs(rdf_structs.RDFProtoStruct):
  protobuf = api_pb2.ApiRemoveClientsLabelsArgs


class ApiRemoveClientsLabelsHandler(api_call_handler_base.ApiCallHandler):
  """Remove labels from a given client."""

  args_type = ApiRemoveClientsLabelsArgs

  def RemoveClientLabels(self, client, labels_names):
    """Removes labels with given names from a given client object."""

    affected_owners = set()
    for label in client.GetLabels():
      if label.name in labels_names and label.owner != "GRR":
        affected_owners.add(label.owner)

    for owner in affected_owners:
      client.RemoveLabels(*labels_names, owner=owner)

  def Handle(self, args, token=None):
    audit_description = ",".join([
        token.username + u"." + utils.SmartUnicode(name) for name in args.labels
    ])
    audit_events = []

    try:
      index = aff4.FACTORY.Create(
          client_index.MAIN_INDEX,
          aff4_type=client_index.ClientIndex,
          mode="rw",
          token=token)
      client_objs = aff4.FACTORY.MultiOpen(
          args.client_ids,
          aff4_type=aff4_grr.VFSGRRClient,
          mode="rw",
          token=token)
      for client_obj in client_objs:
        index.RemoveClientLabels(client_obj)
        self.RemoveClientLabels(client_obj, args.labels)
        index.AddClient(client_obj)
        client_obj.Close()

        audit_events.append(
            events.AuditEvent(
                user=token.username,
                action="CLIENT_REMOVE_LABEL",
                flow_name="handler.ApiRemoveClientsLabelsHandler",
                client=client_obj.urn,
                description=audit_description))
    finally:
      events.Events.PublishMultipleEvents(
          {
              audit.AUDIT_EVENT: audit_events
          }, token=token)


class ApiListClientsLabelsResult(rdf_structs.RDFProtoStruct):
  protobuf = api_pb2.ApiListClientsLabelsResult


class ApiListClientsLabelsHandler(api_call_handler_base.ApiCallHandler):
  """Lists all the available clients labels."""

  result_type = ApiListClientsLabelsResult

  def Handle(self, args, token=None):
    labels_index = aff4.FACTORY.Create(
        standard.LabelSet.CLIENT_LABELS_URN,
        standard.LabelSet,
        mode="r",
        token=token)
    label_objects = []
    for label in labels_index.ListLabels():
      label_objects.append(aff4_rdfvalues.AFF4ObjectLabel(name=label))

    return ApiListClientsLabelsResult(items=label_objects)


class ApiListKbFieldsResult(rdf_structs.RDFProtoStruct):
  protobuf = api_pb2.ApiListKbFieldsResult


class ApiListKbFieldsHandler(api_call_handler_base.ApiCallHandler):
  """Lists all the available clients knowledge base fields."""

  result_type = ApiListKbFieldsResult

  def Handle(self, args, token=None):
    fields = rdf_client.KnowledgeBase().GetKbFieldNames()
    return ApiListKbFieldsResult(items=sorted(fields))


class ApiListClientActionRequestsArgs(rdf_structs.RDFProtoStruct):
  protobuf = api_pb2.ApiListClientActionRequestsArgs


class ApiListClientActionRequestsResult(rdf_structs.RDFProtoStruct):
  protobuf = api_pb2.ApiListClientActionRequestsResult


class ApiListClientActionRequestsHandler(api_call_handler_base.ApiCallHandler):
  """Lists pending client action requests."""

  args_type = ApiListClientActionRequestsArgs
  result_type = ApiListClientActionRequestsResult

  REQUESTS_NUM_LIMIT = 1000

  def _GetRequestResponses(self, manager, client_id, task_id):
    task_id = "task:%s" % task_id

    request_messages = manager.Query(client_id, task_id=task_id)
    if not request_messages:
      return []

    request_message = request_messages[0]
    state_queue = request_message.session_id.Add("state/request:%08X" %
                                                 request_message.request_id)

    result = []
    predicate_pre = (
        manager.FLOW_RESPONSE_PREFIX + "%08X" % request_message.request_id)
    # Get all the responses for this request.
    for _, serialized_message, _ in data_store.DB.ResolvePrefix(
        state_queue, predicate_pre, token=manager.token):
      result.append(
          rdf_flows.GrrMessage.FromSerializedString(serialized_message))

    return result

  def Handle(self, args, token=None):
    manager = queue_manager.QueueManager(token=token)

    result = ApiListClientActionRequestsResult()
    # Passing "limit" argument explicitly, as Query returns just 1 request
    # by default.
    for task in manager.Query(
        args.client_id, limit=self.__class__.REQUESTS_NUM_LIMIT):
      request = ApiClientActionRequest(
          task_id=task.task_id,
          task_eta=task.eta,
          session_id=task.session_id,
          client_action=task.name)

      if args.fetch_responses:
        request.responses = self._GetRequestResponses(manager, args.client_id,
                                                      task.task_id)

      result.items.append(request)

    return result


class ApiGetClientLoadStatsArgs(rdf_structs.RDFProtoStruct):
  protobuf = api_pb2.ApiGetClientLoadStatsArgs


class ApiGetClientLoadStatsResult(rdf_structs.RDFProtoStruct):
  protobuf = api_pb2.ApiGetClientLoadStatsResult


class ApiGetClientLoadStatsHandler(api_call_handler_base.ApiCallHandler):
  """Returns client load stats data."""

  args_type = ApiGetClientLoadStatsArgs
  result_type = ApiGetClientLoadStatsResult

  # pyformat: disable
  GAUGE_METRICS = [
      ApiGetClientLoadStatsArgs.Metric.CPU_PERCENT,
      ApiGetClientLoadStatsArgs.Metric.MEMORY_PERCENT,
      ApiGetClientLoadStatsArgs.Metric.MEMORY_RSS_SIZE,
      ApiGetClientLoadStatsArgs.Metric.MEMORY_VMS_SIZE
  ]
  # pyformat: enable
  MAX_SAMPLES = 100

  def Handle(self, args, token=None):
    start_time = args.start
    end_time = args.end

    if not end_time:
      end_time = rdfvalue.RDFDatetime.Now()

    if not start_time:
      start_time = end_time - rdfvalue.Duration("30m")

    fd = aff4.FACTORY.Create(
        args.client_id.Add("stats"),
        aff4_type=aff4_stats.ClientStats,
        mode="r",
        token=token,
        age=(start_time, end_time))

    stat_values = list(fd.GetValuesForAttribute(fd.Schema.STATS))
    points = []
    for stat_value in reversed(stat_values):
      if args.metric == args.Metric.CPU_PERCENT:
        points.extend((s.cpu_percent, s.timestamp)
                      for s in stat_value.cpu_samples)
      elif args.metric == args.Metric.CPU_SYSTEM:
        points.extend((s.system_cpu_time, s.timestamp)
                      for s in stat_value.cpu_samples)
      elif args.metric == args.Metric.CPU_USER:
        points.extend((s.user_cpu_time, s.timestamp)
                      for s in stat_value.cpu_samples)
      elif args.metric == args.Metric.IO_READ_BYTES:
        points.extend((s.read_bytes, s.timestamp)
                      for s in stat_value.io_samples)
      elif args.metric == args.Metric.IO_WRITE_BYTES:
        points.extend((s.write_bytes, s.timestamp)
                      for s in stat_value.io_samples)
      elif args.metric == args.Metric.IO_READ_OPS:
        points.extend((s.read_count, s.timestamp)
                      for s in stat_value.io_samples)
      elif args.metric == args.Metric.IO_WRITE_OPS:
        points.extend((s.write_count, s.timestamp)
                      for s in stat_value.io_samples)
      elif args.metric == args.Metric.NETWORK_BYTES_RECEIVED:
        points.append((stat_value.bytes_received, stat_value.age))
      elif args.metric == args.Metric.NETWORK_BYTES_SENT:
        points.append((stat_value.bytes_sent, stat_value.age))
      elif args.metric == args.Metric.MEMORY_PERCENT:
        points.append((stat_value.memory_percent, stat_value.age))
      elif args.metric == args.Metric.MEMORY_RSS_SIZE:
        points.append((stat_value.RSS_size, stat_value.age))
      elif args.metric == args.Metric.MEMORY_VMS_SIZE:
        points.append((stat_value.VMS_size, stat_value.age))
      else:
        raise ValueError("Unknown metric.")

    # Points collected from "cpu_samples" and "io_samples" may not be correctly
    # sorted in some cases (as overlaps between different stat_values are
    # possible).
    points.sort(key=lambda x: x[1])

    ts = timeseries.Timeseries()
    ts.MultiAppend(points)

    if args.metric not in self.GAUGE_METRICS:
      ts.MakeIncreasing()

    if len(stat_values) > self.MAX_SAMPLES:
      sampling_interval = rdfvalue.Duration.FromSeconds((
          (end_time - start_time).seconds / self.MAX_SAMPLES) or 1)
      if args.metric in self.GAUGE_METRICS:
        mode = timeseries.NORMALIZE_MODE_GAUGE
      else:
        mode = timeseries.NORMALIZE_MODE_COUNTER

      ts.Normalize(sampling_interval, start_time, end_time, mode=mode)

    result = ApiGetClientLoadStatsResult()
    for value, timestamp in ts.data:
      dp = api_stats.ApiStatsStoreMetricDataPoint(
          timestamp=timestamp, value=value)
      result.data_points.append(dp)

    return result
