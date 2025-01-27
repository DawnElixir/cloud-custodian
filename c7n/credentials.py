# Copyright The Cloud Custodian Authors.
# SPDX-License-Identifier: Apache-2.0
"""
Authentication utilities
"""
import threading
import os

from botocore.credentials import RefreshableCredentials
from botocore.session import get_session
from boto3 import Session
import json

from c7n.version import version
from c7n.utils import get_retry


# we still have some issues (see #5023) to work through to switch to
# default regional endpoints, for now its opt-in.
USE_STS_REGIONAL = os.environ.get(
    'C7N_USE_STS_REGIONAL', '').lower() in ('yes', 'true')


class CustodianSession(Session):

    # track clients and return extant ones if present
    _clients = {}
    lock = threading.Lock()

    def client(self, service_name, region_name=None, *args, **kw):
        if kw.get('config'):
            return super().client(service_name, region_name, *args, **kw)

        key = self._cache_key(service_name, region_name)
        client = self._clients.get(key)
        if client is not None:
            return client

        with self.lock:
            client = self._clients.get(key)
            if client is not None:
                return client

            client = super().client(service_name, region_name, *args, **kw)
            self._clients[key] = client
            return client

    def _cache_key(self, service_name, region_name):
        region_name = region_name or self.region_name
        return (
            # namedtuple so stable comparison
            hash(self.get_credentials().get_frozen_credentials()),
            service_name,
            region_name
        )

    @classmethod
    def close(cls):
        with cls.lock:
            for c in cls._clients.values():
                c.close()
            cls._clients = {}


class SessionFactory:

    def __init__(
            self, region, profile=None, assume_role=None, external_id=None, session_policy=None):
        self.region = region
        self.profile = profile
        self.session_policy = session_policy
        self.assume_role = assume_role
        self.external_id = external_id
        self.session_name = "AccountResourceManager"
        if 'C7N_SESSION_SUFFIX' in os.environ:
            self.session_name = "%s@%s" % (
                self.session_name, os.environ['C7N_SESSION_SUFFIX'])
        self._subscribers = []
        self._policy_name = ""

    def _set_policy_name(self, name):
        self._policy_name = name

    policy_name = property(None, _set_policy_name)

    def __call__(self, assume=True, region=None):
        if self.assume_role and assume:
            session = Session(profile_name=self.profile)
            session = assumed_session(
                self.assume_role, self.session_name, self.session_policy, session,
                region or self.region, self.external_id)
        else:
            session = Session(
                region_name=region or self.region, profile_name=self.profile)

        return self.update(session)

    def update(self, session):
        session._session.user_agent_name = "c7n"
        session._session.user_agent_version = version
        if self._policy_name:
            session._session.user_agent_extra = f"ARM/policy#{self._policy_name}"

        for s in self._subscribers:
            s(session)

        return session

    def set_subscribers(self, subscribers):
        self._subscribers = subscribers


def assumed_session(
        role_arn, session_name, session_policy=None, session=None, region=None, external_id=None):
    """STS Role assume a boto3.Session

    With automatic credential renewal.

    Args:
      role_arn: iam role arn to assume
      session_name: client session identifier
      session: an optional extant session, note session is captured
      in a function closure for renewing the sts assumed role.

    :return: a boto3 session using the sts assumed role credentials

    Notes: We have to poke at botocore internals a few times
    """
    if session is None:
        session = Session()

    retry = get_retry(('Throttling',))

    def refresh():

        parameters = {"RoleArn": role_arn, "RoleSessionName": session_name}
        if session_policy is not None:
            parameters['Policy'] = json.dumps(session_policy)

        if external_id is not None:
            parameters['ExternalId'] = external_id

        credentials = retry(
            get_sts_client(
                session, region).assume_role, **parameters)['Credentials']
        return dict(
            access_key=credentials['AccessKeyId'],
            secret_key=credentials['SecretAccessKey'],
            token=credentials['SessionToken'],
            # Silly that we basically stringify so it can be parsed again
            expiry_time=credentials['Expiration'].isoformat())

    session_credentials = RefreshableCredentials.create_from_metadata(
        metadata=refresh(),
        refresh_using=refresh,
        method='sts-assume-role')

    # so dirty.. it hurts, no clean way to set this outside of the
    # internals poke. There's some work upstream on making this nicer
    # but its pretty baroque as well with upstream support.
    # https://github.com/boto/boto3/issues/443
    # https://github.com/boto/botocore/issues/761

    s = get_session()
    s._credentials = session_credentials
    if region is None:
        region = s.get_config_variable('region') or 'us-east-1'
    s.set_config_variable('region', region)
    return Session(botocore_session=s)


def get_sts_client(session, region):
    """Get the AWS STS endpoint specific for the given region.

    Returns the global endpoint if region is not specified.

    For the list of regional endpoints, see https://amzn.to/2ohJgtR
    """
    if region and USE_STS_REGIONAL:
        endpoint_url = "https://sts.{}.amazonaws.com".format(region)
        region_name = region
    else:
        endpoint_url = None
        region_name = None
    return session.client(
        'sts', endpoint_url=endpoint_url, region_name=region_name)
