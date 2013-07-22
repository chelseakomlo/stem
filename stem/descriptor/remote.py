# Copyright 2013, Damian Johnson
# See LICENSE for licensing information

"""
Module for remotely retrieving descriptors from directory authorities and
mirrors. This is most easily done through the
:class:`~stem.descriptor.remote.DescriptorDownloader` class, which issues
:class:`~stem.descriptor.remote.Query` instances to get you the descriptor
content. For example...

::

  from stem.descriptor.remote import DescriptorDownloader

  downloader = DescriptorDownloader(
    use_mirrors = True,
    timeout = 10,
  )

  query = downloader.get_server_descriptors()

  print "Exit Relays:"

  try:
    for desc in query.run():
      if desc.exit_policy.is_exiting_allowed():
        print "  %s (%s)" % (desc.nickname, desc.fingerprint)

    print
    print "Query took %0.2f seconds" % query.runtime
  except Exception as exc:
    print "Unable to retrieve the server descriptors: %s" % exc

If you don't care about errors then you can also simply iterate over the query
itself...

::

  for desc in downloader.get_server_descriptors():
    if desc.exit_policy.is_exiting_allowed():
      print "  %s (%s)" % (desc.nickname, desc.fingerprint)

::

  Query - Asynchronous request to download tor descriptors
    |- start - issues the query if it isn't already running
    +- run - blocks until the request is finished and provides the results

  DescriptorDownloader - Configurable class for issuing queries
    |- use_directory_mirrors - use directory mirrors to download future descriptors
    |- get_server_descriptors - provides present server descriptors
    |- get_extrainfo_descriptors - provides present extrainfo descriptors
    |- get_microdescriptors - provides present microdescriptors
    |- get_consensus - provides the present consensus or router status entries
    |- get_key_certificates - provides present authority key certificates
    +- query - request an arbitrary descriptor resource

.. data:: MAX_FINGERPRINTS

  Maximum number of descriptors that can requested at a time by their
  fingerprints.

.. data:: MAX_MICRODESCRIPTOR_HASHES

  Maximum number of microdescriptors that can requested at a time by their
  hashes.

.. data:: DIRECTORY_AUTHORITIES

  Mapping of directory authority nicknames to their (address, dirport) tuple.
"""

import io
import random
import sys
import threading
import time
import urllib2
import zlib

import stem.descriptor

from stem import Flag
from stem.util import log

# Tor has a limited number of descriptors we can fetch explicitly by their
# fingerprint or hashes due to a limit on the url length by squid proxies.

MAX_FINGERPRINTS = 96
MAX_MICRODESCRIPTOR_HASHES = 92

# Tor directory authorities as of commit f631b73 (7/4/13). This should only
# include authorities with 'v3ident':
#
# https://gitweb.torproject.org/tor.git/blob/f631b73:/src/or/config.c#l816

DIRECTORY_AUTHORITIES = {
  'moria1': ('128.31.0.39', 9131),
  'tor26': ('86.59.21.38', 80),
  'dizum': ('194.109.206.212', 80),
  'turtles': ('76.73.17.194', 9030),
  'gabelmoo': ('212.112.245.170', 80),
  'dannenberg': ('193.23.244.244', 80),
  'urras': ('208.83.223.34', 443),
  'maatuska': ('171.25.193.9', 443),
  'Faravahar': ('154.35.32.5', 80),
}


def _guess_descriptor_type(resource):
  # Attempts to determine the descriptor type based on the resource url. This
  # raises a ValueError if the resource isn't recognized.

  if resource.startswith('/tor/server/'):
    return 'server-descriptor 1.0'
  elif resource.startswith('/tor/extra/'):
    return 'extra-info 1.0'
  elif resource.startswith('/tor/micro/'):
    return 'microdescriptor 1.0'
  elif resource.startswith('/tor/status-vote/'):
    return 'network-status-consensus-3 1.0'
  elif resource.startswith('/tor/keys/'):
    return 'dir-key-certificate-3 1.0'
  else:
    raise ValueError("Unable to determine the descriptor type for '%s'" % resource)


class Query(object):
  """
  Asynchronous request for descriptor content from a directory authority or
  mirror. These can either be made through the
  :class:`~stem.descriptor.remote.DescriptorDownloader` or directly for more
  advanced usage.

  To block on the response and get results either call
  :func:`~stem.descriptor.remote.Query.run` or iterate over the Query. The
  :func:`~stem.descriptor.remote.Query.run` method pass along any errors that
  arise...

  ::

    from stem.descriptor.remote import Query

    query = Query(
      '/tor/server/all.z',
      descriptor_type = 'server-descriptor 1.0',
      timeout = 30,
    )

    print "Current relays:"

    try:
      for desc in query.run():
        print desc.fingerprint
    except Exception as exc:
      print "Unable to retrieve the server descriptors: %s" % exc

  ... while iterating fails silently...

  ::

    print "Current relays:"

    for desc in Query('/tor/server/all.z', 'server-descriptor 1.0'):
      print desc.fingerprint

  In either case exceptions are available via our 'error' attribute.

  Tor provides quite a few different descriptor resources via its directory
  protocol (see section 4.2 and later of the `dir-spec
  <https://gitweb.torproject.org/torspec.git/blob/HEAD:/dir-spec.txt>`_).
  Commonly useful ones include...

  ===================================== ===========
  Resource                              Description
  ===================================== ===========
  /tor/server/all.z                     all present server descriptors
  /tor/server/fp/<fp1>+<fp2>+<fp3>.z    server descriptors with the given fingerprints
  /tor/extra/all.z                      all present extrainfo descriptors
  /tor/extra/fp/<fp1>+<fp2>+<fp3>.z     extrainfo descriptors with the given fingerprints
  /tor/micro/d/<hash1>-<hash2>.z        microdescriptors with the given hashes
  /tor/status-vote/current/consensus.z  present consensus
  /tor/keys/all.z                       key certificates for the authorities
  /tor/keys/fp/<v3ident1>+<v3ident2>.z  key certificates for specific authorities
  ===================================== ===========

  The '.z' suffix can be excluded to get a plaintext rather than compressed
  response. Compression is handled transparently, so this shouldn't matter to
  the caller.

  :var str resource: resource being fetched, such as '/tor/server/all.z'
  :var str descriptor_type: type of descriptors being fetched (for options see
    :func:`~stem.descriptor.__init__.parse_file`), this is guessed from the
    resource if **None**

  :var list endpoints: (address, dirport) tuples of the authority or mirror
    we're querying, this uses authorities if undefined
  :var int retries: number of times to attempt the request if downloading it
    fails
  :var bool fall_back_to_authority: when retrying request issues the last
    request to a directory authority if **True**

  :var Exception error: exception if a problem occured
  :var bool is_done: flag that indicates if our request has finished
  :var str download_url: last url used to download the descriptor, this is
    unset until we've actually made a download attempt

  :var float start_time: unix timestamp when we first started running
  :var float timeout: duration before we'll time out our request
  :var float runtime: time our query took, this is **None** if it's not yet
    finished

  :var bool validate: checks the validity of the descriptor's content if
    **True**, skips these checks otherwise
  :var stem.descriptor.__init__.DocumentHandler document_handler: method in
    which to parse a :class:`~stem.descriptor.networkstatus.NetworkStatusDocument`

  :param bool start: start making the request when constructed (default is **True**)
  """

  def __init__(self, resource, descriptor_type = None, endpoints = None, retries = 2, fall_back_to_authority = True, timeout = None, start = True, validate = True, document_handler = stem.descriptor.DocumentHandler.ENTRIES):
    if not resource.startswith('/'):
      raise ValueError("Resources should start with a '/': %s" % resource)

    self.resource = resource

    if descriptor_type:
      self.descriptor_type = descriptor_type
    else:
      self.descriptor_type = _guess_descriptor_type(resource)

    self.endpoints = endpoints if endpoints else []
    self.retries = retries
    self.fall_back_to_authority = fall_back_to_authority

    self.error = None
    self.is_done = False
    self.download_url = None

    self.start_time = None
    self.timeout = timeout
    self.runtime = None

    self.validate = validate
    self.document_handler = document_handler

    self._downloader_thread = None
    self._downloader_thread_lock = threading.RLock()

    self._results = None  # descriptor iterator

    if start:
      self.start()

  def start(self):
    """
    Starts downloading the scriptors if we haven't started already.
    """

    with self._downloader_thread_lock:
      if self._downloader_thread is None:
        self._downloader_thread = threading.Thread(
          name = "Descriptor Query",
          target = self._download_descriptors,
          args = (self.retries,)
        )

        self._downloader_thread.setDaemon(True)
        self._downloader_thread.start()

  def run(self, suppress = False):
    """
    Blocks until our request is complete then provides the descriptors. If we
    haven't yet started our request then this does so.

    :param bool suppress: avoids raising exceptions if **True**

    :returns: iterator for the requested :class:`~stem.descriptor.__init__.Descriptor` instances

    :raises:
      Using the iterator can fail with the following if **suppress** is
      **False**...

        * **ValueError** if the descriptor contents is malformed
        * **socket.timeout** if our request timed out
        * **urllib2.URLError** for most request failures

      Note that the urllib2 module may fail with other exception types, in
      which case we'll pass it along.
    """

    with self._downloader_thread_lock:
      self.start()
      self._downloader_thread.join()

      if self.error:
        if suppress:
          return

        raise self.error
      else:
        if self._results is None:
          if suppress:
            return

          raise ValueError('BUG: _download_descriptors() finished without either results or an error')

        try:
          for desc in self._results:
            yield desc
        except ValueError as exc:
          self.error = exc  # encountered a parsing error

          if suppress:
            return

          raise self.error

  def __iter__(self):
    for desc in self.run(True):
      yield desc

  def _pick_url(self, use_authority = False):
    """
    Provides a url that can be queried. If we have multiple endpoints then one
    will be picked randomly.

    :param bool use_authority: ignores our endpoints and uses a directory
      authority instead

    :returns: **str** for the url being queried by this request
    """

    if use_authority or not self.endpoints:
      address, dirport = random.choice(DIRECTORY_AUTHORITIES.values())
    else:
      address, dirport = random.choice(self.endpoints)

    return "http://%s:%i/%s" % (address, dirport, self.resource.lstrip('/'))

  def _download_descriptors(self, retries):
    try:
      use_authority = retries == 0 and self.fall_back_to_authority
      self.download_url = self._pick_url(use_authority)

      self.start_time = time.time()
      response = urllib2.urlopen(self.download_url, timeout = self.timeout).read()

      if self.download_url.endswith('.z'):
        response = zlib.decompress(response)

      self._results = stem.descriptor.parse_file(
        io.BytesIO(response.strip()),
        self.descriptor_type,
        validate = self.validate,
        document_handler = self.document_handler,
      )

      self.runtime = time.time() - self.start_time
      log.trace("Descriptors retrieved from '%s' in %0.2fs" % (self.download_url, self.runtime))
    except:
      exc = sys.exc_info()[1]

      if retries > 0:
        log.debug("Unable to download descriptors from '%s' (%i retries remaining): %s" % (self.download_url, retries, exc))
        return self._download_descriptors(retries - 1)
      else:
        log.debug("Unable to download descriptors from '%s': %s" % (self.download_url, exc))
        self.error = exc
    finally:
      self.is_done = True


class DescriptorDownloader(object):
  """
  Configurable class that issues :class:`~stem.descriptor.remote.Query`
  instances on your behalf.

  :param bool use_mirrors: downloads the present consensus and uses the directory
    mirrors to fetch future requests, this fails silently if the consensus
    cannot be downloaded
  :param default_args: default arguments for the
    :class:`~stem.descriptor.remote.Query` constructor
  """

  def __init__(self, use_mirrors = False, **default_args):
    self._default_args = default_args
    self._endpoints = DIRECTORY_AUTHORITIES.values()

    if use_mirrors:
      try:
        start_time = time.time()
        self.use_directory_mirrors()
        log.debug("Retrieve directory mirrors (took %0.2fs)" % (time.time() - start_time))
      except Exception as exc:
        log.debug("Unable to retrieve directory mirrors: %s" % exc)

  def use_directory_mirrors(self):
    """
    Downloads the present consensus and configures ourselves to use directory
    mirrors, in addition to authorities.

    :raises: **Exception** if unable to determine the directory mirrors
    """

    new_endpoints = set(DIRECTORY_AUTHORITIES.values())

    for desc in self.get_consensus().run():
      if Flag.V2DIR in desc.flags:
        new_endpoints.add((desc.address, desc.dir_port))

    # we need our endpoints to be a list rather than set for random.choice()

    self._endpoints = list(new_endpoints)

  def get_server_descriptors(self, fingerprints = None, **query_args):
    """
    Provides the server descriptors with the given fingerprints. If no
    fingerprints are provided then this returns all descriptors in the present
    consensus.

    :param str,list fingerprints: fingerprint or list of fingerprints to be
      retrieved, gets all descriptors if **None**
    :param query_args: additional arguments for the
      :class:`~stem.descriptor.remote.Query` constructor

    :returns: :class:`~stem.descriptor.remote.Query` for the server descriptors

    :raises: **ValueError** if we request more than 96 descriptors by their
      fingerprints (this is due to a limit on the url length by squid proxies).
    """

    resource = '/tor/server/all.z'

    if isinstance(fingerprints, str):
      fingerprints = [fingerprints]

    if fingerprints:
      if len(fingerprints) > MAX_FINGERPRINTS:
        raise ValueError("Unable to request more than %i descriptors at a time by their fingerprints" % MAX_FINGERPRINTS)

      resource = '/tor/server/fp/%s.z' % '+'.join(fingerprints)

    return self.query(resource, **query_args)

  def get_extrainfo_descriptors(self, fingerprints = None, **query_args):
    """
    Provides the extrainfo descriptors with the given fingerprints. If no
    fingerprints are provided then this returns all descriptors in the present
    consensus.

    :param str,list fingerprints: fingerprint or list of fingerprints to be
      retrieved, gets all descriptors if **None**
    :param query_args: additional arguments for the
      :class:`~stem.descriptor.remote.Query` constructor

    :returns: :class:`~stem.descriptor.remote.Query` for the extrainfo descriptors

    :raises: **ValueError** if we request more than 96 descriptors by their
      fingerprints (this is due to a limit on the url length by squid proxies).
    """

    resource = '/tor/extra/all.z'

    if isinstance(fingerprints, str):
      fingerprints = [fingerprints]

    if fingerprints:
      if len(fingerprints) > MAX_FINGERPRINTS:
        raise ValueError("Unable to request more than %i descriptors at a time by their fingerprints" % MAX_FINGERPRINTS)

      resource = '/tor/extra/fp/%s.z' % '+'.join(fingerprints)

    return self.query(resource, **query_args)

  def get_microdescriptors(self, hashes, **query_args):
    """
    Provides the microdescriptors with the given hashes. To get these see the
    'microdescriptor_hashes' attribute of
    :class:`~stem.descriptor.router_status_entry.RouterStatusEntryV3`. Note
    that these are only provided via a microdescriptor consensus (such as
    'cached-microdesc-consensus' in your data directory).

    :param str,list hashes: microdescriptor hash or list of hashes to be
      retrieved
    :param query_args: additional arguments for the
      :class:`~stem.descriptor.remote.Query` constructor

    :returns: :class:`~stem.descriptor.remote.Query` for the microdescriptors

    :raises: **ValueError** if we request more than 92 microdescriptors by their
      hashes (this is due to a limit on the url length by squid proxies).
    """

    if isinstance(hashes, str):
      hashes = [hashes]

    if len(hashes) > MAX_MICRODESCRIPTOR_HASHES:
      raise ValueError("Unable to request more than %i microdescriptors at a time by their hashes" % MAX_MICRODESCRIPTOR_HASHES)

    return self.query('/tor/micro/d/%s.z' % '-'.join(hashes), **query_args)

  def get_consensus(self, authority_v3ident = None, document_handler = stem.descriptor.DocumentHandler.ENTRIES, **query_args):
    """
    Provides the present router status entries.

    :param str authority_v3ident: fingerprint of the authority key for which
      to get the consensus, see `'v3ident' in tor's config.c
      <https://gitweb.torproject.org/tor.git/blob/f631b73:/src/or/config.c#l816>`_
      for the values.
    :param stem.descriptor.__init__.DocumentHandler document_handler: method in
      which to parse the :class:`~stem.descriptor.networkstatus.NetworkStatusDocumentV3`
    :param query_args: additional arguments for the
      :class:`~stem.descriptor.remote.Query` constructor

    :returns: :class:`~stem.descriptor.remote.Query` for the router status
      entries
    """

    resource = '/tor/status-vote/current/consensus'

    if authority_v3ident:
      resource += '/%s' % authority_v3ident

    return self.query(resource + '.z', document_handler = document_handler, **query_args)

  def get_key_certificates(self, authority_v3idents = None, **query_args):
    """
    Provides the key certificates for authorities with the given fingerprints.
    If no fingerprints are provided then this returns all present key
    certificates.

    :param str authority_v3idents: fingerprint or list of fingerprints of the
      authority keys, see `'v3ident' in tor's config.c
      <https://gitweb.torproject.org/tor.git/blob/f631b73:/src/or/config.c#l816>`_
      for the values.
    :param query_args: additional arguments for the
      :class:`~stem.descriptor.remote.Query` constructor

    :returns: :class:`~stem.descriptor.remote.Query` for the key certificates

    :raises: **ValueError** if we request more than 96 key certificates by
      their identity fingerprints (this is due to a limit on the url length by
      squid proxies).
    """

    resource = '/tor/keys/all.z'

    if isinstance(authority_v3idents, str):
      authority_v3idents = [authority_v3idents]

    if authority_v3idents:
      if len(authority_v3idents) > MAX_FINGERPRINTS:
        raise ValueError("Unable to request more than %i key certificates at a time by their identity fingerprints" % MAX_FINGERPRINTS)

      resource = '/tor/keys/fp/%s.z' % '+'.join(authority_v3idents)

    return self.query(resource, **query_args)

  def query(self, resource, **query_args):
    """
    Issues a request for the given resource.

    :param str resource: resource being fetched, such as '/tor/server/all.z'
    :param query_args: additional arguments for the
      :class:`~stem.descriptor.remote.Query` constructor

    :returns: :class:`~stem.descriptor.remote.Query` for the descriptors

    :raises: **ValueError** if resource is clearly invalid or the descriptor
      type can't be determined when 'descriptor_type' is **None**
    """

    args = dict(self._default_args)
    args.update(query_args)

    return Query(
      resource,
      endpoints = self._endpoints,
      **args
    )