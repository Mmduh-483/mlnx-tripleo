#!/bin/bash

switchdev_config_dir="/etc/switchdev-config/"

# Removing sriov_config service
echo "Removing sriov_config service"
systemctl disable sriov_config
rm -f "/etc/systemd/system/sriov_config.service"

# Removing sriov_bind service
echo "Removing sriov_bind service"
systemctl disable sriov_bind
rm -f "/etc/systemd/system/sriov_bind.service"

# Removing switchdev_network_config script
echo "Removing switchdev_network_config script"
rm -f "$switchdev_config_dir/switchdev_network_config"

# Removing udev rules
echo "Removing udev rules"
rm -f "/etc/udev/rules.d/90-sriov-config.rules"
rm -f "/etc/udev/rep-link-name.sh"
