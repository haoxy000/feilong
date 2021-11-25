#    Copyright 2017,2021 IBM Corp.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.


import abc
import re
import shutil
import six
import threading
import os

from zvmsdk import config
from zvmsdk import database
from zvmsdk import dist
from zvmsdk import exception
from zvmsdk import log
from zvmsdk import smtclient
from zvmsdk import utils as zvmutils
from zvmsdk import vmops


_VolumeOP = None
CONF = config.CONF
LOG = log.LOG

# instance parameters:
NAME = 'name'
OS_TYPE = 'os_type'
# volume parameters:
SIZE = 'size'
TYPE = 'type'
LUN = 'lun'
# connection_info parameters:
ALIAS = 'alias'
PROTOCOL = 'protocol'
FCPS = 'fcps'
WWPNS = 'wwpns'
DEDICATE = 'dedicate'


def get_volumeop():
    global _VolumeOP
    if not _VolumeOP:
        _VolumeOP = VolumeOperatorAPI()
    return _VolumeOP


@six.add_metaclass(abc.ABCMeta)
class VolumeOperatorAPI(object):
    """Volume operation APIs oriented towards SDK driver.

    The reason to design these APIs is to facilitate the SDK driver
    issuing a volume related request without knowing details. The
    details among different distributions, different instance status,
    different volume types and so on are all hidden behind these APIs.
    The only thing the issuer need to know is what it want to do on
    which targets.

    In fact, that's an ideal case. In the real world, something like
    connection_info still depends on different complex and the issuer
    needs to know how to deal with its case. Even so, these APIs can
    still make things much easier.
    """

    _fcp_manager_obj = None

    def __init__(self):
        if not VolumeOperatorAPI._fcp_manager_obj:
            VolumeOperatorAPI._fcp_manager_obj = FCPVolumeManager()
        self._volume_manager = VolumeOperatorAPI._fcp_manager_obj

    def attach_volume_to_instance(self, connection_info):
        self._volume_manager.attach(connection_info)

    def detach_volume_from_instance(self, connection_info):
        self._volume_manager.detach(connection_info)

    def volume_refresh_bootmap(self, fcpchannel, wwpn, lun,
                               wwid='',
                               transportfiles='', guest_networks=None):
        return self._volume_manager.volume_refresh_bootmap(fcpchannel, wwpn,
                                            lun, wwid=wwid,
                                            transportfiles=transportfiles,
                                            guest_networks=guest_networks)

    def get_volume_connector(self, assigner_id, reserve):
        return self._volume_manager.get_volume_connector(assigner_id, reserve)

    def check_fcp_exist_in_db(self, fcp, raise_exec=True):
        return self._volume_manager.check_fcp_exist_in_db(fcp, raise_exec)

    def get_all_fcp_usage(self, assigner_id=None):
        return self._volume_manager.get_all_fcp_usage(assigner_id)

    def get_all_fcp_usage_grouped_by_path(self, assigner_id=None):
        return self._volume_manager.get_all_fcp_usage_grouped_by_path(
                assigner_id)

    def get_fcp_usage(self, fcp):
        return self._volume_manager.get_fcp_usage(fcp)

    def set_fcp_usage(self, assigner_id, fcp, reserved, connections):
        return self._volume_manager.set_fcp_usage(fcp, assigner_id,
                                                  reserved, connections)


@six.add_metaclass(abc.ABCMeta)
class VolumeConfiguratorAPI(object):
    """Volume configure APIs to implement volume config jobs on the
    target instance, like: attach, detach, and so on.

    The reason to design these APIs is to hide the details among
    different Linux distributions and releases.
    """
    def __init__(self):
        self._vmop = vmops.get_vmops()
        self._dist_manager = dist.LinuxDistManager()
        self._smtclient = smtclient.get_smtclient()

    def check_IUCV_is_ready(self, assigner_id):
        # Make sure the iucv channel is ready for communication with VM
        ready = True
        try:
            self._smtclient.execute_cmd(assigner_id, 'pwd')
        except exception.SDKSMTRequestFailed as err:
            if 'UNAUTHORIZED_ERROR' in err.format_message():
                # If unauthorized, we must raise exception
                errmsg = err.results['response'][0]
                msg = ('IUCV failed to get authorization from VM %(vm)s with '
                       'error %(err)s' % {'vm': assigner_id,
                                          'err': errmsg})
                LOG.error(msg)
                raise exception.SDKVolumeOperationError(rs=6,
                                                        userid=assigner_id,
                                                        msg=errmsg)
            else:
                # In such case, we can continue without raising exception
                ready = False
                msg = ('Failed to connect VM %(vm)s with error '
                       '%(err)s, assume it is OFF status '
                       'and continue' % {'vm': assigner_id,
                                         'err': err.results['response'][0]})
                LOG.debug(msg)
        return ready

    def _get_status_code_from_systemctl(self, assigner_id, command):
        """get the status code from systemctl status
        for example, if systemctl status output:
        Main PID: 28406 (code=exited, status=0/SUCCESS)

        this function will return the 3 behind status=
        """
        output = self._smtclient.execute_cmd_direct(assigner_id, command)
        exit_code = 0
        for line in output['response']:
            if 'Main PID' in line:
                # the status code start with = and before /FAILURE
                pattern = '(?<=status=)([0-9]+)'
                ret = re.search(pattern, line)
                exit_code = int(ret.group(1))
                break
        return exit_code

    def config_attach(self, fcp_list, assigner_id, target_wwpns, target_lun,
                      multipath, os_version, mount_point):
        LOG.info("Begin to configure volume (WWPN:%s, LUN:%s) on the "
                 "target machine %s with FCP devices "
                 "%s." % (target_wwpns, target_lun, assigner_id, fcp_list))
        linuxdist = self._dist_manager.get_linux_dist(os_version)()
        self.configure_volume_attach(fcp_list, assigner_id, target_wwpns,
                                     target_lun, multipath, os_version,
                                     mount_point, linuxdist)
        iucv_is_ready = self.check_IUCV_is_ready(assigner_id)
        if iucv_is_ready:
            # active mode should restart zvmguestconfigure to run reader file
            active_cmds = linuxdist.create_active_net_interf_cmd()
            ret = self._smtclient.execute_cmd_direct(assigner_id, active_cmds)
            LOG.debug('attach scripts return values: %s' % ret)
            if ret['rc'] != 0:
                # get exit code by systemctl status
                get_status_cmd = 'systemctl status zvmguestconfigure.service'
                exit_code = self._get_status_code_from_systemctl(
                    assigner_id, get_status_cmd)
                if exit_code == 1:
                    errmsg = ('attach script execution failed because the '
                              'volume (WWPN:%s, LUN:%s) did not show up in '
                              'the target machine %s , please check its '
                              'connections.' % (target_wwpns, target_lun,
                                                assigner_id))
                else:
                    errmsg = ('attach script execution in the target machine '
                              '%s for volume (WWPN:%s, LUN:%s) '
                              'failed with unknown reason, exit code is: %s.'
                              % (assigner_id, target_wwpns, target_lun,
                                 exit_code))
                LOG.error(errmsg)
                raise exception.SDKVolumeOperationError(rs=8,
                                                        userid=assigner_id,
                                                        msg=errmsg)
        LOG.info("Configuration of volume (WWPN:%s, LUN:%s) on the "
                 "target machine %s with FCP devices "
                 "%s is done." % (target_wwpns, target_lun, assigner_id,
                                  fcp_list))

    def config_detach(self, fcp_list, assigner_id, target_wwpns, target_lun,
                      multipath, os_version, mount_point, connections):
        LOG.info("Begin to deconfigure volume (WWPN:%s, LUN:%s) on the "
                 "target machine %s with FCP devices "
                 "%s." % (target_wwpns, target_lun, assigner_id, fcp_list))
        linuxdist = self._dist_manager.get_linux_dist(os_version)()
        self.configure_volume_detach(fcp_list, assigner_id, target_wwpns,
                                     target_lun, multipath, os_version,
                                     mount_point, linuxdist, connections)
        iucv_is_ready = self.check_IUCV_is_ready(assigner_id)
        if iucv_is_ready:
            # active mode should restart zvmguestconfigure to run reader file
            active_cmds = linuxdist.create_active_net_interf_cmd()
            ret = self._smtclient.execute_cmd_direct(assigner_id, active_cmds)
            LOG.debug('detach scripts return values: %s' % ret)
            if ret['rc'] != 0:
                get_status_cmd = 'systemctl status zvmguestconfigure.service'
                exit_code = self._get_status_code_from_systemctl(
                    assigner_id, get_status_cmd)
                if exit_code == 1:
                    errmsg = ('detach scripts execution failed because the '
                              'device %s in the target virtual machine %s '
                              'is in use.' % (fcp_list, assigner_id))
                else:
                    errmsg = ('detach scripts execution on fcp %s in the '
                              'target virtual machine %s failed '
                              'with unknow reason, exit code is: %s'
                              % (fcp_list, assigner_id, exit_code))
                LOG.error(errmsg)
                raise exception.SDKVolumeOperationError(rs=9,
                                                        userid=assigner_id,
                                                        msg=errmsg)
        LOG.info("Deconfiguration of volume (WWPN:%s, LUN:%s) on the "
                 "target machine %s with FCP devices "
                 "%s is done." % (target_wwpns, target_lun, assigner_id,
                                  fcp_list))

    def _create_file(self, assigner_id, file_name, data):
        temp_folder = self._smtclient.get_guest_temp_path(assigner_id)
        file_path = os.path.join(temp_folder, file_name)
        with open(file_path, "w") as f:
            f.write(data)
        return file_path, temp_folder

    def configure_volume_attach(self, fcp_list, assigner_id, target_wwpns,
                                target_lun, multipath, os_version,
                                mount_point, linuxdist):
        """new==True means this is first attachment"""
        # get configuration commands
        fcp_list_str = ' '.join(fcp_list)
        target_wwpns_str = ' '.join(target_wwpns)
        config_cmds = linuxdist.get_volume_attach_configuration_cmds(
            fcp_list_str, target_wwpns_str, target_lun, multipath,
            mount_point)
        LOG.debug('Got volume attachment configuation cmds for %s,'
                  'the content is:%s'
                  % (assigner_id, config_cmds))
        # write commands into script file
        config_file, config_file_path = self._create_file(assigner_id,
                                                          'atvol.sh',
                                                          config_cmds)
        LOG.debug('Creating file %s to contain volume attach '
                  'configuration file' % config_file)
        # punch file into guest
        fileClass = "X"
        try:
            self._smtclient.punch_file(assigner_id, config_file, fileClass)
        finally:
            LOG.debug('Removing the folder %s ', config_file_path)
            shutil.rmtree(config_file_path)

    def configure_volume_detach(self, fcp_list, assigner_id, target_wwpns,
                                target_lun, multipath, os_version,
                                mount_point, linuxdist, connections):
        # get configuration commands
        fcp_list_str = ' '.join(fcp_list)
        target_wwpns_str = ' '.join(target_wwpns)
        config_cmds = linuxdist.get_volume_detach_configuration_cmds(
            fcp_list_str, target_wwpns_str, target_lun, multipath,
            mount_point, connections)
        LOG.debug('Got volume detachment configuation cmds for %s,'
                  'the content is:%s'
                  % (assigner_id, config_cmds))
        # write commands into script file
        config_file, config_file_path = self._create_file(assigner_id,
                                                          'devol.sh',
                                                          config_cmds)
        LOG.debug('Creating file %s to contain volume detach '
                  'configuration file' % config_file)
        # punch file into guest
        fileClass = "X"
        try:
            self._smtclient.punch_file(assigner_id, config_file, fileClass)
        finally:
            LOG.debug('Removing the folder %s ', config_file_path)
            shutil.rmtree(config_file_path)


class FCP(object):
    def __init__(self, init_info):
        self._dev_no = None
        self._dev_status = None
        self._npiv_port = None
        self._chpid = None
        self._physical_port = None
        self._assigned_id = None

        self._parse(init_info)

    @staticmethod
    def _get_wwpn_from_line(info_line):
        wwpn = info_line.split(':')[-1].strip().lower()
        return wwpn if (wwpn and wwpn.upper() != 'NONE') else None

    @staticmethod
    def _get_dev_number_from_line(info_line):
        dev_no = info_line.split(':')[-1].strip().lower()
        return dev_no if dev_no else None

    @staticmethod
    def _get_dev_status_from_line(info_line):
        dev_status = info_line.split(':')[-1].strip().lower()
        return dev_status if dev_status else None

    @staticmethod
    def _get_chpid_from_line(info_line):
        chpid = info_line.split(':')[-1].strip().upper()
        return chpid if chpid else None

    def _parse(self, init_info):
        """Initialize a FCP device object from several lines of string
           describing properties of the FCP device.
           Here is a sample:
               opnstk1: FCP device number: B83D
               opnstk1:   Status: Free
               opnstk1:   NPIV world wide port number: NONE
               opnstk1:   Channel path ID: 59
               opnstk1:   Physical world wide port number: 20076D8500005181
           The format comes from the response of xCAT, do not support
           arbitrary format.
        """
        if isinstance(init_info, list) and (len(init_info) == 5):
            self._dev_no = self._get_dev_number_from_line(init_info[0])
            self._dev_status = self._get_dev_status_from_line(init_info[1])
            self._npiv_port = self._get_wwpn_from_line(init_info[2])
            self._chpid = self._get_chpid_from_line(init_info[3])
            self._physical_port = self._get_wwpn_from_line(init_info[4])

    def get_dev_no(self):
        return self._dev_no

    def get_dev_status(self):
        return self._dev_status

    def get_npiv_port(self):
        return self._npiv_port

    def get_physical_port(self):
        return self._physical_port

    def get_chpid(self):
        return self._chpid

    def is_valid(self):
        # FIXME: add validation later
        return True


class FCPManager(object):

    def __init__(self):
        # _fcp_pool store the objects of FCP index by fcp id
        self._fcp_pool = {}
        # _fcp_path_info store the FCP path mapping index by path no
        self._fcp_path_mapping = {}
        self.db = database.FCPDbOperator()
        self._smtclient = smtclient.get_smtclient()

    def init_fcp(self, assigner_id):
        """init_fcp to init the FCP managed by this host"""
        # TODO master_fcp_list (zvm_zhcp_fcp_list) really need?
        fcp_list = CONF.volume.fcp_list
        if fcp_list == '':
            errmsg = ("because CONF.volume.fcp_list is empty, "
                      "no volume functions available")
            LOG.info(errmsg)
            return

        self._init_fcp_pool(fcp_list, assigner_id)
        self._sync_db_fcp_list()

    def _init_fcp_pool(self, fcp_list, assigner_id):
        """The FCP infomation got from smt(zthin) looks like :
           host: FCP device number: xxxx
           host:   Status: Active
           host:   NPIV world wide port number: xxxxxxxx
           host:   Channel path ID: xx
           host:   Physical world wide port number: xxxxxxxx
           ......
           host: FCP device number: xxxx
           host:   Status: Active
           host:   NPIV world wide port number: xxxxxxxx
           host:   Channel path ID: xx
           host:   Physical world wide port number: xxxxxxxx

        """
        self._fcp_path_mapping = self._expand_fcp_list(fcp_list)
        complete_fcp_set = set()
        for path, fcp_set in self._fcp_path_mapping.items():
            complete_fcp_set = complete_fcp_set | fcp_set
        fcp_info = self._get_all_fcp_info(assigner_id)
        lines_per_item = 5
        # after process, _fcp_pool should be a list include FCP ojects
        # whose FCP ID are from CONF.volume.fcp_list and also should be
        # found in fcp_info
        num_fcps = len(fcp_info) // lines_per_item
        for n in range(0, num_fcps):
            fcp_init_info = fcp_info[(5 * n):(5 * (n + 1))]
            fcp = FCP(fcp_init_info)
            dev_no = fcp.get_dev_no()
            if dev_no in complete_fcp_set:
                if fcp.is_valid():
                    self._fcp_pool[dev_no] = fcp
                else:
                    errmsg = ("Find an invalid FCP device with properties {"
                              "dev_no: %(dev_no)s, "
                              "NPIV_port: %(NPIV_port)s, "
                              "CHPID: %(CHPID)s, "
                              "physical_port: %(physical_port)s} !") % {
                                  'dev_no': fcp.get_dev_no(),
                                  'NPIV_port': fcp.get_npiv_port(),
                                  'CHPID': fcp.get_chpid(),
                                  'physical_port': fcp.get_physical_port()}
                    LOG.warning(errmsg)
            else:
                # normal, FCP not used by cloud connector at all
                msg = "Found a fcp %s not in fcp_list" % dev_no
                LOG.debug(msg)

    @staticmethod
    def _expand_fcp_list(fcp_list):
        """Expand fcp list string into a python list object which contains
        each fcp devices in the list string. A fcp list is composed of fcp
        device addresses, range indicator '-', and split indicator ';'.

        For example, if fcp_list is
        "0011-0013;0015;0017-0018", expand_fcp_list(fcp_list) will return
        [0011, 0012, 0013, 0015, 0017, 0018].

        ATTENTION: To support multipath, we expect fcp_list should be like
        "0011-0014;0021-0024", "0011-0014" should have been on same physical
        WWPN which we called path0, "0021-0024" should be on another physical
        WWPN we called path1 which is different from "0011-0014".
        path0 and path1 should have same count of FCP devices in their group.
        When attach, we will choose one WWPN from path0 group, and choose
        another one from path1 group. Then we will attach this pair of WWPNs
        together to the guest as a way to implement multipath.

        """
        LOG.debug("Expand FCP list %s" % fcp_list)

        if not fcp_list:
            return set()
        fcp_list = fcp_list.strip()
        fcp_list = fcp_list.replace(' ', '')
        range_pattern = '[0-9a-fA-F]{1,4}(-[0-9a-fA-F]{1,4})?'
        match_pattern = "^(%(range)s)(;%(range)s;?)*$" % \
                        {'range': range_pattern}

        item_pattern = "(%(range)s)(,%(range)s?)*" % \
                       {'range': range_pattern}

        multi_match_pattern = "^(%(range)s)(;%(range)s;?)*$" % \
                       {'range': item_pattern}

        if not re.match(match_pattern, fcp_list) and \
           not re.match(multi_match_pattern, fcp_list):
            errmsg = ("Invalid FCP address %s") % fcp_list
            raise exception.SDKInternalError(msg=errmsg)

        fcp_devices = {}
        path_no = 0
        for _range in fcp_list.split(';'):
            for item in _range.split(','):
                # remove duplicate entries
                devices = set()
                if item != '':
                    if '-' not in item:
                        # single device
                        fcp_addr = int(item, 16)
                        devices.add("%04x" % fcp_addr)
                    else:
                        # a range of address
                        (_min, _max) = item.split('-')
                        _min = int(_min, 16)
                        _max = int(_max, 16)
                        for fcp_addr in range(_min, _max + 1):
                            devices.add("%04x" % fcp_addr)
                    if fcp_devices.get(path_no):
                        fcp_devices[path_no].update(devices)
                    else:
                        fcp_devices[path_no] = devices
            path_no = path_no + 1
        return fcp_devices

    def _report_orphan_fcp(self, fcp):
        """check there is record in db but not in FCP configuration"""
        LOG.warning("WARNING: fcp %s found in db but we can not use it "
                    "because it is not in CONF.volume.fcp_list %s or "
                    "it did not belongs to free status FCPs %s." %
                    (fcp, CONF.volume.fcp_list, self._fcp_pool.keys()))
        if not self.db.is_reserved(fcp):
            self.db.delete(fcp)
            LOG.info("Remove %s from fcp db" % fcp)

    def _add_fcp(self, fcp, path):
        """add fcp to db if it's not in db but in fcp list and init it"""
        try:
            LOG.info("fcp %s found in CONF.volume.fcp_list, add it to db" %
                     fcp)
            if self._fcp_pool[fcp].get_dev_status() == 'free':
                self.db.new(fcp, path)
            else:
                LOG.warning("fcp %s was not added into database because it is "
                            "not in Free status." % fcp)
        except Exception:
            LOG.info("failed to add fcp %s into db", fcp)

    def _sync_db_fcp_list(self):
        """sync db records from given fcp list, for example, you need
        warn if some FCP already removed while it's still in use,
        or info about the new FCP added"""
        fcp_db_list = self.db.get_all()

        for fcp_rec in fcp_db_list:
            if not fcp_rec[0].lower() in self._fcp_pool.keys():
                self._report_orphan_fcp(fcp_rec[0])
        # firt loop is for getting the path No
        for path, fcp_list in self._fcp_path_mapping.items():
            for fcp in fcp_list:
                if fcp.lower() in self._fcp_pool.keys():
                    res = self.db.get_from_fcp(fcp)
                    # if not found this record, a [] will be returned
                    if len(res) == 0:
                        self._add_fcp(fcp, path)
                    else:
                        old_path = res[0][4]
                        if old_path != path:
                            self.db.update_path_of_fcp(fcp, path)

    def _list_fcp_details(self, userid, status):
        return self._smtclient.get_fcp_info_by_status(userid, status)

    def _get_all_fcp_info(self, assigner_id):
        fcp_info = []
        free_fcp_info = self._list_fcp_details(assigner_id, 'free')
        active_fcp_info = self._list_fcp_details(assigner_id, 'active')

        if free_fcp_info:
            fcp_info.extend(free_fcp_info)

        if active_fcp_info:
            fcp_info.extend(active_fcp_info)

        return fcp_info

    def find_and_reserve_fcp(self, assigner_id):
        """reserve the fcp to assigner_id

        The function to reserve a fcp for user
        1. Check whether assigner_id has a fcp already
           if yes, make the reserve of that record to 1
        2. No fcp, then find a fcp and reserve it

        fcp will be returned, or None indicate no fcp
        """
        fcp_list = self.db.get_allocated_fcps_from_assigner(assigner_id)
        if not fcp_list:
            new_fcp = self.db.find_and_reserve()
            if new_fcp is None:
                LOG.info("no more fcp to be allocated")
                return None

            LOG.debug("allocated %s fcp for %s assigner" %
                      (new_fcp, assigner_id))
            return new_fcp
        else:
            # we got it from db, let's reuse it
            old_fcp = fcp_list[0][0]
            self.db.reserve(fcp_list[0][0])
            return old_fcp

    def increase_fcp_usage(self, fcp, assigner_id=None):
        """Incrase fcp usage of given fcp

        Returns True if it's a new fcp, otherwise return False
        """
        # TODO: check assigner_id to make sure on the correct fcp record
        connections = self.db.get_connections_from_fcp(fcp)
        new = False
        if not connections:
            self.db.assign(fcp, assigner_id)
            new = True
        else:
            self.db.increase_usage(fcp)

        return new

    def add_fcp_for_assigner(self, fcp, assigner_id=None):
        """Incrase fcp usage of given fcp
        Returns True if it's a new fcp, otherwise return False
        """
        # get the sum of connections belong to assigner_id
        connections = self.db.get_connections_from_fcp(fcp)
        new = False
        if connections == 0:
            # ATTENTION: logically, only new fcp was added
            self.db.assign(fcp, assigner_id)
            new = True
        else:
            self.db.increase_usage_by_assigner(fcp, assigner_id)

        return new

    def decrease_fcp_usage(self, fcp, assigner_id=None):
        # TODO: check assigner_id to make sure on the correct fcp record
        connections = self.db.decrease_usage(fcp)

        return connections

    def unreserve_fcp(self, fcp, assigner_id=None):
        # TODO: check assigner_id to make sure on the correct fcp record
        self.db.unreserve(fcp)

    def is_reserved(self, fcp):
        self.db.is_reserved(fcp)

    def get_available_fcp(self, assigner_id, reserve):
        """get all the fcps not reserved, choose one from path0
           and choose another from path1, compose a pair to return.
           result will only have two FCP IDs, looks like [0011, 0021]
        """
        available_list = []
        if not reserve:
            # go here, means try to detach volumes, cinder still need the info
            # of the FCPs belongs to assigner to do some cleanup jobs
            fcp_list = self.db.get_reserved_fcps_from_assigner(assigner_id)
            LOG.info("Got fcp records %s belonging to instance %s in "
                     "Unreserve mode." % (fcp_list, assigner_id))
            # in this case, we just return the fcp_list
            # no need to allocated new ones if fcp_list is empty
            for old_fcp in fcp_list:
                available_list.append(old_fcp[0])
            return available_list

        # go here, means try to attach volumes
        # first check whether this userid already has a FCP device
        # get the FCP devices belongs to assigner_id
        fcp_list = self.db.get_allocated_fcps_from_assigner(assigner_id)
        LOG.info("Previously allocated records %s for instance %s." %
                 (fcp_list, assigner_id))
        if not fcp_list:
            # allocate new ones if fcp_list is empty
            LOG.info("There is no allocated fcps for %s, will allocate "
                     "new ones." % assigner_id)
            if CONF.volume.get_fcp_pair_with_same_index:
                '''
                If use get_fcp_pair_with_same_index,
                then fcp pair is randomly selected from below combinations.
                [fa00,fb00],[fa01,fb01],[fa02,fb02]
                '''
                free_unreserved = self.db.get_fcp_pair_with_same_index()
            else:
                '''
                If use get_fcp_pair,
                then fcp pair is randomly selected from below combinations.
                [fa00,fb00],[fa01,fb00],[fa02,fb00]
                [fa00,fb01],[fa01,fb01],[fa02,fb01]
                [fa00,fb02],[fa01,fb02],[fa02,fb02]
                '''
                free_unreserved = self.db.get_fcp_pair()
            for item in free_unreserved:
                available_list.append(item)
                # record the assigner id in the fcp so that
                # when the vm provision with both root and data volumes
                # the root and data volume would get the same FCP devices
                # with the get_volume_connector call.
                self.db.assign(item, assigner_id, update_connections=False)

            LOG.info("Newly allocated %s fcp for %s assigner" %
                      (available_list, assigner_id))
        else:
            # reuse the old ones if fcp_list is not empty
            LOG.info("Found allocated fcps %s for %s, will reuse them."
                     % (fcp_list, assigner_id))
            path_count = self.db.get_path_count()
            if len(fcp_list) != path_count:
                # TODO: handle the case when len(fcp_list) < multipath_count
                LOG.warning("FCPs previously assigned to %s includes %s, "
                            "it is not equal to the path count: %s." %
                            (assigner_id, fcp_list, path_count))
            # we got it from db, let's reuse it
            for old_fcp in fcp_list:
                available_list.append(old_fcp[0])

        return available_list

    def get_wwpn(self, fcp_no):
        fcp = self._fcp_pool.get(fcp_no)
        if not fcp:
            return None
        npiv = fcp.get_npiv_port()
        physical = fcp.get_physical_port()
        if npiv:
            return npiv
        if physical:
            return physical
        return None

    def get_all_fcp_pool(self, assigner_id):
        all_fcp_info = self._get_all_fcp_info(assigner_id)
        all_fcp_pool = {}
        lines_per_item = 5
        num_fcps = len(all_fcp_info) // lines_per_item
        for n in range(0, num_fcps):
            fcp_init_info = all_fcp_info[(5 * n):(5 * (n + 1))]
            fcp = FCP(fcp_init_info)
            dev_no = fcp.get_dev_no()
            all_fcp_pool[dev_no] = fcp
        return all_fcp_pool

    def get_wwpn_for_fcp_not_in_conf(self, all_fcp_pool, fcp_no):
        fcp = all_fcp_pool.get(fcp_no)
        if not fcp:
            return None
        npiv = fcp.get_npiv_port()
        physical = fcp.get_physical_port()
        if npiv:
            return npiv
        if physical:
            return physical
        return None

    def get_physical_wwpn(self, fcp_no):
        fcp = self._fcp_pool.get(fcp_no)
        if not fcp:
            return None
        physical = fcp.get_physical_port()
        return physical


# volume manager for FCP protocol
class FCPVolumeManager(object):
    def __init__(self):
        self.fcp_mgr = FCPManager()
        self.config_api = VolumeConfiguratorAPI()
        self._smtclient = smtclient.get_smtclient()
        self._lock = threading.RLock()
        self.db = database.FCPDbOperator()

    def _dedicate_fcp(self, fcp, assigner_id):
        self._smtclient.dedicate_device(assigner_id, fcp, fcp, 0)

    def _add_disks(self, fcp_list, assigner_id, target_wwpns, target_lun,
                   multipath, os_version, mount_point,):
        self.config_api.config_attach(fcp_list, assigner_id, target_wwpns,
                                      target_lun, multipath, os_version,
                                      mount_point)

    def _rollback_dedicated_fcp(self, fcp_list, assigner_id,
                                all_fcp_list=None):
        # fcp param should be a list
        for fcp in fcp_list:
            with zvmutils.ignore_errors():
                LOG.info("Rolling back dedicated FCP: %s" % fcp)
                connections = self.fcp_mgr.decrease_fcp_usage(fcp, assigner_id)
                if connections == 0:
                    self._undedicate_fcp(fcp, assigner_id)
        # If attach volume fails, we need to unreserve all FCP devices.
        if all_fcp_list:
            for fcp in all_fcp_list:
                if not self.db.get_connections_from_fcp(fcp):
                    LOG.info("Unreserve the fcp device %s", fcp)
                    self.db.unreserve(fcp)

    def _attach(self, fcp_list, assigner_id, target_wwpns, target_lun,
                multipath, os_version, mount_point, path_count,
                is_root_volume):
        """Attach a volume

        First, we need translate fcp into local wwpn, then
        dedicate fcp to the user if it's needed, after that
        call smt layer to call linux command
        """
        LOG.info("Start to attach volume to FCP devices "
                 "%s on machine %s." % (fcp_list, assigner_id))

        # TODO: init_fcp should be called in contructor function
        # but no assigner_id in contructor
        self.fcp_mgr.init_fcp(assigner_id)
        # fcp_status is like { '1a10': 'True', '1b10', 'False' }
        # True or False means it is first attached or not
        # We use this bool value to determine dedicate or not
        fcp_status = {}
        for fcp in fcp_list:
            fcp_status[fcp] = self.fcp_mgr.add_fcp_for_assigner(fcp,
                                                                assigner_id)
        if is_root_volume:
            LOG.info("Is root volume, adding FCP records %s to %s is "
                     "done." % (fcp_list, assigner_id))
            # FCP devices for root volume will be defined in user directory
            return []

        LOG.debug("The status of fcp devices before "
                  "dedicating them to %s is: %s." % (assigner_id, fcp_status))

        try:
            # dedicate the new FCP devices to the userid
            for fcp in fcp_list:
                if fcp_status[fcp]:
                    # only dedicate the ones first attached
                    LOG.info("Start to dedicate FCP %s to "
                             "%s." % (fcp, assigner_id))
                    self._dedicate_fcp(fcp, assigner_id)
                    LOG.info("FCP %s dedicated to %s is "
                             "done." % (fcp, assigner_id))
                else:
                    LOG.info("This is not the first volume for FCP %s, "
                             "skip dedicating FCP device." % fcp)
            # online and configure volumes in target userid
            self._add_disks(fcp_list, assigner_id, target_wwpns,
                            target_lun, multipath, os_version,
                            mount_point)
        except exception.SDKBaseException as err:
            errmsg = ("Dedicate FCP devices failed with "
                      "error:" + err.format_message())
            LOG.error(errmsg)
            self._rollback_dedicated_fcp(fcp_list, assigner_id,
                                         all_fcp_list=fcp_list)
            raise exception.SDKBaseException(msg=errmsg)
        LOG.info("Attaching volume to FCP devices %s on machine %s is "
                 "done." % (fcp_list, assigner_id))

    def volume_refresh_bootmap(self, fcpchannels, wwpns, lun,
                               wwid='',
                               transportfiles=None, guest_networks=None):
        ret = None
        with zvmutils.acquire_lock(self._lock):
            LOG.debug('Enter lock scope of volume_refresh_bootmap.')
            ret = self._smtclient.volume_refresh_bootmap(fcpchannels, wwpns,
                                        lun, wwid=wwid,
                                        transportfiles=transportfiles,
                                        guest_networks=guest_networks)
        LOG.debug('Exit lock of volume_refresh_bootmap with ret %s.' % ret)
        return ret

    def attach(self, connection_info):
        """Attach a volume to a guest

        connection_info contains info from host and storage side
        this mostly includes
        host side FCP: this can get host side wwpn
        storage side wwpn
        storage side lun

        all the above assume the storage side info is given by caller
        """
        fcp = connection_info['zvm_fcp']
        wwpns = connection_info['target_wwpn']
        target_lun = connection_info['target_lun']
        assigner_id = connection_info['assigner_id']
        assigner_id = assigner_id.upper()
        multipath = connection_info['multipath']
        multipath = multipath.lower()
        if multipath == 'true':
            multipath = True
        else:
            multipath = False
        os_version = connection_info['os_version']
        mount_point = connection_info['mount_point']
        is_root_volume = connection_info.get('is_root_volume', False)

        # TODO: check exist in db?
        if is_root_volume is False and \
                not zvmutils.check_userid_exist(assigner_id):
            LOG.error("User directory '%s' does not exist." % assigner_id)
            raise exception.SDKObjectNotExistError(
                    obj_desc=("Guest '%s'" % assigner_id), modID='volume')
        else:
            # TODO: the length of fcp is the count of paths in multipath
            path_count = len(fcp)
            # transfer to lower cases
            fcp_list = [x.lower() for x in fcp]
            target_wwpns = [wwpn.lower() for wwpn in wwpns]
            self._attach(fcp_list, assigner_id,
                         target_wwpns, target_lun,
                         multipath, os_version,
                         mount_point, path_count,
                         is_root_volume)

    def _undedicate_fcp(self, fcp, assigner_id):
        self._smtclient.undedicate_device(assigner_id, fcp)

    def _remove_disks(self, fcp_list, assigner_id, target_wwpns, target_lun,
                      multipath, os_version, mount_point, connections):
        self.config_api.config_detach(fcp_list, assigner_id, target_wwpns,
                                      target_lun, multipath, os_version,
                                      mount_point, connections)

    def _detach(self, fcp_list, assigner_id, target_wwpns, target_lun,
            multipath, os_version, mount_point, is_root_volume,
            update_connections_only):
        """Detach a volume from a guest"""
        LOG.info("Start to detach volume on machine %s from "
                 "FCP devices %s" % (assigner_id, fcp_list))
        # fcp_connections is like {'1a10': 0, '1b10': 3}
        # the values are the connections colume value in database
        fcp_connections = {}
        # need_rollback is like {'1a10': False, '1b10': True}
        # if need_rollback set to True, we need rollback
        # when some actions failed
        need_rollback = {}
        for fcp in fcp_list:
            # need_rollback default to True
            need_rollback[fcp] = True
            try:
                connections = self.fcp_mgr.decrease_fcp_usage(fcp, assigner_id)
            except exception.SDKObjectNotExistError:
                connections = 0
                # if the connections already are 0 before decreasing it,
                # there might be something wrong, no need to rollback
                # because rollback increase connections and the FCPs
                # are not available anymore
                need_rollback[fcp] = False
                LOG.warning("The connections of FCP device %s is 0.", fcp)
            fcp_connections[fcp] = connections

        # If is root volume we only need update database record
        # because the dedicate is done by volume_refresh_bootmap
        # If update_connections set to True, means upper layer want
        # to update database record only. For example, try to delete
        # the instance, then no need to waste time on undedicate
        if is_root_volume or update_connections_only:
            if update_connections_only:
                LOG.info("Update connections only, deleting FCP records %s "
                         "from %s is done." % (fcp_list, assigner_id))
            else:
                LOG.info("Is root volume, deleting FCP records %s from %s is "
                         "done." % (fcp_list, assigner_id))
            return

        # when detaching volumes, if userid not exist, no need to
        # raise exception. we stop here after the database operations done.
        if not zvmutils.check_userid_exist(assigner_id):
            LOG.warning("Found %s not exist when trying to detach volumes "
                        "from it.", assigner_id)
            return

        try:
            self._remove_disks(fcp_list, assigner_id, target_wwpns, target_lun,
                               multipath, os_version, mount_point, connections)
            for fcp in fcp_list:
                if not fcp_connections.get(fcp, 0):
                    LOG.info("Start to undedicate FCP %s from "
                             "%s." % (fcp, assigner_id))
                    self._undedicate_fcp(fcp, assigner_id)
                    LOG.info("FCP %s undedicated from %s is "
                             "done." % (fcp, assigner_id))
                else:
                    LOG.info("Found still have volumes on FCP %s, "
                             "skip undedicating FCP device." % fcp)
        except (exception.SDKBaseException,
                exception.SDKSMTRequestFailed) as err:
            rc = err.results['rc']
            rs = err.results['rs']
            if rc == 404 or rc == 204 and rs == 8:
                # We ignore the already undedicate FCP device exception.
                LOG.warning("The FCP device %s has already undedicdated", fcp)
            else:
                errmsg = "detach failed with error:" + err.format_message()
                LOG.error(errmsg)
                for fcp in fcp_list:
                    if need_rollback.get(fcp, True):
                        # rollback the connections data before remove disks
                        LOG.info("Rollback usage of fcp %s on instance %s."
                                 % (fcp, assigner_id))
                        self.fcp_mgr.increase_fcp_usage(fcp, assigner_id)
                        _userid, _reserved, _conns = self.get_fcp_usage(fcp)
                        LOG.info("After rollback, fcp usage of %s "
                                 "is (assigner_id: %s, reserved:%s, "
                                 "connections: %s)."
                                 % (fcp, _userid, _reserved, _conns))
                with zvmutils.ignore_errors():
                    self._add_disks(fcp_list, assigner_id,
                                    target_wwpns, target_lun,
                                    multipath, os_version, mount_point)
                raise exception.SDKBaseException(msg=errmsg)
        LOG.info("Detaching volume on machine %s from FCP devices %s is "
                 "done." % (assigner_id, fcp_list))

    def detach(self, connection_info):
        """Detach a volume from a guest
        """
        fcp = connection_info['zvm_fcp']
        wwpns = connection_info['target_wwpn']
        target_lun = connection_info['target_lun']
        assigner_id = connection_info['assigner_id']
        assigner_id = assigner_id.upper()
        multipath = connection_info['multipath']
        os_version = connection_info['os_version']
        mount_point = connection_info['mount_point']
        multipath = multipath.lower()
        if multipath == 'true':
            multipath = True
        else:
            multipath = False

        is_root_volume = connection_info.get('is_root_volume', False)
        update_connections_only = connection_info.get(
                'update_connections_only', False)
        # transfer to lower cases
        fcp_list = [x.lower() for x in fcp]
        target_wwpns = [wwpn.lower() for wwpn in wwpns]
        self._detach(fcp_list, assigner_id,
                     target_wwpns, target_lun,
                     multipath, os_version, mount_point,
                     is_root_volume, update_connections_only)

    def get_volume_connector(self, assigner_id, reserve):
        """Get connector information of the instance for attaching to volumes.

        Connector information is a dictionary representing the ip of the
        machine that will be making the connection, the name of the iscsi
        initiator and the hostname of the machine as follows::

            {
                'zvm_fcp': [fcp]
                'wwpns': [wwpn]
                'phy_to_virt_initiators':{virt:physical}
                'host': host
            }
        """

        empty_connector = {'zvm_fcp': [], 'wwpns': [], 'host': '',
                           'phy_to_virt_initiators': {}}
        # get lpar name of the userid, if no host name got, raise exception
        zvm_host = zvmutils.get_lpar_name()
        if zvm_host == '':
            errmsg = "failed to get zvm host."
            LOG.error(errmsg)
            raise exception.SDKVolumeOperationError(rs=11,
                                                    userid=assigner_id,
                                                    msg=errmsg)
        # init fcp pool
        self.fcp_mgr.init_fcp(assigner_id)
        # fcp = self.fcp_mgr.find_and_reserve_fcp(assigner_id)
        fcp_list = self.fcp_mgr.get_available_fcp(assigner_id, reserve)
        if not fcp_list:
            errmsg = "No available FCP device found."
            LOG.error(errmsg)
            return empty_connector
        wwpns = []
        phy_virt_wwpn_map = {}
        wwpn = None
        all_fcp_pool = {}
        # get wwpns of fcp devices
        for fcp_no in fcp_list:
            if self.fcp_mgr._fcp_pool.get(fcp_no):
                wwpn = self.fcp_mgr.get_wwpn(fcp_no)
            else:
                if not all_fcp_pool:
                    all_fcp_pool = self.fcp_mgr.get_all_fcp_pool(assigner_id)
                wwpn = self.fcp_mgr.get_wwpn_for_fcp_not_in_conf(all_fcp_pool,
                                                                 fcp_no)
            if not wwpn:
                errmsg = "FCP device %s has no available WWPN." % fcp_no
                LOG.error(errmsg)
            else:
                wwpns.append(wwpn)
            # We use initiator to build up zones on fabric, for NPIV, the
            # virtual ports are not yet logged in when we creating zones.
            # so we will generate the physical virtual initiator mapping
            # to determine the proper zoning on the fabric.
            # Refer to #7039 for details about avoid creating zones on
            # the fabric to which there is no fcp connected.
            phy_virt_wwpn_map[wwpn] = self.fcp_mgr.get_physical_wwpn(fcp_no)

        if not wwpns:
            errmsg = "No available WWPN found."
            LOG.error(errmsg)
            return empty_connector

        # reserve or unreserve FCP record in database
        for fcp_no in fcp_list:
            if reserve:
                # Reserve fcp device
                LOG.info("Reserve fcp device %s for "
                         "instance %s." % (fcp_no, assigner_id))
                self.db.reserve(fcp_no)
                _userid, _reserved, _conns = self.get_fcp_usage(fcp_no)
                LOG.info("After reserve, fcp usage of %s "
                         "is (assigner_id: %s, reserved:%s, connections: %s)."
                         % (fcp_no, _userid, _reserved, _conns))
            elif not reserve and \
                self.db.get_connections_from_fcp(fcp_no) == 0:
                # Unreserve fcp device
                LOG.info("Unreserve fcp device %s from "
                         "instance %s." % (fcp_no, assigner_id))
                self.db.unreserve(fcp_no)
                _userid, _reserved, _conns = self.get_fcp_usage(fcp_no)
                LOG.info("After unreserve, fcp usage of %s "
                         "is (assigner_id: %s, reserved:%s, connections: %s)."
                         % (fcp_no, _userid, _reserved, _conns))

        connector = {'zvm_fcp': fcp_list,
                     'wwpns': wwpns,
                     'phy_to_virt_initiators': phy_virt_wwpn_map,
                     'host': zvm_host}
        LOG.info('get_volume_connector returns %s for %s' %
                  (connector, assigner_id))
        return connector

    def check_fcp_exist_in_db(self, fcp, raise_exec=True):
        all_fcps_raw = self.db.get_all()
        all_fcps = []
        for item in all_fcps_raw:
            all_fcps.append(item[0].lower())
        if fcp not in all_fcps:
            if raise_exec:
                LOG.error("fcp %s not exist in db!", fcp)
                raise exception.SDKObjectNotExistError(
                    obj_desc=("FCP '%s'" % fcp), modID='volume')
            else:
                LOG.warning("fcp %s not exist in db!", fcp)
                return False
        else:
            return True

    def get_all_fcp_usage(self, assigner_id=None):
        """Get all fcp information grouped by FCP id.
        Every item under one FCP is like:
            [userid, reserved, connections, path].
        For example, the return value format should be:
        {
          '1a00': ('userid1', 2, 1, 0),
          '1a01': ('userid2', 1, 1, 0),
          '1b00': ('userid1', 2, 1, 1),
          '1b01': ('userid2', 1, 1, 1)
        }
        """
        ret = self.db.get_all_fcps_of_assigner(assigner_id)
        if assigner_id:
            LOG.info("Got all fcp usage of userid %s: %s" % (assigner_id, ret))
        else:
            # if userid is None, get usage of all the fcps
            LOG.info("Got all fcp usage: %s" % ret)
        # transfer records into dict grouped by FCP id
        fcp_id_mapping = {}
        for item in ret:
            fcp_id = item[0]
            fcp_id_mapping[fcp_id] = item
        return fcp_id_mapping

    def get_all_fcp_usage_grouped_by_path(self, assigner_id=None):
        """Get all fcp information grouped by path id.
        Every item under one path format:i
            [fcp_id, userid, reserved, connections, path].
        For example, the return value format should be:
        {
          0: [ (u'1a00', 'userid1', 2, 1, 0), (u'1a01', 'userid2', 1, 1, 0) ],
          1: [ (u'1b00', 'userid1', 2, 1, 1), (u'1b01', 'userid2', 1, 1, 1) ]
        }
        """
        # get FCP records from database
        ret = self.db.get_all_fcps_of_assigner(assigner_id)
        if assigner_id:
            LOG.info("Got all fcp usage of userid %s: %s" % (assigner_id, ret))
        else:
            # if userid is None, get usage of all the fcps
            LOG.info("Got all fcp usage: %s" % ret)
        # transfer records into dict grouped by path id
        path_fcp_mapping = {}
        for item in ret:
            path_id = item[4]
            if not path_fcp_mapping.get(path_id, None):
                path_fcp_mapping[path_id] = []
            path_fcp_mapping[path_id].append(item)
        return path_fcp_mapping

    def get_fcp_usage(self, fcp):
        userid, reserved, connections = self.db.get_usage_of_fcp(fcp)
        LOG.debug("Got userid:%s, reserved:%s, connections:%s of "
                  "FCP:%s" % (userid, reserved, connections, fcp))
        return userid, reserved, connections

    def set_fcp_usage(self, fcp, assigner_id, reserved, connections):
        self.db.update_usage_of_fcp(fcp, assigner_id, reserved, connections)
        LOG.info("Set usage of fcp %s to userid:%s, reserved:%s, "
                 "connections:%s." % (fcp, assigner_id, reserved, connections))
