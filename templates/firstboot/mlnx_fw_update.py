#!/usr/bin/env python

import logging
import os
import re
import shutil
import tempfile
import threading

from six.moves import html_parser
from six.moves.urllib import request as urlRequest
from six.moves.urllib import error as urlError

from oslo_concurrency import processutils

FW_VERSION_REGEX = r'FW Version:\s*\t*(?P<fw_ver>\d+\.\d+\.\d+)'
RUNNING_FW_VERSION_REGEX = r'FW Version\(Running\):\s*\t*(?P<fw_ver>\d+\.\d+\.\d+)'
PSID_REGEX = r'PSID:\s*\t*(?P<psid>\w+)'

_DEV_WHITE_LIST = $DEV_WHITE_LIST
_FORCE_UPDATE = $FORCE_UPDATE
_BIN_DIR_URL = "$BIN_DIR_URL"

#TODO(adrianc): add configurable parameter for logging
logging.basicConfig(
    filename='/var/log/mellnox_fw_update.log',
    filemode='w',
    level=logging.DEBUG)
LOG = logging.getLogger("mellnox_fw_update")

_MLX_CONFIG = {
    "SRIOV_EN": "$SRIOV_EN",
    "NUM_OF_VFS": "$NUM_OF_VFS",
    "LINK_TYPE_P1": "$LINK_TYPE",
    "LINK_TYPE_P2": "$LINK_TYPE",
    "ESWITCH_IPV4_TTL_MODIFY_ENABLE": "$ESWITCH_IPV4_TTL_MODIFY_ENABLE",
    "PRIO_TAG_REQUIRED_EN": "$PRIO_TAG_REQUIRED_EN"
}


def run_command(*cmd, **kwargs):
    try:
        out, err = processutils.execute(*cmd, **kwargs)
    except processutils.ProcessExecutionError as e:
        LOG.error("Failed to execute %s, %s", ' '.join(cmd), str(e))
        raise e
    if err:
        LOG.warning("Got stderr output: %s" % err)
    LOG.debug(out)
    return out


def parse_mstflint_query_output(out):
    """ Parse Mstflint query output

    For now just extract 'FW Version' and 'PSID'

    :param out: mstflint query output
    :return: dictionary of query attributes
    """
    query_info = {}
    for line in out.split('\n'):
        fw_ver = re.match(FW_VERSION_REGEX, line)
        psid = re.match(PSID_REGEX, line)
        running_fw_ver = re.match(RUNNING_FW_VERSION_REGEX, line)
        if fw_ver:
            query_info["fw_ver"] = fw_ver.group('fw_ver')
        if running_fw_ver:
            query_info["running_fw_ver"] = running_fw_ver.group('fw_ver')
        if psid:
            query_info["psid"] = psid.group('psid')
    return query_info


class MlnxDevices(object):
    """ Discover and retrieve Mellanox PCI devices.

    Can be used as an iterator once discover has been called.
    """

    def __init__(self, dev_white_list):
        self._devs = []
        self._dev_white_list = dev_white_list

    def discover(self):
        """ Discover Mellanox devices in the system. (first PF of every device)

        :return: None
        """
        if self._devs:
            return self._devs

        devs = []
        cmd = ['lspci', '-D', '-d', '15b3:']
        out = run_command(*cmd)
        for line in out.split('\n'):
            if not line:
                continue
            dev = line.split()[0]
            if dev.endswith('.0') and (not self._dev_white_list or
                    dev in self._dev_white_list):
                devs.append(dev)
        self._devs = devs
        LOG.info("Found Mellanox devices: %s", devs)
        other_devs = set(self._dev_white_list) - set(devs)
        if other_devs:
             LOG.warning("Not all devices in PCI white list where discovered,"
                         " %s these may not be mellanox devices or have their "
                         "PCI function set to non zero." % other_devs)

    def __len__(self):
        return len(self._devs)

    def __iter__(self):
        return self._devs.__iter__()

    def __next__(self):
        return self._devs.__next__()


class MlnxDeviceConfig(object):
    """ Get/Set Mellanox Device configurations
    """

    def __init__(self, pci_dev):
        self.pci_dev = pci_dev
        self._tool_confs = None

    def _mstconfig_parse_data(self, data):
        # Parsing the mstconfig out to json
        data = list(filter(None, data.split('\n')))
        r = {}
        c = 0
        for line in data:
            c += 1
            if 'Configurations:' in line:
                break
        for i in range(c, len(data)):
            d = list(filter(None, data[i].strip().split()))
            r[d[0]] = d[1]
        return r

    def get_device_conf_dict(self):
        """ Get device Configurations

        :return:  dict {"PARAM_NAME": "Param value", ....}
        """
        LOG.info("Getting configurations for device: %s" % self.pci_dev)
        out = run_command("mstconfig", "-d", self.pci_dev, "q")
        return self._mstconfig_parse_data(out)

    def param_supp_by_config_tool(self, param_name):
        """ Check if configuration tool supports the provided configuration
        parameter.

        :param param_name: configuration name
        :return: bool
        """
        if self._tool_confs is None:
            self._tool_confs = run_command("mstconfig", "-d", self.pci_dev, "i")
        return param_name in self._tool_confs

    def set_config(self, conf_dict):
        """ Set device configurations

        :param conf_dict: a dictionary of:
                          {"PARAM_NAME": "Param value to set", ...}
        :return: None
        """
        current_mlx_config = self.get_device_conf_dict()
        params_to_set = []
        for key, value in conf_dict.items():
            if not self.param_supp_by_config_tool(key):
                LOG.error("Configuraiton: %s is not supported by mstconfig,"
                            " please update to the latest mstflint package." % key)
                continue
            if current_mlx_config.get(key) and value.lower(
            ) not in current_mlx_config.get(key).lower():
                # Aggregate all configurations required to be modified
                params_to_set.append("%s=%s" % (key, value))

        if params_to_set:
            LOG.info("Setting configurations for device: %s" % self.pci_dev)
            run_command("mstconfig", "-d", self.pci_dev, "-y",
                        "set", *params_to_set)
            LOG.info("Set device configurations: Setting %s done successfully",
                     " ".join(params_to_set))
        else:
            LOG.info("Set device configurations: No operation required")


class MlnxFirmwareBinary(object):

    def __init__(self, local_bin_path):
        self.bin_path = local_bin_path
        self.image_info = {}

    def get_info(self):
        """ Get firmware information from binary

        Caller should wrap this call under try catch to skip non compliant
        firmware binaries.

        :return: dict of firmware image attributes
        """
        if self.image_info.get('file_path', '') == self.bin_path:
            return self.image_info
        self.image_info = {'file_path': self.bin_path}
        cmd = ['mstflint', '-i', self.bin_path, 'query']
        out = run_command(*cmd)
        self.image_info.update(parse_mstflint_query_output(out))
        # Note(adrianc): deep copy ?
        return self.image_info


class MlnxFirmwareBinariesFetcher(object):
    """ A class for fetching firmware binaries form a directory
    provided by a URL link

    Note: URL MUST point to a directory and end with '/'
    e.g http://www.mysite.com/mlnx_bins/
    """
    dest_dir = tempfile.mkdtemp(suffix="tripleo_mlnx_firmware")

    class FileHTMLParser(html_parser.HTMLParser):
        """ A crude HTML Parser to extract files from an HTTP response.
        """

        def __init__(self, suffix):
            # HTMLParser is Old style class dont use super() method
            html_parser.HTMLParser.__init__(self)
            self.matches = []
            self.suffix = suffix

        def handle_starttag(self, tag, attrs):
            for name, val in attrs:
                if name == 'href' and val.endswith(self.suffix):
                    self.matches.append(val)

    def __init__(self, url):
        self.url = url

    def __del__(self):
        self._cleanup_dest_dir()

    def _cleanup_dest_dir(self):
        if os.path.exists(MlnxFirmwareBinariesFetcher.dest_dir):
            shutil.rmtree(MlnxFirmwareBinariesFetcher.dest_dir)

    def _get_file_from_url(self, file_name):
        try:
            full_path = self.url + "/" + file_name
            LOG.info("Downloading file: %s to %s", full_path,
                     MlnxFirmwareBinariesFetcher.dest_dir)
            url_data = urlRequest.urlopen(full_path)
        except urlError.HTTPError as e:
            LOG.error("Failed to download data: %s", str(e))
            raise e
        dest_file_path = os.path.join(MlnxFirmwareBinariesFetcher.dest_dir,
                                      file_name)
        with open(dest_file_path, 'wb') as f:
            f.write(url_data.read())
        return dest_file_path

    def _get_file_create_bin_obj(self, file_name, fw_bins):
        """ This wrapper method will download a firmware binary,
        create MlnxFirmwareBinary object and append to the provided
        fw_bins list.

        :return: None
        """
        try:
            dest_file_path = self._get_file_from_url(file_name)
            fw_bin = MlnxFirmwareBinary(dest_file_path)
            # Note(adrianc): Pre query image, to skip incompatible files
            # in case of Error
            fw_bin.get_info()
            fw_bins.append(fw_bin)
        except Exception as e:
            LOG.warning("Failed to download and query %s, skipping file. "
                        "%s", file_name, str(e))

    def get_firmware_binaries(self):
        """ Get Firmware binaries

        :return: list containing the files downloaded
        """
        # get list of files
        # download into dest_dir
        # for each file, create MlnxFirmwareBinary
        # return list of the MlnxFirmwareBinary
        if not self.url.endswith('/'):
            LOG.error("Bad URL provided (%s), expected URL to be a directory",
                      self.url)
            raise RuntimeError('Failed to get firmware binaries, '
                               'expected directory URL path '
                               '(e.g "http://<your_ip>/mlnx_bins/"). '
                               'Given URL path: %s', self.url)
        try:
            index_data = str(urlRequest.urlopen(_BIN_DIR_URL).read())
        except urlError.HTTPError as err:
            LOG.error(err)
            raise err
        parser = MlnxFirmwareBinariesFetcher.FileHTMLParser(suffix=".bin")
        parser.feed(index_data)
        parser.close()
        if not parser.matches:
            LOG.warning("No bin Files found in the provided URL: %s", self.url)

        fw_bins = []
        threads = []
        for file_name in parser.matches:
            # TODO(adrianc) fetch files async with co-routines,
            # may need to limit thread count
            t = threading.Thread(target=self._get_file_create_bin_obj,
                                 args=(file_name, fw_bins))
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        return fw_bins


class MlnxDevFirmwareOps(object):
    """ Perform various Firmware related operations on device
    """

    def __init__(self, dev):
        self.dev = dev
        self.dev_info = {}

    def query_device(self, force=False):
        """ Get firmware information from device

        :param force: force device query, even if query was executed in
                      previous calls.
        :return: dict of firmware image attributes
        """
        if not force and self.dev_info.get('device', '') == self.dev:
            return self.dev_info

        self.dev_info = {'device': self.dev}
        cmd = ['mstflint', '-d', self.dev, '-qq', 'query']
        out = run_command(*cmd)
        self.dev_info = parse_mstflint_query_output(out)
        # Note(adrianc): deep copy ?
        return self.dev_info

    def need_update(self, image_info):
        """ Check if device requires firmware update

        :param image_info: image_info dict as returned from
                           MlnxFirmwareBinary.get_info()
        :return: bool, True if update is needed
        """
        if not self.dev_info:
            self.query_device()
        LOG.info("Device firmware version: %s, Image firmware version: %s" %
                 (self.dev_info['fw_ver'], image_info['fw_ver']))
        return self.dev_info['fw_ver'] < image_info['fw_ver']

    def need_reset_before_config(self):
        """ Check if device requires firmware reset before applying any
        configurations on the device.

        :return: bool, True if reset is needed
        """
        self.query_device(force=True)
        next_boot_image_newer = 'running_fw_ver' in self.dev_info and \
               self.dev_info['running_fw_ver'] < self.dev_info['fw_ver']
        if next_boot_image_newer:
            mandatory_params = ["ESWITCH_IPV4_TTL_MODIFY_ENABLE",
                                "PRIO_TAG_REQUIRED_EN"]
            device_config = MlnxDeviceConfig(self.dev)
            conf_dict = device_config.get_device_conf_dict()
            for param in mandatory_params:
                if param not in conf_dict and \
                        device_config.param_supp_by_config_tool(param):
                    return True
        return False

    def burn_firmware(self, image_path):
        """ Burn firmware on device

        :param image_path: firmware binary file path
        :return: None
        """
        LOG.info("Updating firmware image (%s) for device: %s",
                 image_path, self.dev)
        cmd = ["mstflint", "-d", self.dev, "-i", image_path,
               "-y", "burn"]
        run_command(*cmd)
        LOG.info("Device %s: Successfully updated.", self.dev)

    def reset_device(self):
        """ Reset firmware

        :return: None
        """
        LOG.info("Device %s: Performing firmware reset.", self.dev)
        cmd = ["mstfwreset", "-d", self.dev, "-y", "reset"]
        run_command(*cmd)
        LOG.info("Device %s: Firmware successfully reset.", self.dev)


def check_prereq():
    """ Check that all needed tools are available in the system.

    :return: None
    """
    try:
        # check for mstflint
        run_command('mstflint', '-v')
        # check for mstconfig
        run_command('mstconfig', '-v')
        # check for mstfwreset
        run_command('mstfwreset', '-v')
        # check for lspci
        run_command('lspci', '--version')
    except Exception as e:
        LOG.error("Failed Prerequisite check. %s", str(e))
        raise e


def process_device(pci_dev, psid_map):
    """ Process a single Mellanox device.

    Processing pipeline:
        - Perform firmware update if required
        - Reset device to load firmware if required
        - Perform device configurations if required

    :param pci_dev: mellanox PCI device address (String)
    :param psid_map: dict mapping between PSID and an image_info dict
    :return: None
    """
    try:
        LOG.info("Processing Device: %s", pci_dev)
        dev_ops = MlnxDevFirmwareOps(pci_dev)
        device_config = MlnxDeviceConfig(pci_dev)
        dev_query = dev_ops.query_device()
        # see if there is a matching bin
        dev_psid = dev_query['psid']
        if dev_psid in psid_map:
            if _FORCE_UPDATE or dev_ops.need_update(psid_map[dev_psid]):
                dev_ops.burn_firmware(psid_map[dev_psid]['file_path'])
            else:
                LOG.info("Firmware update is not required for Device.")
        else:
            LOG.warning("No firmware binary found for device %s with "
                        "PSID: %s, skipping...", pci_dev, dev_psid)
        # check if reset is required.
        # Note: device Reset is required if a newer firmware version was burnt
        # and current firmware does not support some mandatory configurations.
        if dev_ops.need_reset_before_config():
            dev_ops.reset_device()
        # set device configurations
        device_config.set_config(_MLX_CONFIG)
        LOG.info("Device %s processed successfully.", pci_dev)
    except Exception as e:
        LOG.error("Failed to process device %s. %s", pci_dev, str(e))


def main():
    check_prereq()
    # discover devices
    mlnx_devices = MlnxDevices(_DEV_WHITE_LIST)
    mlnx_devices.discover()
    # get binaries
    binary_getter = MlnxFirmwareBinariesFetcher(_BIN_DIR_URL)
    fw_binaries = binary_getter.get_firmware_binaries()
    # prep psid map
    psid_map = {}
    for fw_bin in fw_binaries:
        image_info = fw_bin.get_info()
        psid_map[image_info['psid']] = image_info
    # process devices
    for pci_dev in mlnx_devices:
        process_device(pci_dev, psid_map)


if __name__ == '__main__':
    main()
