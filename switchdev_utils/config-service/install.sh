#!/bin/bash

switchdev_config_dir="/etc/switchdev-config/"
# Creating switchdev-config directory
echo "Creating switchdev-config directory $switchdev_config_dir"
mkdir -p $switchdev_config_dir

# Copying switchdev_network_config and uninstall.sh to /etc/switchdev-config/
echo "Copying switchdev_network_config and uninstall.sh to $switchdev_config_dir"
cp ./switchdev_network_config $switchdev_config_dir
cp ./uninstall.sh $switchdev_config_dir

if [ $# -ne 0 ]
then
  config_file=$1
  if test -f $config_file
  then
    # Copying config yaml file to /etc/switchdev-config/"
    echo "Copying config $config_file to $switchdev_config_dir"
    cp $config_file "$switchdev_config_dir/config.yaml"
  fi
fi
