from __future__ import division
"""
Author: Keith Bourgoin
"""
__license__ = """
Copyright 2015 Parse.ly, Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
__all__ = ["Producer", "AsyncProducer"]
from collections import defaultdict, deque
import itertools
import logging
import threading
import time

from .common import CompressionType
from .exceptions import (
    UnknownTopicOrPartition, NotLeaderForPartition, RequestTimedOut,
    ProduceFailureError, SocketDisconnectedError, InvalidMessageError,
    InvalidMessageSize, MessageSizeTooLarge, ProducerQueueFullError
)
from .partitioners import random_partitioner
from .protocol import Message, ProduceRequest


log = logging.getLogger(__name__)


class Producer():
    """
    This class implements the synchronous producer logic found in the
    JVM driver.
    """
    def __init__(self,
                 cluster,
                 topic,
                 partitioner=random_partitioner,
                 compression=CompressionType.NONE,
                 max_retries=3,
                 retry_backoff_ms=100,
                 required_acks=1,
                 ack_timeout_ms=10000,
                 batch_size=200):
        """Instantiate a new Producer.

        :param cluster: The cluster to which to connect
        :type cluster: :class:`pykafka.cluster.Cluster`
        :param topic: The topic to which to produce messages
        :type topic: :class:`pykafka.topic.Topic`
        :param partitioner: The partitioner to use during message production
        :type partitioner: :class:`pykafka.partitioners.BasePartitioner`
        :param compression: The type of compression to use.
        :type compression: :class:`pykafka.common.CompressionType`
        :param max_retries: How many times to attempt to produce messages
            before raising an error.
        :type max_retries: int
        :param retry_backoff_ms: The amount of time (in milliseconds) to
            back off during produce request retries.
        :type retry_backoff_ms: int
        :param required_acks: How many other brokers must have committed the
            data to their log and acknowledged this to the leader before a
            request is considered complete?
        :type required_acks: int
        :param ack_timeout_ms: Amount of time (in milliseconds) to wait for
            acknowledgment of a produce request.
        :type ack_timeout_ms: int
        :param batch_size: Size (in bytes) of batches to send to brokers.
        :type batch_size: int
        """
        # See BaseProduce.__init__.__doc__ for docstring
        self._cluster = cluster
        self._topic = topic
        self._partitioner = partitioner
        self._compression = compression
        self._max_retries = max_retries
        self._retry_backoff_ms = retry_backoff_ms
        self._required_acks = required_acks
        self._ack_timeout_ms = ack_timeout_ms
        self._batch_size = batch_size

    def __repr__(self):
        return "<{module}.{name} at {id_}>".format(
            module=self.__class__.__module__,
            name=self.__class__.__name__,
            id_=hex(id(self))
        )

    def _send_request(self, broker, req, attempt, owned_broker=None):
        """Send the produce request to the broker and handle the response.

        :param broker: The broker to which to send the request
        :type broker: :class:`pykafka.broker.Broker`
        :param req: The produce request to send
        :type req: :class:`pykafka.protocol.ProduceRequest`
        :param attempt: The current attempt count. Used for retry logic
        :type attempt: int
        :param owned_broker: An OwnedBroker instance containing information
            about the queue from which the messages in `req` were removed.
            Only used when called from the
            :class:`pykafka.producer.AsyncProducer`
        :type owned_broker: :class:`pykafka.producer.OwnedBroker`
        """

        def _get_partition_msgs(partition_id, req):
            """Get all the messages for the partitions from the request."""
            messages = itertools.chain.from_iterable(
                mset.messages
                for topic, partitions in req.msets.iteritems()
                for p_id, mset in partitions.iteritems()
                if p_id == partition_id
            )
            for message in messages:
                yield (message.partition_key, message.value), partition_id

        # Do the request
        to_retry = []
        try:
            response = broker.produce_messages(req)

            # Figure out if we need to retry any messages
            # TODO: Convert to using utils.handle_partition_responses
            to_retry = []
            for topic, partitions in response.topics.iteritems():
                for partition, presponse in partitions.iteritems():
                    if presponse.err == 0:
                        if owned_broker is not None:
                            msg_count = len(req.msets[topic][partition].messages)
                            with owned_broker.lock:
                                owned_broker.messages_inflight -= msg_count
                        continue  # All's well
                    if presponse.err == UnknownTopicOrPartition.ERROR_CODE:
                        log.warning('Unknown topic: %s or partition: %s. '
                                    'Retrying.', topic, partition)
                    elif presponse.err == NotLeaderForPartition.ERROR_CODE:
                        log.warning('Partition leader for %s/%s changed. '
                                    'Retrying.', topic, partition)
                        # Update cluster metadata to get new leader
                        self._cluster.update()
                        if owned_broker is not None:
                            owned_broker.producer._update_leaders()
                    elif presponse.err == RequestTimedOut.ERROR_CODE:
                        log.warning('Produce request to %s:%s timed out. '
                                    'Retrying.', broker.host, broker.port)
                    elif presponse.err == InvalidMessageError.ERROR_CODE:
                        log.warning('Encountered InvalidMessageError')
                    elif presponse.err == InvalidMessageSize.ERROR_CODE:
                        log.warning('Encountered InvalidMessageSize')
                        continue
                    elif presponse.err == MessageSizeTooLarge.ERROR_CODE:
                        log.warning('Encountered MessageSizeTooLarge')
                        continue
                    to_retry.extend(_get_partition_msgs(partition, req))
        except SocketDisconnectedError:
            log.warning('Broker %s:%s disconnected. Retrying.',
                        broker.host, broker.port)
            self._cluster.update()
            to_retry = [
                ((message.partition_key, message.value), p_id)
                for topic, partitions in req.msets.iteritems()
                for p_id, mset in partitions.iteritems()
                for message in mset.messages
            ]

        if to_retry:
            attempt += 1
            if attempt < self._max_retries:
                time.sleep(self._retry_backoff_ms / 1000)
                # we have to call _produce here since the broker/partition
                # target for each message may have changed
                self._produce(to_retry, attempt)
            else:
                raise ProduceFailureError('Unable to produce messages. See log for details.')

    def _partition_messages(self, messages):
        """Assign messages to partitions using the partitioner.

        :param messages: Iterable of messages to publish.
        :returns:        Generator of ((key, value), partition_id)
        """
        partitions = self._topic.partitions.values()
        for message in messages:
            if isinstance(message, basestring):
                key = None
                value = message
            else:
                key, value = message
            value = str(value)
            yield (key, value), self._partitioner(partitions, message).id

    def _produce(self, message_partition_tups, attempt):
        """Publish a set of messages to relevant brokers.

        :param message_partition_tups: Messages with partitions assigned.
        :type message_partition_tups:  tuples of ((key, value), partition_id)
        """
        # Requests grouped by broker
        requests = defaultdict(lambda: ProduceRequest(
            compression_type=self._compression,
            required_acks=self._required_acks,
            timeout=self._ack_timeout_ms,
        ))

        for ((key, value), partition_id) in message_partition_tups:
            # N.B. This handles retries, so the leader lookup is needed
            leader = self._topic.partitions[partition_id].leader
            requests[leader].add_message(
                Message(value, partition_key=key),
                self._topic.name,
                partition_id
            )
            # Send requests at the batch size
            if requests[leader].message_count() >= self._batch_size:
                self._send_request(leader, requests.pop(leader), attempt)

        # Send any still not sent
        for leader, req in requests.iteritems():
            self._send_request(leader, req, attempt)

    def produce(self, messages):
        """Produce a set of messages.

        :param messages: The messages to produce
        :type messages: Iterable of str or (str, str) tuples
        """
        # Do partition distribution here. We need to be able to retry producing
        # only *some* messages when a leader changes. Therefore, we don't want
        # a random partition distribution changing that on the retry.
        self._produce(self._partition_messages(messages), 0)


class OwnedBroker():
    """A packet of information for a producer queue

    An OwnedBroker object contains thread-synchronization primitives corresponding
    to a single broker for this producer.

    :ivar lock: The lock used to control access to shared resources for this
        queue
    :type lock: threading.Lock
    :ivar flush_ready: A condition variable that indicates that the queue is
        ready to be flushed via requests to the broker
    :type flush_ready: threading.Condition
    :ivar slot_available: A condition variable that indicates that there is
        at least one position free in the queue for a new message
    :type slot_available: threading.Condition
    :ivar queue: The message queue for this broker. Contains messages that have
        been supplied as arguments to `produce()` waiting to be sent to the
        broker.
    :type queue: collections.deque
    :ivar messages_inflight: A counter indicating how many messages have been
        enqueued for this broker and not yet sent in a request.
    :type messages_inflight: int
    :ivar producer: The producer to which this OwnedBroker instance belongs
    :type producer: :class:`pykafka.producer.AsyncProducer`
    """
    def __init__(self, producer, broker):
        self.producer = producer
        self.broker = broker
        self.lock = threading.Lock()
        self.flush_ready = threading.Event()
        self.slot_available = threading.Event()
        self.queue = deque()
        self.messages_inflight = 0

        def queue_reader():
            while True:
                batch = self.flush(self.producer._linger_ms)
                if batch:
                    self.producer._send_request(batch, self)
        log.info("Starting new produce worker thread for broker %s", broker.id)
        self.producer._cluster.handler.spawn(queue_reader)

    @property
    def message_inflight(self):
        """
        Indicates whether there are currently any messages that have been
            `produce()`d and not yet sent to the broker
        """
        return self.messages_inflight > 0

    def enqueue(self,
                messages,
                max_queued_messages,
                block_on_queue_full,
                acquire=True):
        self._wait_for_available_slot(max_queued_messages, block_on_queue_full)
        if acquire:
            self.lock.acquire()
        self.queue.extendleft(messages)
        self.messages_inflight += len(messages)
        if len(self.queue) >= max_queued_messages:
            if not self.flush_ready.is_set():
                self.flush_ready.set()
        if acquire:
            self.lock.release()

    def flush(self, linger_ms, acquire=True, release_inflight=False):
        self._wait_for_used_slot(linger_ms)
        if acquire:
            self.lock.acquire()
        batch = [self.queue.pop() for _ in xrange(len(self.queue))]
        if release_inflight:
            self.messages_inflight -= len(batch)
        if not self.slot_available.is_set():
            self.slot_available.set()
        if acquire:
            self.lock.release()
        return batch

    def resolve_event_state(self, max_queued_messages):
        if len(self.queue) >= max_queued_messages:
            self.slot_available.clear()
            self.flush_ready.set()
        elif len(self.queue) == 0:
            self.slot_available.set()
            self.flush_ready.clear()
        else:
            self.slot_available.set()

    def _wait_for_used_slot(self, linger_ms):
        if len(self.queue) == 0:
            with self.lock:
                if len(self.queue) == 0:
                    self.flush_ready.clear()
            self.flush_ready.wait(linger_ms / 1000)

    def _wait_for_available_slot(self,
                                 max_queued_messages,
                                 block_on_queue_full):
        if len(self.queue) >= max_queued_messages:
            with self.lock:
                if len(self.queue) >= max_queued_messages:
                    self.slot_available.clear()
            if block_on_queue_full:
                self.slot_available.wait()
            else:
                raise ProducerQueueFullError("Queue full for broker %d",
                                             self.broker.id)


class AsyncProducer():
    """
    This class implements asynchronous producer logic similar to that found in
    the JVM driver. In inherits from the synchronous implementation.
    """
    def __init__(self,
                 cluster,
                 topic,
                 partitioner=random_partitioner,
                 compression=CompressionType.NONE,
                 max_retries=3,
                 retry_backoff_ms=100,
                 required_acks=1,
                 ack_timeout_ms=10 * 1000,
                 max_queued_messages=10000,
                 linger_ms=0,
                 block_on_queue_full=True):
        """Instantiate a new AsyncProducer

        :param cluster: The cluster to which to connect
        :type cluster: :class:`pykafka.cluster.Cluster`
        :param topic: The topic to which to produce messages
        :type topic: :class:`pykafka.topic.Topic`
        :param partitioner: The partitioner to use during message production
        :type partitioner: :class:`pykafka.partitioners.BasePartitioner`
        :param compression: The type of compression to use.
        :type compression: :class:`pykafka.common.CompressionType`
        :param max_retries: How many times to attempt to produce messages
            before raising an error.
        :type max_retries: int
        :param retry_backoff_ms: The amount of time (in milliseconds) to
            back off during produce request retries.
        :type retry_backoff_ms: int
        :param required_acks: How many other brokers must have committed the
            data to their log and acknowledged this to the leader before a
            request is considered complete?
        :type required_acks: int
        :param ack_timeout_ms: Amount of time (in milliseconds) to wait for
            acknowledgment of a produce request.
        :type ack_timeout_ms: int
        :param max_queued_messages: The maximum number of messages the producer
            can have waiting to be sent to the broker. If messages are sent
            faster than they can be delivered to the broker, the producer will
            either block or throw an exception based on the preference specified
            by block_on_queue_full.
        :type max_queued_messages: int
        :param linger_ms: This setting gives the upper bound on the delay for
            batching: once the producer gets max_queued_messages worth of
            messages for a broker, it will be sent immediately regardless of
            this setting.  However, if we have fewer than this many messages
            accumulated for this partition we will 'linger' for the specified
            time waiting for more records to show up. linger_ms=0 indicates no
            lingering.
        :type linger_ms: int
        :param block_on_queue_full: When the producer's message queue for a
            broker contains max_queued_messages, we must either stop accepting
            new messages (block) or throw an error. If True, this setting
            indicates we should block until space is available in the queue.
        :type block_on_queue_full: bool
        """
        self._cluster = cluster
        self._topic = topic
        self._partitioner = partitioner
        self._compression = compression
        self._max_retries = max_retries
        self._retry_backoff_ms = retry_backoff_ms
        self._required_acks = required_acks
        self._ack_timeout_ms = ack_timeout_ms
        self._max_queued_messages = max_queued_messages
        self._linger_ms = linger_ms
        self._block_on_queue_full = block_on_queue_full

        self._running = False
        self.start()

    def start(self):
        """Connect to brokers and start worker threads"""
        if not self._running:
            self._setup_internal_producer()
            self._owned_brokers = {}
            for partition in self._topic.partitions.values():
                if partition.leader.id not in self._owned_brokers:
                    self._owned_brokers[partition.leader.id] = OwnedBroker(
                        self, partition.leader)
            self._running = True

    def stop(self):
        """Mark as stopped and wait for all inflight messages to send"""
        self._running = False
        last_log = time.time()
        while any(q.message_inflight for q in self._owned_brokers.itervalues()):
            if time.time() - last_log >= .5:
                log.info("Waiting for all worker threads to exit")
                last_log = time.time()

    def __enter__(self):
        """Context manager entry point - start the producer"""
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        """Context manager exit point - stop the producer"""
        self.stop()

    def _setup_internal_producer(self):
        """Instantiate the internal producer"""
        self._producer = Producer(
            self._cluster,
            self._topic,
            self._partitioner,
            compression=self._compression,
            max_retries=self._max_retries,
            retry_backoff_ms=self._retry_backoff_ms,
            required_acks=self._required_acks,
            ack_timeout_ms=self._ack_timeout_ms,
        )

    def _produce(self, message_partition_tups):
        """Enqueue a set of messages for the relevant brokers

        :param message_partition_tups: Messages with partitions assigned.
        :type message_partition_tups: tuples of ((key, value), partition_id)
        """
        for ((key, value), partition_id) in message_partition_tups:
            leader = self._topic.partitions[partition_id].leader
            self._owned_brokers[leader.id].enqueue(
                [((key, value), partition_id)],
                self._max_queued_messages,
                self._block_on_queue_full)

    def _send_request(self, message_batch, owned_broker):
        """Prepare a request and send it to the broker

        :param message_batch: An iterable of messages to send
        :type message_batch: iterable of `((key, value), partition_id)` tuples
        :param owned_broker: The `OwnedBroker` to which to send the request
        :type owned_broker: :class:`pykafka.producer.OwnedBroker`
        """
        log.debug("Sending %d messages to broker %d",
                  len(message_batch), owned_broker.broker.id)
        request = ProduceRequest(
            compression_type=self._compression,
            required_acks=self._required_acks,
            timeout=self._ack_timeout_ms
        )
        for (key, value), partition_id in message_batch:
            request.add_message(
                Message(value, partition_key=key),
                self._topic.name,
                partition_id
            )
        try:
            self._producer._send_request(owned_broker.broker, request, 0,
                                         owned_broker)
        except ProduceFailureError as e:
            log.error("Producer error: %s", e)

    def _update_leaders(self):
        """Ensure each message in each queue is in the queue owned by its
            partition's leader

        This function empties all broker queues, maps their messages to the
        current leaders for their partitions, and enqueues the messages in
        the appropriate queues.
        """
        # empty queues and figure out updated partition leaders
        new_queue_contents = defaultdict(list)
        for owned_broker in self._owned_brokers.itervalues():
            owned_broker.lock.acquire()
            current_queue_contents = owned_broker.flush(0, acquire=False,
                                                        release_inflight=True)
            for kv, partition_id in current_queue_contents:
                partition_leader = self._topic.partitions[partition_id].leader
                new_queue_contents[partition_leader.id].append((kv, partition_id))
        # retain locks for all brokers between these two steps
        for owned_broker in self._owned_brokers.itervalues():
            owned_broker.enqueue(new_queue_contents[owned_broker.broker.id],
                                 self._max_queued_messages,
                                 self._block_on_queue_full,
                                 acquire=False)
            owned_broker.resolve_event_state(self._max_queued_messages)
            owned_broker.lock.release()

    def produce(self, messages):
        """Produce a set of messages.

        :param messages: The messages to produce
        :type messages: Iterable of str or (str, str) tuples
        """
        self._produce(self._producer._partition_messages(messages))
