#
# Copyright (C) 2013, 2015 S3IT, University of Zurich
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
__author__ = '''
Nicolas Baer <nicolas.baer@uzh.ch>,
Antonio Messina <antonio.s.messina@gmail.com>,
Riccardo Murri <riccardo.murri@gmail.com>
'''

# stdlib imports
from copy import copy
import logging
import os
import re
import tempfile
import shutil
from subprocess import call
import sys

# 3rd party imports
from ansible.errors import AnsibleError

# Elasticluster imports
import elasticluster
from elasticluster import log
from elasticluster.providers import AbstractSetupProvider


_PATH_SPLIT_RE = re.compile(r'\s* [,:] \s*', re.X)


class AnsibleSetupProvider(AbstractSetupProvider):
    """This implementation uses ansible to configure and manage the cluster
    setup. See https://github.com/ansible/ansible for details.

    :param dict groups: dictionary of node kinds with corresponding
                        ansible groups to install on the node kind.
                        e.g [node_kind] = ['ansible_group1', 'ansible_group2']
                        The group defined here can be references in each
                        node. Therefore groups can make it easier to
                        define multiple groups for one node.

    :param str playbook_path: path to playbook; if empty this will use
                              the shared playbook of elasticluster

    :param dict environment_vars: dictonary to define variables per node
                                  kind, e.g. [node_kind][var] = value

    :param str storage_path: path to store the inventory file. By default
                             the inventory file is saved temporarily in a
                             temporary directory and deleted when the
                             cluster in stopped.

    :param bool sudo: indication whether use sudo to gain root permission

    :param str sudo_user: user with root permission

    :param str ansible_module_dir: comma- or colon-separated
                                   path to additional ansible modules
    :param extra_conf: tbd.

    :ivar groups: node kind and ansible group mapping dictionary
    :ivar environment: additional environment variables
    """
    inventory_file_ending = 'ansible-inventory'

    def __init__(self, groups, playbook_path=None, environment_vars=dict(),
                 storage_path=None, sudo=True, sudo_user='root',
                 ansible_module_dir=None, ssh_pipelining=True, **extra_conf):
        self.groups = groups
        self._playbook_path = playbook_path
        self.environment = environment_vars
        self._storage_path = storage_path
        self._sudo_user = sudo_user
        self._sudo = sudo
        self.ssh_pipelining = ssh_pipelining
        self.extra_conf = extra_conf

        if not self._playbook_path:
            self._playbook_path = os.path.join(
                sys.prefix,
                'share/elasticluster/providers/ansible-playbooks',
                'site.yml'
            )
        else:
            self._playbook_path = os.path.expanduser(self._playbook_path)
            self._playbook_path = os.path.expandvars(self._playbook_path)

        if self._storage_path:
            self._storage_path = os.path.expanduser(self._storage_path)
            self._storage_path = os.path.expandvars(self._storage_path)
            self._storage_path_tmp = False
            if not os.path.exists(self._storage_path):
                os.makedirs(self._storage_path)
        else:
            self._storage_path = tempfile.mkdtemp()
            self._storage_path_tmp = True

        if ansible_module_dir:
            self._ansible_module_dir = [
                pth.strip() for pth in _PATH_SPLIT_RE.split(ansible_module_dir)]
        else:
            self._ansible_module_dir = []

    def setup_cluster(self, cluster):
        """Configures the cluster according to the node_kind to ansible
        group matching. This method is idempotent and therefore can be
        called multiple times without corrupting the cluster configuration.

        :param cluster: cluster to configure
        :type cluster: :py:class:`elasticluster.cluster.Cluster`

        :return: True on success, False otherwise. Please note, if nothing
                 has to be configures True is returned

        :raises: `AnsibleError` if the playbook can not be found or playbook
                 is corrupt
        """
        inventory_path = self._build_inventory(cluster)
        private_key_file = cluster.user_key_private

        # use env vars to configure Ansible
        #
        # Ansible does not merge keys in configuration files: rather
        # it uses the first configuration file found.  However,
        # environment variables can be used to selectively override
        # parts of the config; according to [1]: "they are mostly
        # considered to be a legacy system as compared to the config
        # file, but are equally valid."
        #
        # [1]: http://docs.ansible.com/ansible/intro_configuration.html#environmental-configuration
        ansible_env = copy(os.environ)
        # see all values in https://github.com/ansible/ansible/blob/devel/lib/ansible/constants.py
        ansible_env['ANSIBLE_FORKS'] = '10'  # FIXME: make configurable!
        ansible_env['ANSIBLE_HOST_KEY_CHECKING'] = 'no'
        ansible_env['ANSIBLE_PRIVATE_KEY_FILE'] = private_key_file
        ansible_env['ANSIBLE_SSH_PIPELINING'] = 'yes'
        ansible_env['ANSIBLE_TIMEOUT'] = '120'  # FIXME: make configurable!
        if __debug__:
            elasticluster.log.debug(
                "Calling `ansible-playbook` with the following environment:")
            for var, value in sorted(ansible_env.items()):
                elasticluster.log.debug("- %s=%r", var, value)

        # check paths
        if not inventory_path:
            # No inventory file has been created, maybe an
            # invalid class has been specified in config file? Or none?
            # assume it is fine.
            elasticluster.log.info("No setup required for this cluster.")
            return True
        if not os.path.exists(inventory_path):
            raise AnsibleError(
                "inventory file `{inventory_path}` could not be found"
                .format(inventory_path=inventory_path))
        # ANTONIO: These should probably be configuration error
        # instead, and should probably checked inside __init__().
        if not os.path.exists(self._playbook_path):
            raise AnsibleError(
                "playbook `{playbook_path}` could not be found"
                .format(playbook_path=self._playbook_path))
        if not os.path.isfile(self._playbook_path):
            raise AnsibleError(
                "playbook `{playbook_path}` is not a file"
                .format(playbook_path=self._playbook_path))

        elasticluster.log.debug("Using playbook file %s.", self._playbook_path)

        # build `ansible-playbook` command-line
        cmd = [
            'ansible-playbook',
            os.path.realpath(self._playbook_path),
            ('--inventory=' + inventory_path),
        ]

        if self._sudo:
            cmd.extend([
                # force all plays to use `sudo` (even if not marked as such)
                '--sudo',
                # desired sudo-to user
                ('--sudo-user=' + self._sudo_user),
            ])

        # determine Ansible verbosity as a function of ElastiCluster's
        # log level (since we lack access to
        # `ElastiCluster().params.verbose` here, but we can still
        # access the log configuration since it's global).
        verbosity = min(3, (logging.WARNING - elasticluster.log.level) / 10)
        if verbosity > 0:
            cmd.append('-' + ('v' * verbosity))  # e.g., `-vv`

        elasticluster.log.debug(
            "Running Ansible command `%s` ...", (' '.join(cmd)))
        rc = call(cmd, env=ansible_env, bufsize=1, close_fds=True)
        if rc == 0:
            elasticluster.log.info("Cluster correctly configured.")
            return True
        else:
            elasticluster.log.error(
                "Command `ansible-playbook` failed with exit code %d.", rc)
            elasticluster.log.error(
                "Check the output lines above for additional information on this error.")
            elasticluster.log.error(
                "The cluster has likely *not* been configured correctly."
                " You may need to re-run `elasticluster setup` or fix the playbooks.")
            return False

    def _build_inventory(self, cluster):
        """Builds the inventory for the given cluster and returns its path

        :param cluster: cluster to build inventory for
        :type cluster: :py:class:`elasticluster.cluster.Cluster`
        """
        inventory = dict()
        for node in cluster.get_all_nodes():
            if node.kind in self.groups:
                extra_vars = ['ansible_ssh_user=%s' % node.image_user]
                if node.kind in self.environment:
                    extra_vars.extend('%s=%s' % (k, v) for k, v in
                                      self.environment[node.kind].items())
                for group in self.groups[node.kind]:
                    if group not in inventory:
                        inventory[group] = []
                    public_ip = node.preferred_ip
                    inventory[group].append(
                        (node.name, public_ip, str.join(' ', extra_vars)))

        if inventory:
            # create a temporary file to pass to ansible, since the
            # api is not stable yet...
            if self._storage_path_tmp:
                if not self._storage_path:
                    self._storage_path = tempfile.mkdtemp()
                elasticluster.log.warning("Writing inventory file to tmp dir "
                                          "`%s`", self._storage_path)
            fname = '%s.%s' % (AnsibleSetupProvider.inventory_file_ending,
                               cluster.name)
            inventory_path = os.path.join(self._storage_path, fname)

            inventory_fd = open(inventory_path, 'w+')
            for section, hosts in inventory.items():
                inventory_fd.write("\n[" + section + "]\n")
                if hosts:
                    for host in hosts:
                        hostline = "%s ansible_ssh_host=%s %s\n" \
                                   % host
                        inventory_fd.write(hostline)

            inventory_fd.close()

            return inventory_path
        else:
            elasticluster.log.info("No inventory file was created.")
            return None

    def cleanup(self, cluster):
        """Deletes the inventory file used last recently used.

        :param cluster: cluster to clear up inventory file for
        :type cluster: :py:class:`elasticluster.cluster.Cluster`
        """
        if self._storage_path and os.path.exists(self._storage_path):
            fname = '%s.%s' % (AnsibleSetupProvider.inventory_file_ending,
                               cluster.name)
            inventory_path = os.path.join(self._storage_path, fname)

            if os.path.exists(inventory_path):
                try:
                    os.unlink(inventory_path)
                    if self._storage_path_tmp:
                        if len(os.listdir(self._storage_path)) == 0:
                            shutil.rmtree(self._storage_path)
                except OSError as ex:
                    log.warning(
                        "AnsibileProvider: Ignoring error while deleting "
                        "inventory file %s: %s", inventory_path, ex)

    def __setstate__(self, state):
        self.__dict__ = state
        # Compatibility fix: allow loading clusters created before
        # option `ssh_pipelining` was added.
        if 'ssh_pipelining' not in state:
            self.ssh_pipelining = True
