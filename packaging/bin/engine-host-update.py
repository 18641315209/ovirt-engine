#!/usr/bin/python -u

#
# engine-host-update - oVirt engine host update utility
# Copyright (C) 2017 Red Hat, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

from __future__ import print_function

import atexit
import getopt
import logging
import os
import sys
import time

try:
    import ovirtsdk.api
    import ovirtsdk.xml
except ImportError:
    print(
        'This tools requires oVirt Python SDK v3.\n'
        'Please install it with e.g. yum install ovirt-engine-sdk-python\n'
    )
    sys.exit(1)


# Host states
# enum Reference: backend/manager/modules/common/src/main/java/org/ovirt
# /engine/core/common/businessentities/VDSStatus.java
# 2
HOST_STATE_MAINTENANCE = 'maintenance'
# 3
HOST_STATE_UP = 'up'
# 4
HOST_STATE_NON_RESPONSIVE = 'non_responsive'
# 5
HOST_STATE_ERROR = 'error'
# 6
HOST_STATE_INSTALLING = 'installing'
# 7
HOST_STATE_INSTALL_FAILED = 'install_failed'
# 8
HOST_STATE_REBOOT = 'reboot'
# 9
HOST_STATE_PREPARING_FOR_MAINT = 'preparing_for_maintenance'
# 10
HOST_STATE_NON_OPERATIONAL = 'non_operational'

HOST_INSTALL_FAILED_STATES = [
    HOST_STATE_INSTALL_FAILED,
    HOST_STATE_NON_RESPONSIVE,
    HOST_STATE_NON_OPERATIONAL,
    HOST_STATE_ERROR,
]

OVIRT_NODE_LEGACY_HOST_TYPES = (
    'rhev-h',
    'RHEV_H',
)

OVIRT_NODE_HOST_TYPES = (
    'ovirt-node',
    'ovirt_node',
    'OVIRT_NODE',
)

# Default connection params.
defaultEngineFqdn = 'engine'
defaultPort = 443
defaultUsername = 'admin@internal'

# All timeouts are in seconds
connectionTimeout = 30
activationTimeout = 900
maintenanceTimeout = 900
waitForInstallTimeout = 90
installProcessTimeout = 900
waitForUpgradeTimeout = 90
upgradeInstallTimeout = 900
upgradeRebootTimeout = 900

# Wait times are in seconds
HOST_UP_VERIFY_TIME = 90
SLEEP_TIME = 5

# Max tries
MAX_NON_RESPONSIVE_COUNT = 10

ENV_ADMIN_USER = 'OVIRT_ADMIN_USER'
ENV_ADMIN_PASS = 'OVIRT_ADMIN_PASS'
PASSWORD_FILE = '~/.host_update.cred'

hostsToUpdate = []
clustersToUpdate = []


class TimeoutError(Exception):
    def __init__(self, value):
        self.value = value

    def __str__(self):
        return repr(self.value)


class InvalidState(Exception):
    def __init__(self, value):
        self.value = value

    def __str__(self):
        return repr(self.value)


class InvalidHostName(Exception):
    def __init__(self, value):
        self.value = value

    def __str__(self):
        return repr(self.value)


def connect():
    """
    Connects to the oVirt/RHEV engine.
    """
    api_params = {
        'url': 'https://{engineFqdn}:{port}/ovirt-engine/api'.format(
            engineFqdn=engineFqdn,
            port=port,
        ),
        'username': username,
        'password': password,
        'timeout': connectionTimeout,
    }
    if insecure:
        api_params['insecure'] = True
        api_params['validate_cert_chain'] = False
    else:
        api_params['ca_file'] = ca

    logging.debug('API params: ' + str(api_params))
    try:
        api = ovirtsdk.api.API(**api_params)
    except ovirtsdk.infrastructure.errors.RequestError as err:
        if err.status == 401:
            print(
                'Authorization error. Invalid admin username and/or password.'
            )
            sys.exit(1)
    except ovirtsdk.infrastructure.errors.ConnectionError as err:
        print(
            'Error connecting to the engine at https://%s:%s' % (
                engineFqdn,
                port,
            )
        )
        logging.debug(repr(err))
        sys.exit(1)
    logging.debug('Opened connection.')
    atexit.register(disconnect)
    return api


def activateHost(
        api,
        name,
        activationTimeout=activationTimeout,
        skipInvalidHostNames=False,
):
    """
    Activate (move from maintenance) oVirt/RHEV host.
    """
    if api.hosts.list(name=name):
        host = api.hosts.list(name=name)[0]
        state = getHostState(api, name)
    else:
        if skipInvalidHostNames:
            return
        else:
            raise InvalidHostName(
                'Invalid host name %s.' % name
            )

    if state == HOST_STATE_MAINTENANCE:
        print('\tActivating host', end='')
        host.activate()
        secs = 0

        while True:
            print('.', end='')
            time.sleep(SLEEP_TIME)
            secs += SLEEP_TIME
            if secs > activationTimeout:
                raise TimeoutError('Timed out activating host.')
            state = getHostState(api, name)
            if state == HOST_STATE_UP:
                print('\n\tHost activated.')
                break


def deactivateHost(
        api,
        name,
        maintenanceTimeout=maintenanceTimeout,
        skipInvalidHostNames=False,
):
    """
    Deactivate (move to the maintenance) oVirt/RHEV host.
    """
    if api.hosts.list(name=name):
        host = api.hosts.list(name=name)[0]
        state = getHostState(api, name)
    else:
        if skipInvalidHostNames:
            return
        else:
            raise InvalidHostName(
                'Invalid host name %s.' % name
            )

    if state == HOST_STATE_UP:
        print('\tMoving host to the maintenance', end='')
        host.deactivate()
        secs = 0

        while True:
            print('.', end='')
            time.sleep(SLEEP_TIME)
            secs += SLEEP_TIME
            if secs > maintenanceTimeout:
                raise TimeoutError(
                    'Timed out while moving host to maintenance.'
                )
            state = getHostState(api, name)
            if state == HOST_STATE_MAINTENANCE:
                print('\n\tHost moved to maintenance.')
                break


def reinstallHost(
        api,
        name,
        waitForInstallTimeout=waitForInstallTimeout,
        installProcessTimeout=installProcessTimeout,
        skipInvalidHostNames=False,
):
    """
    Perform re-installation of oVirt/RHEV RHEL/oVirt-Node host.

    Expects the host to be in the maintenance, otherwise it raises
    an InvalidState exception.
    """
    if api.hosts.list(name=name):
        host = api.hosts.list(name=name)[0]
        state = getHostState(api, name)
    else:
        if skipInvalidHostNames:
            return
        else:
            raise InvalidHostName(
                'Invalid host name %s.' % name
            )

    if state == HOST_STATE_MAINTENANCE:
        host.install(
            ovirtsdk.xml.params.Action(
                ssh=ovirtsdk.xml.params.SSH(
                    authentication_method='publickey'
                ),
                host=ovirtsdk.xml.params.Host(override_iptables=True),
            )
        )
        secs = 0
        while True:
            state = getHostState(api, name)
            if state == HOST_STATE_INSTALLING:
                print('\tInstalling', end='')
                break
            elif state in HOST_INSTALL_FAILED_STATES:
                raise RuntimeError(
                    'Unable to complete the reinstall operational, '
                    'host is in mode: {0}'.format(state)
                )

            time.sleep(SLEEP_TIME)
            secs += SLEEP_TIME
            if secs > waitForInstallTimeout:
                raise TimeoutError(
                    'Timed out while waiting for host to begin installation.'
                )

        secs = 0
        while True:
            print('.', end='')
            state = getHostState(api, name)
            if state == HOST_STATE_MAINTENANCE:
                print("\n\tInstalled.")
                break
            elif state in HOST_INSTALL_FAILED_STATES:
                raise RuntimeError(
                    'Unable to complete the reinstall operational, '
                    'host is in mode: {0}'.format(state)
                )

            time.sleep(SLEEP_TIME)
            secs += SLEEP_TIME
            if secs > installProcessTimeout:
                raise TimeoutError('Timed out during host installation.')

    else:
        raise InvalidState(
            'Host must be in maintenance mode before attemting an upgrade.'
        )


def upgradeoVirtNodeLegacy(
        api,
        name,
        waitForUpgradeTimeout=waitForUpgradeTimeout,
        upgradeInstallTimeout=upgradeInstallTimeout,
        upgradeRebootTimeout=upgradeRebootTimeout,
        skipInvalidHostNames=False,
):
    """
    Performs upgrade of oVirt Legacy node.

    Expects the host to be in the up or maintenance, otherwise it raises
    an InvalidState exception.
    """
    if api.hosts.list(name=name):
        host = api.hosts.list(name=name)[0]
        state = getHostState(api, name)
    else:
        if skipInvalidHostNames:
            return
        else:
            raise InvalidHostName(
                'Invalid host name %s.' % name
            )
    if state == HOST_STATE_UP:
        host.upgrade(
            ovirtsdk.xml.params.Action(
                image='rhev-hypervisor.iso',
            )
        )
        secs = 0
        while True:
            state = getHostState(api, name)
            if state == HOST_STATE_INSTALLING:
                print('\tInstalling', end='')
                break
            elif state in HOST_INSTALL_FAILED_STATES:
                raise RuntimeError(
                    'Unable to complete the reinstall operational, '
                    'host is in mode: {0}'.format(state)
                )

            time.sleep(SLEEP_TIME)
            secs += SLEEP_TIME
            if secs > waitForUpgradeTimeout:
                raise TimeoutError(
                    'Timed out while waiting for host to begin installation.'
                )

        secs = 0
        while True:
            print('.', end='')
            state = getHostState(api, name)
            if state == HOST_STATE_REBOOT:
                print("\n\tRebooting.", end='')
                break
            elif state in HOST_INSTALL_FAILED_STATES:
                raise RuntimeError(
                    'Unable to complete the reinstall operational, '
                    'host is in mode: {0}'.format(state)
                )

            time.sleep(SLEEP_TIME)
            secs += SLEEP_TIME
            if secs > upgradeInstallTimeout:
                raise TimeoutError(
                    'Timed out while waiting for host to be re-installed.'
                )

        secs = 0
        nonResponsiveCounter = 0
        while True:
            print('.', end='')
            state = getHostState(api, name)
            if state == HOST_STATE_UP:
                print("\n\tInstalled.")
                break
            elif state in HOST_INSTALL_FAILED_STATES:
                print('*', end='')
                nonResponsiveCounter += 1
                if nonResponsiveCounter >= MAX_NON_RESPONSIVE_COUNT:
                    raise RuntimeError(
                        'Unable to complete the reinstall operational, '
                        'host is in mode: {0}'.format(state)
                    )

            time.sleep(SLEEP_TIME)
            secs += SLEEP_TIME
            if secs > upgradeRebootTimeout:
                raise TimeoutError(
                    'Timed out while waiting for host to reboot and activate.'
                )

    else:
        raise InvalidState(
            'Host must be up before attemting an upgrade.'
        )


def upgradeoVirtNode(
        api,
        name,
        waitForUpgradeTimeout=waitForUpgradeTimeout,
        upgradeInstallTimeout=upgradeInstallTimeout,
        upgradeRebootTimeout=upgradeRebootTimeout,
        skipInvalidHostNames=False,
):
    """
    Performs upgrade of oVirt NGN node.

    Expects the host to be in the up or maintenance, otherwise it raises
    an InvalidState exception.
    """
    if api.hosts.list(name=name):
        host = api.hosts.list(name=name)[0]
        state = getHostState(api, name)
    else:
        if skipInvalidHostNames:
            return
        else:
            raise InvalidHostName(
                'Invalid host name %s.' % name
            )
    if state == HOST_STATE_UP:
        try:
            host.upgrade()
        except ovirtsdk.infrastructure.errors.RequestError as err:
            if err.status == 409:
                print(
                    '\tCannot upgrade Host. '
                    'There are no available updates for the host.'
                )
                return
        secs = 0
        while True:
            state = getHostState(api, name)
            if state == HOST_STATE_INSTALLING:
                print('\tInstalling', end='')
                break
            elif state in HOST_INSTALL_FAILED_STATES:
                raise RuntimeError(
                    'Unable to complete the reinstall operational, '
                    'host is in mode: {0}'.format(state)
                )

            time.sleep(SLEEP_TIME)
            secs += SLEEP_TIME
            if secs > waitForUpgradeTimeout:
                raise TimeoutError(
                    'Timed out while waiting for host to begin installation.'
                )

        secs = 0
        while True:
            print('.', end='')
            state = getHostState(api, name)
            if state == HOST_STATE_REBOOT:
                print("\n\tRebooting.", end='')
                break
            elif state in HOST_INSTALL_FAILED_STATES:
                raise RuntimeError(
                    'Unable to complete the reinstall operational, '
                    'host is in mode: {0}'.format(state)
                )

            time.sleep(SLEEP_TIME)
            secs += SLEEP_TIME
            if secs > upgradeInstallTimeout:
                raise TimeoutError(
                    'Timed out while waiting for host to be re-installed.'
                )

        secs = 0
        nonResponsiveCounter = 0
        while True:
            print('.', end='')
            state = getHostState(api, name)
            if state == HOST_STATE_UP:
                print("\n\tInstalled.")
                break
            elif state in HOST_INSTALL_FAILED_STATES:
                print('*', end='')
                nonResponsiveCounter += 1
                if nonResponsiveCounter >= MAX_NON_RESPONSIVE_COUNT:
                    raise RuntimeError(
                        'Unable to complete the reinstall operational, '
                        'host is in mode: {0}'.format(state)
                    )

            time.sleep(SLEEP_TIME)
            secs += SLEEP_TIME
            if secs > upgradeRebootTimeout:
                raise TimeoutError(
                    'Timed out while waiting for host to reboot and activate.'
                )

    else:
        raise InvalidState(
            'Host must be up before attemting an upgrade.'
        )


def verifyHost(api, name, skipInvalidHostNames=False):
    """
    Verifies that oVirt/RHEV host functions properly e.g. stays 'up'.

    Generally host may move to the 'up' state momentarily, only to go down
    due to any number of issues being discovered right after the host began
    functioning.

    This function checks to see if the host stays up for at least for
    HOST_UP_VERIFY_TIME seconds long.

    This function expects to find the host in the 'up' state, otherwise it
    will raise InvalidState exception.

    If the host moves to any other state from 'up' or found in any other
    state but 'up' at the end of the verification perid then InvalidState
    exception will also be raised.
    """
    if api.hosts.list(name=name):
        state = getHostState(api, name)
    else:
        if skipInvalidHostNames:
            return
        else:
            raise InvalidHostName(
                'Invalid host name %s.' % name
            )

    print('\tVerifying that host stays up', end='')
    if state != HOST_STATE_UP:
        raise InvalidState(
            'Invalid host state. It\'s expected to be: %s.' % HOST_STATE_UP
        )

    secs = 0
    while True:
        print('.', end='')
        time.sleep(SLEEP_TIME)
        secs += SLEEP_TIME
        if secs >= HOST_UP_VERIFY_TIME:
            break
        state = getHostState(api, name)
        if state != HOST_STATE_UP:
            raise InvalidState(
                'Host changed it\'s state to: %s '
                'while we expected it to stay as: %s.' %
                (
                    state,
                    HOST_STATE_UP,
                )
            )

    state = getHostState(api, name)
    if state != HOST_STATE_UP:
        raise InvalidState(
            'Host changed it\'s state to: %s '
            'while we expected it to stay as: %s.' %
            (
                state,
                HOST_STATE_UP,
            )
        )
    print('\n\tVerified.')


def processHost(api, name, skipInvalidHostNames):
    """
    Perform a single oVirt/RHEV host re-installation.

    This function will first move the host to the maintenance,
    perform re-installation, then activate and verify the host.
    """
    print("Processing Host: %s" % name)
    if api.hosts.list(name=name):
        host = api.hosts.list(name=name)[0]
    else:
        print('\tInvalid host name.\n')
        if skipInvalidHostNames:
            return
        else:
            raise InvalidHostName(
                'Invalid host name %s.' % name
            )

    vdsType = host.get_type()
    print('Type: %s' % vdsType)
    try:
        state = getHostState(api, name)
        if state == HOST_STATE_UP:
            if vdsType in OVIRT_NODE_LEGACY_HOST_TYPES:
                print('\tPerforming oVirt Node/RHEVH (Legacy) upgrade...')
                upgradeoVirtNodeLegacy(api, name)
            elif vdsType in OVIRT_NODE_HOST_TYPES:
                print('\tPerforming oVirt Node NGN upgrade...')
                upgradeoVirtNode(api, name)
            else:
                print('\tPerforming host update through reinstallation...')
                deactivateHost(api, name)
                reinstallHost(api, name)
            activateHost(api, name)
            verifyHost(api, name)
    except TimeoutError as error:
        print('Error: ' + repr(error))
        sys.exit(1)
    except InvalidState as error:
        print('Error: ' + repr(error))
        sys.exit(2)
    except InvalidHostName as error:
        print('Error: ' + repr(error))
        sys.exit(3)
    except RuntimeError as error:
        print('Error: ' + repr(error))
        sys.exit(4)


def hostsByClusterName(api, name):
    """
    Return the list of host names of a given oVirt/RHEV cluster.
    """
    hosts = set()
    query = 'cluster = %s' % name
    hostObjs = api.hosts.list(query=query)

    for host in hostObjs:
        hosts.add(host.get_name())

    logging.debug(
        'Cluster %s contains the following hosts: %s' % (
            name,
            str(sorted(list(hosts))),
        )
    )
    return hosts


def getHostState(api, name, skipInvalidHostNames=False):
    if api.hosts.list(name=name):
        state = ''
        while not state:
            try:
                state = api.hosts.get(name).status.state
            except Exception as error:
                state = ''
                logging.debug(
                    'Got an exception while was '
                    'trying to get host\'s state : %s' % (
                        repr(error)
                    )
                )
        return state
    else:
        if skipInvalidHostNames:
            return
        else:
            raise InvalidHostName(
                'Invalid host name %s.' % name
            )


def verifyHostName(api, name):
    if api.hosts.list(name=name):
        return True
    else:
        return False


def disconnect():
    """
    Disconnects from oVirt/RHEV engine.
    """
    global api

    if api:
        api.disconnect()
        api = None
        logging.debug('Closed connection.')


def usage():
    """
    Show command line options of the tool.
    """
    print('\nUpdates RHEL-H hosts and upgrades oVirt Node/RHEVH Legacy hosts.')
    print('--engine = <engine FQDN>')
    print(
        '--username = <Admin username> '
        '(if not provided defaults to admin@internal)'
    )
    print('--password = <Admin password>')
    print(
        '\nNote that you can securely pass the username and/or password using '
        'the following ways:\n'
        '\tEnvironment variables: \n'
        '\t\t%s containing the admin username and '
        '%s containing the admin password.\n'
        '\tPassword file: \n'
        '\t\t%s containing a one line username and password '
        'separated by a colon (each part may be empty).\n' % (
            ENV_ADMIN_USER,
            ENV_ADMIN_PASS,
            PASSWORD_FILE,
        )
    )
    print('--ca = <Location of the engine CA certificate file>')
    print(
        '--insecure - connects in the insecure mode, '
        'doesn\'t verify engine\'s TLS certificate against CA'
    )
    print('--host | --hosts = <host0[,host1...]> - hosts to update')
    print(
        '--cluster | --clusters = <cluster0[,cluster1...]> '
        '- clusters to update.'
    )
    print(
        '--resume - Resume operation. In this mode only one host must be '
        'specified, from which one forwards the cluster will be upgraded.'
    )
    print(
        '--after - When used together with --resume it will begin with the '
        'host right after the one specified.'
    )
    print(
        '--skip-invalid-host-names - Skip invalid host names and continue.'
    )
    print(
        '-d | ---debug - Show debugging information. '
        'Note! This will expose admin password in clear text.'
    )
    sys.exit(0)


if __name__ == '__main__':

    try:
        opts, args = getopt.getopt(
            sys.argv[1:],
            'dh:c:re:u:p:k:i',
            [
                'host=',
                'hosts=',
                'cluster=',
                'clusters=',
                'resume',
                'after',
                'engine=',
                'username=',
                'password=',
                'ca=',
                'insecure',
                'skip-invalid-host-names',
                'debug'
            ],
        )
    except getopt.GetoptError:
        usage()

    if len(sys.argv) < 2:
        usage()

    engineFqdn = defaultEngineFqdn
    username = None
    password = None
    port = defaultPort
    hosts = set()
    clusters = set()
    resume = False
    after = False
    ca = '/etc/pki/ovirt-engine/ca.pem'
    insecure = False
    skipInvalidHostNames = False
    debug = False
    loggingLevel = logging.CRITICAL

    for opt, arg in opts:
        if opt in ('--help'):
            usage()
            sys.exit()
        elif opt in ('-h', '--host', '--hosts'):
            hosts = arg
        elif opt in ('-c', '--cluster', '--clusters'):
            clusters = arg
        elif opt in ('-r', '--resume'):
            resume = True
        elif opt in ('-a', '--after'):
            after = True
        elif opt in ('-e', '--engine'):
            engineFqdn = arg
        elif opt in ('-u', '--username'):
            username = arg
        elif opt in ('-p', '--password'):
            password = arg
        elif opt in ('--ca'):
            ca = arg
        elif opt in ('-i', '--insecure'):
            insecure = True
        elif opt in ('--skip-invalid-host-names'):
            skipInvalidHostNames = True
        elif opt in ('-d', '--debug'):
            debug = True

    if debug:
        loggingLevel = logging.DEBUG

    logging.basicConfig(format='%(message)s', level=loggingLevel)

    if username:
        logging.debug('Got username %s from command line.' % username)
    if password:
        logging.debug('Got password %s from command line.' % password)

    # if we received both username and password from the command line
    # then no further steps are necessary, otherwise we check the
    # environment, password file and as a last resort use default
    # (for username only).
    if not (username and password):

        # first we check if any were supplied in the environment.
        if os.environ.get(ENV_ADMIN_USER) and not username:
            username = os.environ.get(ENV_ADMIN_USER)
            logging.debug('%s is set to: %s' % (ENV_ADMIN_USER, username))

        if os.environ.get(ENV_ADMIN_PASS) and not password:
            password = os.environ.get(ENV_ADMIN_PASS)
            logging.debug('%s is set to: %s' % (ENV_ADMIN_PASS, password))

        # next, if we're still missing both username and password then
        # we're trying to read username and/or password from the password file.
        if not (username and password):
            try:
                logging.debug(
                    'Trying to read password file (%s).' % (
                        os.path.expanduser(PASSWORD_FILE)
                    )
                )
                with open(os.path.expanduser(PASSWORD_FILE)) as f:
                    user, passw = f.readlines()[0].strip().split(':')
                    if user and not username:
                        username = user
                        logging.debug(
                            'Using username %s from password file.' % username
                        )
                    if passw and not password:
                        password = passw
                        logging.debug(
                            'Using password %s from password file.' % password
                        )
            except IOError, ValueError:
                pass

        # if we haven't received an username to use thus far,
        # then use the default one.
        if not username:
            username = defaultUsername
            logging.debug(
                'Using default admin username of %s.' % defaultUsername
            )

    logging.debug('host(s): %s' % (hosts if hosts else None))
    logging.debug('cluster(s): %s' % (clusters if clusters else None))
    logging.debug('resume: %s' % resume)
    logging.debug('engine: %s' % engineFqdn)
    logging.debug('username: %s' % username)
    logging.debug('password: %s' % ('*' * len(password) if password else None))
    logging.debug(
        'ca: %s' % (ca if os.path.isfile(ca) else ca + ' (File is missing)')
    )
    logging.debug('insecure: %s' % insecure)
    logging.debug('skip invalid host names: %s' % skipInvalidHostNames)
    logging.debug('debug: %s' % debug)

    api = connect()

    if not resume:
        if hosts:
            hosts = set(host for host in hosts.split(',') if host)

        if clusters:
            clusters = set(
                cluster for cluster in clusters.split(',') if cluster
            )

            for cluster in clusters:
                hosts = hosts.union(hostsByClusterName(api, cluster))

        if not hosts:
            print('No hosts to process.\n')
            sys.exit(3)
        hosts = sorted(hosts)
        for host in hosts:
            processHost(api, host, skipInvalidHostNames)
    else:
        if hosts:
            hosts = list(host for host in hosts.split(',') if host)
            if len(hosts) > 1:
                print('Resume option requires host option of a single host.')
                sys.exit(1)
        else:
            print(
                'Resume option requires a host option to be specified.'
            )
            sys.exit(1)
        name = hosts[0]
        if name.endswith('++') or name.endswith('+1'):
            name = name[:-2]
            after = True
        if api.hosts.list(name=name):
            hostObj = api.hosts.list(name=name)[0]
        else:
            print('Invalid host name.\n')
            sys.exit(3)
        hostClusterObj = hostObj.get_cluster()
        clusterObjs = api.clusters.list()
        for clusterObj in clusterObjs:
            if clusterObj.id == hostClusterObj.id:
                break
        hosts = sorted(list(hostsByClusterName(api, clusterObj.name)))
        hostIndex = hosts.index(name)
        if after:
            hostIndex += 1
            if hostIndex == len(hosts):
                print(
                    'The host %s is the last host in the cluster. '
                    'No more hosts to process.' % (
                        name,
                    )
                )
                sys.exit(0)
            else:
                name = hosts[hostIndex]
        print(
            'Resuming operation on cluster %s, starting from host %s' % (
                clusterObj.name,
                name,
            )
        )
        hosts = hosts[hostIndex:]
        for host in hosts:
            processHost(api, host, skipInvalidHostNames)
