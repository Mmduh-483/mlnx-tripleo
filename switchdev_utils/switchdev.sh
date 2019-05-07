#!/bin/bash


set -ex
set -o pipefail
exec 1> >(logger -s -t $(basename $1)) 2>&1

REP_LINK_NAME_FILE="/etc/udev/rep-link-name.sh"
UDEV_RULE_FILE='/etc/udev/rules.d/70-persistent-net.rules'


create_rep_link_name_script(){
  cat > $REP_LINK_NAME_FILE <<EOL
#!/bin/bash
SWID="\$1"
PORT="\$2"
parent_phys_port_name=\${PORT%vf*}
parent_phys_port_name=\${parent_phys_port_name//f}
for i in \`ls -1 /sys/class/net/*/phys_port_name\`
do
    nic=\`echo \$i | cut -d/ -f 5\`
    sw_id=\`cat /sys/class/net/\$nic/phys_switch_id 2>/dev/null\`
    phys_port_name=\`cat /sys/class/net/\$nic/phys_port_name 2>/dev/null\`
    if [ "\$parent_phys_port_name" = "\$phys_port_name" ] &&
       [ "\$sw_id" = "\$SWID" ]
    then
        echo "NAME=\${nic}_\${PORT##pf*vf}"
        break
        exit
    fi
done
EOL
  chmod 755 $REP_LINK_NAME_FILE
}


reload_udev_rules(){
  /usr/sbin/udevadm control --reload-rules
}


add_udev_rule(){
  if ! test -f "$2"
  then
    echo "$1" > "$2"
    reload_udev_rules
  else
    if ! grep -Fxq "$1" "$2"
    then
      echo "$1" >> "$2"
      reload_udev_rules
    fi
  fi
}


add_udev_rule_for_sriov_pf(){
    pf_pci=$(grep PCI_SLOT_NAME /sys/class/net/$1/device/uevent | cut -d'=' -f2)
    udev_data_line="SUBSYSTEM==\"net\", ACTION==\"add\", DRIVERS==\"?*\", "\
"KERNELS==\"$pf_pci\", NAME=\"$1\""
    add_udev_rule "$udev_data_line" "$UDEV_RULE_FILE"
}


add_udev_rule_for_vf_representors(){
  udev_data_line="SUBSYSTEM==\"net\", ACTION==\"add\", ATTR{phys_switch_id}"\
"!=\"\", ATTR{phys_port_name}==\"pf*vf*\", "\
"IMPORT{program}=\"$REP_LINK_NAME_FILE "\
"\$attr{phys_switch_id} \$attr{phys_port_name}\" "\
"NAME=\"\$env{NAME}\""
  create_rep_link_name_script
  add_udev_rule "$udev_data_line" "$UDEV_RULE_FILE"

}


##################################################
##################################################
####################   MAIN   ####################
##################################################
##################################################


# Configuring num of vfs for the interface
vendor_id="$(cat /sys/class/net/$1/device/vendor)"
if [ "$(cat /sys/class/net/$1/device/sriov_numvfs)" == "0" ]
then
  echo $2 >/sys/class/net/$1/device/sriov_numvfs
else
  exit 0
fi

# Unbinding the vfs for mellanox interfaces
if [ $vendor_id == "0x15b3" ]
then
  vfs_pci_list=$(grep PCI_SLOT_NAME /sys/class/net/$1/device/virtfn*/uevent | cut -d'=' -f2)
  for pci in $vfs_pci_list
  do
    echo "$pci" > /sys/bus/pci/drivers/mlx5_core/unbind
  done
fi

# Adding a udev rule to save the sriov_pf name
add_udev_rule_for_sriov_pf $1

# Adding a udev rule for VF representor rename
add_udev_rule_for_vf_representors

# Moving the interface to switchdev mode
interface_pci=$(grep PCI_SLOT_NAME /sys/class/net/$1/device/uevent | cut -d'=' -f2)
/usr/sbin/devlink dev eswitch set pci/"$interface_pci" mode switchdev

# ifup the interface

/usr/sbin/ifup $1
if [[ "$(/usr/sbin/devlink dev eswitch show pci/"$interface_pci")" =~ "mode switchdev" ]]
then
  echo "PCI device $interface_pci set to mode switchdev."
else
  echo "Failed to set PCI device $interface_pci to mode switchdev."
  exit 1
fi
interface_device=$(cat /sys/class/net/$1/device/device)
if [ "$interface_device" == "0x1013" ] || [ "$interface_device" == "0x1015" ]
then
  /usr/sbin/devlink dev eswitch set pci/"$interface_pci" inline-mode transport
fi

# Enabling hw-tc-offload for the interface
/usr/sbin/ethtool -K $1 hw-tc-offload on

# Enabling  hw-offload in ovs
if [[ ! $(ovs-vsctl get Open_Vswitch . other_config:hw-offload | grep -i true) ]]
then
  ovs-vsctl set Open_Vswitch  . other_config:hw-offload=true
fi

