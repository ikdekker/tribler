from __future__ import absolute_import

import logging
import socket
import time
from binascii import hexlify

from pony.orm import db_session

from twisted.internet import reactor
from twisted.internet.defer import CancelledError, DeferredList, maybeDeferred, succeed
from twisted.internet.error import ConnectingCancelledError, ConnectionLost
from twisted.python.failure import Failure
from twisted.web.client import HTTPConnectionPool

from Tribler.Core.TorrentChecker.session import FakeDHTSession, UdpSocketManager, create_tracker_session
from Tribler.Core.Utilities.tracker_utils import MalformedTrackerURLException
from Tribler.Core.Utilities.utilities import is_valid_url
from Tribler.Core.simpledefs import NTFY_TORRENT, NTFY_UPDATE
from Tribler.pyipv8.ipv8.database import database_blob
from Tribler.pyipv8.ipv8.taskmanager import TaskManager

# some settings
DEFAULT_TORRENT_SELECTION_INTERVAL = 20  # every 20 seconds, the thread will select torrents to check
DEFAULT_TORRENT_CHECK_INTERVAL = 900  # base multiplier for the check delay

DEFAULT_MAX_TORRENT_CHECK_RETRIES = 8  # max check delay increments when failed.
DEFAULT_TORRENT_CHECK_RETRY_INTERVAL = 30  # interval when the torrent was successfully checked for the last time


class TorrentChecker(TaskManager):

    def __init__(self, session):
        super(TorrentChecker, self).__init__()
        self._logger = logging.getLogger(self.__class__.__name__)
        self.tribler_session = session

        self._should_stop = False

        self._torrent_check_interval = DEFAULT_TORRENT_CHECK_INTERVAL
        self._torrent_check_retry_interval = DEFAULT_TORRENT_CHECK_RETRY_INTERVAL
        self._max_torrent_check_retries = DEFAULT_MAX_TORRENT_CHECK_RETRIES

        self._session_list = {'DHT': []}
        self._last_torrent_selection_time = 0

        # Track all session cleanups
        self.session_stop_defer_list = []

        self.socket_mgr = self.udp_port = None
        self.connection_pool = None

    def initialize(self):
        self._reschedule_tracker_select()
        self.connection_pool = HTTPConnectionPool(reactor, False)
        self.socket_mgr = UdpSocketManager()
        self.create_socket_or_schedule()

    def listen_on_udp(self):
        return reactor.listenUDP(0, self.socket_mgr)

    def create_socket_or_schedule(self):
        """
        This method attempts to bind to a UDP port. If it fails for some reason (i.e. no network connection), we try
        again later.
        """
        try:
            self.udp_port = self.listen_on_udp()
        except socket.error as exc:
            self._logger.error("Error when creating UDP socket in torrent checker: %s", exc)
            self.register_task("listen_udp_port", reactor.callLater(10, self.create_socket_or_schedule))

    def shutdown(self):
        """
        Shutdown the torrent health checker.

        Once shut down it can't be started again.
        :returns A deferred that will fire once the shutdown has completed.
        """
        self._should_stop = True

        if self.udp_port:
            self.session_stop_defer_list.append(maybeDeferred(self.udp_port.stopListening))
            self.udp_port = None

        if self.connection_pool:
            self.session_stop_defer_list.append(self.connection_pool.closeCachedConnections())

        self.shutdown_task_manager()

        # kill all the tracker sessions.
        # Wait for the defers to all have triggered by using a DeferredList
        for tracker_url in self._session_list.keys():
            for session in self._session_list[tracker_url]:
                self.session_stop_defer_list.append(session.cleanup())

        return DeferredList(self.session_stop_defer_list)

    def _reschedule_tracker_select(self):
        """
        Changes the tracker selection interval dynamically and schedules the task.
        """
        self.register_task(u"torrent_checker_tracker_selection",
                           reactor.callLater(DEFAULT_TORRENT_SELECTION_INTERVAL, self._task_select_tracker))

    def _task_select_tracker(self):
        """
        The regularly scheduled task that selects torrents associated with a specific tracker to check.
        """

        # update the torrent selection interval
        self._reschedule_tracker_select()

        # start selecting torrents
        tracker_url = self.get_valid_next_tracker_for_auto_check()
        if tracker_url is None:
            self._logger.warn(u"No tracker to select from, skip")
            return succeed(None)

        self._logger.debug(u"Start selecting torrents on tracker %s.", tracker_url)

        # get the torrents that should be checked
        infohashes = []
        with db_session:
            tracker = self.tribler_session.lm.mds.TrackerState.get(url=tracker_url)
            if tracker:
                torrents = tracker.torrents
                for torrent in torrents:
                    dynamic_interval = self._torrent_check_retry_interval * (2 ** tracker.failures)
                    if torrent.last_check + dynamic_interval < int(time.time()):
                        infohashes.append(torrent.infohash)

        if len(infohashes) == 0:
            # We have no torrent to recheck for this tracker. Still update the last_check for this tracker.
            self._logger.info("No torrent to check for tracker %s", tracker_url)
            self.update_tracker_info(tracker_url, True)
            return succeed(None)
        elif tracker_url != u'DHT' and tracker_url != u'no-DHT':
            try:
                session = self._create_session_for_request(tracker_url, timeout=30)
            except MalformedTrackerURLException as e:
                # Remove the tracker from the database
                self.remove_tracker(tracker_url)
                self._logger.error(e)
                return succeed(None)

            for infohash in infohashes:
                session.add_infohash(infohash)

            self._logger.info(u"Selected %d new torrents to check on tracker: %s", len(infohashes), tracker_url)
            return session.connect_to_tracker().addCallbacks(*self.get_callbacks_for_session(session)) \
                .addErrback(lambda _: None)

    def get_callbacks_for_session(self, session):
        success_lambda = lambda info_dict: self._on_result_from_session(session, info_dict)
        error_lambda = lambda failure: self.on_session_error(session, failure)
        return success_lambda, error_lambda

    def get_valid_next_tracker_for_auto_check(self):
        tracker_url = self.get_next_tracker_for_auto_check()
        while tracker_url and not is_valid_url(tracker_url):
            self.remove_tracker(tracker_url)
            tracker_url = self.get_next_tracker_for_auto_check()
        return tracker_url

    def get_next_tracker_for_auto_check(self):
        return self.tribler_session.lm.tracker_manager.get_next_tracker_for_auto_check()

    def remove_tracker(self, tracker_url):
        self.tribler_session.lm.tracker_manager.remove_tracker(tracker_url)

    def update_tracker_info(self, tracker_url, value):
        self.tribler_session.lm.tracker_manager.update_tracker_info(tracker_url, value)

    @db_session
    def get_valid_trackers_of_torrent(self, torrent_id):
        """ Get a set of valid trackers for torrent. Also remove any invalid torrent."""
        db_tracker_list = self.tribler_session.lm.mds.TorrentState.get(infohash=database_blob(torrent_id)).trackers
        return set([str(tracker.url) for tracker in db_tracker_list if is_valid_url(str(tracker.url))])

    def on_gui_request_completed(self, infohash, result):
        final_response = {}

        torrent_update_dict = {'infohash': infohash, 'seeders': 0, 'leechers': 0, 'last_check': int(time.time())}
        for success, response in reversed(result):
            if not success and isinstance(response, Failure):
                final_response[response.tracker_url] = {'error': response.getErrorMessage()}
                continue
            final_response[response.keys()[0]] = response[response.keys()[0]][0]

            s = response[response.keys()[0]][0]['seeders']
            l = response[response.keys()[0]][0]['leechers']

            # More leeches is better, because undefined peers are marked as leeches in DHT
            if s > torrent_update_dict['seeders'] or \
                    (s == torrent_update_dict['seeders'] and l > torrent_update_dict['leechers']):
                torrent_update_dict['seeders'] = s
                torrent_update_dict['leechers'] = l

        self._update_torrent_result(torrent_update_dict)

        # Add this result to popularity community to publish to subscribers
        self.publish_torrent_result(torrent_update_dict)

        # TODO: DRY! Stop doing lots of formats, just make REST endpoint automatically encode binary data to hex!
        self.tribler_session.notifier.notify(NTFY_TORRENT, NTFY_UPDATE, infohash,
                                             {"num_seeders": torrent_update_dict["seeders"],
                                              "num_leechers": torrent_update_dict["leechers"],
                                              "last_tracker_check": torrent_update_dict["last_check"],
                                              "health": "updated"})
        return final_response

    def add_gui_request(self, infohash, timeout=20, scrape_now=False):
        """
        Public API for adding a GUI request.
        :param infohash: Torrent infohash.
        :param timeout: The timeout to use in the performed requests
        :param scrape_now: Flag whether we want to force scraping immediately
        """
        tracker_set = []

        # We first check whether the torrent is already in the database and checked before
        with db_session:
            result = self.tribler_session.lm.mds.TorrentState.get(infohash=database_blob(infohash))
            if result:
                torrent_id = str(result.infohash)
                last_check = result.last_check
                time_diff = time.time() - last_check
                if time_diff < self._torrent_check_interval and not scrape_now:
                    self._logger.debug(u"time interval too short, skip GUI request. infohash: %s", hexlify(infohash))
                    return succeed({
                        "db": {
                            "seeders": result.seeders,
                            "leechers": result.leechers,
                            "infohash": hexlify(infohash)
                        }
                    })

                # get torrent's tracker list from DB
                tracker_set = self.get_valid_trackers_of_torrent(torrent_id)

        deferred_list = []
        for tracker_url in tracker_set:
            session = self._create_session_for_request(tracker_url, timeout=timeout)
            session.add_infohash(infohash)
            deferred_list.append(session.connect_to_tracker().
                                 addCallbacks(*self.get_callbacks_for_session(session)))

        # Create a (fake) DHT session for the lookup
        session = FakeDHTSession(self.tribler_session, infohash, timeout)
        self._session_list['DHT'].append(session)
        deferred_list.append(session.connect_to_tracker().
                             addCallbacks(*self.get_callbacks_for_session(session)))

        return DeferredList(deferred_list, consumeErrors=True).addCallback(
            lambda res: self.on_gui_request_completed(infohash, res))

    def on_session_error(self, session, failure):
        """
        Handles the scenario of when a tracker session has failed by calling the
        tracker_manager's update_tracker_info function.
        Trap value errors that are thrown by e.g. the HTTPTrackerSession when a connection fails.
        And trap CancelledErrors that can be thrown when shutting down.
        :param failure: The failure object raised by Twisted.
        """
        failure.trap(ValueError, CancelledError, ConnectingCancelledError, ConnectionLost, RuntimeError)
        self._logger.warning(u"Got session error for URL %s: %s", session.tracker_url,
                             str(failure).replace(u'\n]', u']'))

        self.clean_session(session)

        # Do not update if the connection got cancelled, we are probably shutting down
        # and the tracker_manager may have shutdown already.
        if failure.check(CancelledError, ConnectingCancelledError) is None:
            self.tribler_session.lm.tracker_manager.update_tracker_info(session.tracker_url, False)

        failure.tracker_url = session.tracker_url
        return failure

    def _create_session_for_request(self, tracker_url, timeout=20):
        session = create_tracker_session(tracker_url, timeout, self.socket_mgr, connection_pool=self.connection_pool)

        if tracker_url not in self._session_list:
            self._session_list[tracker_url] = []
        self._session_list[tracker_url].append(session)

        self._logger.debug(u"Session created for tracker %s", tracker_url)
        return session

    def clean_session(self, session):
        self.tribler_session.lm.tracker_manager.update_tracker_info(session.tracker_url, not session.is_failed)
        self.session_stop_defer_list.append(session.cleanup())

        # Remove the session from our session list dictionary
        self._session_list[session.tracker_url].remove(session)
        if len(self._session_list[session.tracker_url]) == 0 and session.tracker_url != u"DHT":
            del self._session_list[session.tracker_url]

    def _on_result_from_session(self, session, result_list):
        if self._should_stop:
            return

        self.clean_session(session)

        return result_list

    def _update_torrent_result(self, response):
        infohash = response['infohash']
        seeders = response['seeders']
        leechers = response['leechers']
        last_check = response['last_check']

        self._logger.debug(u"Update result %s/%s for %s", seeders, leechers, hexlify(infohash))

        with db_session:
            # Update torrent state
            torrent = self.tribler_session.lm.mds.TorrentState.get(infohash=database_blob(infohash))
            if not torrent:
                # Something is wrong, there should exist a corresponding TorrentState entry in the DB.
                return
            torrent.seeders = seeders
            torrent.leechers = leechers
            torrent.last_check = last_check

    def publish_torrent_result(self, response):
        if response['seeders'] == 0:
            self._logger.info("Not publishing zero seeded torrents")
            return
        content = (response['infohash'], response['seeders'], response['leechers'], response['last_check'])
        if self.tribler_session.lm.popularity_community:
            self.tribler_session.lm.popularity_community.queue_content(content)
        else:
            self._logger.info("Popular community not available to publish torrent checker result")
