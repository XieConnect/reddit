# The contents of this file are subject to the Common Public Attribution
# License Version 1.0. (the "License"); you may not use this file except in
# compliance with the License. You may obtain a copy of the License at
# http://code.reddit.com/LICENSE. The License is based on the Mozilla Public
# License Version 1.1, but Sections 14 and 15 have been added to cover use of
# software over a computer network and provide for limited attribution for the
# Original Developer. In addition, Exhibit A has been modified to be consistent
# with Exhibit B.
#
# Software distributed under the License is distributed on an "AS IS" basis,
# WITHOUT WARRANTY OF ANY KIND, either express or implied. See the License for
# the specific language governing rights and limitations under the License.
#
# The Original Code is Reddit.
#
# The Original Developer is the Initial Developer.  The Initial Developer of the
# Original Code is CondeNet, Inc.
#
# All portions of the code written by CondeNet are Copyright (c) 2006-2010
# CondeNet, Inc. All Rights Reserved.
################################################################################

from Queue import Queue
from threading import local, Thread
from datetime import datetime
import os
import sys
import time
import errno
import socket
import itertools
import pickle

from amqplib import client_0_8 as amqp

from pylons import g

amqp_host = g.amqp_host
amqp_user = g.amqp_user
amqp_pass = g.amqp_pass
amqp_exchange = 'reddit_exchange'
log = g.log
amqp_virtual_host = g.amqp_virtual_host
amqp_logging = g.amqp_logging

#there are two ways of interacting with this module: add_item and
#handle_items/consume_items. _add_item (the internal function for
#adding items to amqp that are added using add_item) might block for
#an arbitrary amount of time while trying to get a connection to amqp.

class Worker:
    def __init__(self):
        self.q = Queue()
        self.t = Thread(target=self._handle)
        self.t.setDaemon(True)
        self.t.start()

    def _handle(self):
        while True:
            fn = self.q.get()
            try:
                fn()
                self.q.task_done()
            except:
                import traceback
                print traceback.format_exc()

    def do(self, fn, *a, **kw):
        fn1 = lambda: fn(*a, **kw)
        self.q.put(fn1)

    def join(self):
        self.q.join()

worker = Worker()

class ConnectionManager(local):
    # There should be only two threads that ever talk to AMQP: the
    # worker thread and the foreground thread (whether consuming queue
    # items or a shell). This class is just a wrapper to make sure
    # that they get separate connections
    def __init__(self):
        self.connection = None
        self.channel = None
        self.have_init = False

    def get_connection(self):
        while not self.connection:
            try:
                self.connection = amqp.Connection(host = amqp_host,
                                                  userid = amqp_user,
                                                  password = amqp_pass,
                                                  virtual_host = amqp_virtual_host,
                                                  insist = False)
            except (socket.error, IOError):
                print 'error connecting to amqp %s @ %s' % (amqp_user, amqp_host)
                time.sleep(1)

        # don't run init_queue until someone actually needs it. this
        # allows the app server to start and serve most pages if amqp
        # isn't running
        if not self.have_init:
            self.init_queue()
            self.have_init = True

        return self.connection

    def get_channel(self, reconnect = False):
        # Periodic (and increasing with uptime) errors appearing when
        # connection object is still present, but appears to have been
        # closed.  This checks that the the connection is still open.
        if self.connection and self.connection.channels is None:
            log.error("Error: amqp.py, connection object with no available channels.  Reconnecting...")
            self.connection = None

        if not self.connection or reconnect:
            self.connection = None
            self.channel = None
            self.get_connection()

        if not self.channel:
            self.channel = self.connection.channel()

        return self.channel

    def init_queue(self):
        from r2.lib.queues import RedditQueueMap

        chan = self.get_channel()

        RedditQueueMap(amqp_exchange, chan).init()

connection_manager = ConnectionManager()

DELIVERY_TRANSIENT = 1
DELIVERY_DURABLE = 2

def _add_item(routing_key, body, message_id = None,
              delivery_mode = DELIVERY_DURABLE):
    """adds an item onto a queue. If the connection to amqp is lost it
    will try to reconnect and then call itself again."""
    if not amqp_host:
        log.error("Ignoring amqp message %r to %r" % (body, routing_key))
        return

    chan = connection_manager.get_channel()
    msg = amqp.Message(body,
                       timestamp = datetime.now(),
                       delivery_mode = delivery_mode)
    if message_id:
        msg.properties['message_id'] = message_id

    try:
        chan.basic_publish(msg,
                           exchange = amqp_exchange,
                           routing_key = routing_key)
    except Exception as e:
        if e.errno == errno.EPIPE:
            get_channel(True)
            add_item(routing_key, body, message_id)
        else:
            raise

def add_item(routing_key, body, message_id = None, delivery_mode = DELIVERY_DURABLE):
    if amqp_host and amqp_logging:
        log.debug("amqp: adding item %r to %r" % (body, routing_key))

    worker.do(_add_item, routing_key, body, message_id = message_id,
              delivery_mode = delivery_mode)

def add_kw(routing_key, **kw):
    add_item(routing_key, pickle.dumps(kw))

def consume_items(queue, callback, verbose=True):
    """A lighter-weight version of handle_items that uses AMQP's
       basic.consume instead of basic.get. Callback is only passed a
       single items at a time. This is more efficient than
       handle_items when the queue is likely to be occasionally empty
       or if batching the received messages is not necessary."""
    chan = connection_manager.get_channel()

    def _callback(msg):
        if verbose:
            count_str = ''
            if 'message_count' in msg.delivery_info:
                # the count from the last message, if the count is
                # available
                count_str = '(%d remaining)' % msg.delivery_info['message_count']

            print "%s: 1 item %s" % (queue, count_str)

        g.reset_caches()
        ret = callback(msg)
        msg.channel.basic_ack(msg.delivery_tag)
        sys.stdout.flush()
        return ret

    chan.basic_consume(queue=queue, callback=_callback)

    try:
        while chan.callbacks:
            try:
                chan.wait()
            except KeyboardInterrupt:
                chan.close()
                break
    finally:
        if chan.is_open:
            chan.close()

def handle_items(queue, callback, ack = True, limit = 1, drain = False,
                 verbose=True, sleep_time = 1):
    """Call callback() on every item in a particular queue. If the
       connection to the queue is lost, it will die. Intended to be
       used as a long-running process."""

    chan = connection_manager.get_channel()
    countdown = None

    while True:

        # NB: None != 0, so we don't need an "is not None" check here
        if countdown == 0:
            break

        msg = chan.basic_get(queue)
        if not msg and drain:
            return
        elif not msg:
            time.sleep(sleep_time)
            continue

        if countdown is None and drain and 'message_count' in msg.delivery_info:
            countdown = 1 + msg.delivery_info['message_count']

        g.reset_caches()

        items = []

        while msg and countdown != 0:
            items.append(msg)
            if countdown is not None:
                countdown -= 1
            if len(items) >= limit:
                break # the innermost loop only
            msg = chan.basic_get(queue)

        try:
            count_str = ''
            if 'message_count' in items[-1].delivery_info:
                # the count from the last message, if the count is
                # available
                count_str = '(%d remaining)' % items[-1].delivery_info['message_count']
            if verbose:
                print "%s: %d items %s" % (queue, len(items), count_str)
            callback(items, chan)

            if ack:
                # ack *all* outstanding messages
                chan.basic_ack(0, multiple=True)

            # flush any log messages printed by the callback
            sys.stdout.flush()
        except:
            for item in items:
                # explicitly reject the items that we've not processed
                chan.basic_reject(item.delivery_tag, requeue = True)
            raise


def empty_queue(queue):
    """debug function to completely erase the contents of a queue"""
    chan = connection_manager.get_channel()
    chan.queue_purge(queue)


def _test_setup(test_q = 'test_q'):
    from r2.lib.queues import RedditQueueMap
    chan = connection_manager.get_channel()
    rqm = RedditQueueMap(amqp_exchange, chan)
    rqm._q(test_q, durable=False, auto_delete=True, self_refer=True)
    return chan

def test_consume(test_q = 'test_q'):
    chan = _test_setup()
    def _print(msg):
        print msg.body
    consume_items(test_q, _print)

def test_produce(test_q = 'test_q', msg_body = 'hello, world!'):
    _test_setup()
    add_item(test_q, msg_body)
    worker.join()
